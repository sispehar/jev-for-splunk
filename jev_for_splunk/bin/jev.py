#!/usr/bin/env python
"""| jev - ask TypeSafe Jev typed questions about every event and add the answers as fields.

Primitive form, one question per pipe stage:
    ... | jev noul message "The customer says they were charged more than they should have been" as overcharged
    ... | jev choice message "Which part of the shop is this about?" options=lookup:shop_areas as area
    ... | jev score message "How upset is the customer?" levels="calm; annoyed; angry" as upset

Battery form, several questions in one request per event:
    ... | jev battery=shop_messages
    ... | jev state="text=_raw" questions="{\"urgent\":{\"type\":\"noul\",\"instructions\":\"Does `text` convey urgency?\"}}"

Answers are cached per (model, state, question) in the KV store collection
jev_cache, so asking again, or asking the same question from a dashboard, only
pays for events that were never judged. See README/jev.conf.spec and
default/searchbnf.conf. The heavy lifting lives in jev_core.
"""
from __future__ import annotations

import os
import re
import sys
import time

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(BIN_DIR)
for _path in (os.path.join(BIN_DIR, "lib"), BIN_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from splunklib.searchcommands import Configuration, Option, StreamingCommand, dispatch, validators  # noqa: E402

from jev_core import __version__  # noqa: E402
from jev_core.battery import Battery, BatteryError, load_battery, parse_state_mapping  # noqa: E402
from jev_core.cache import SqliteCache  # noqa: E402
from jev_core.client import JevClient, JevError  # noqa: E402
from jev_core.config import load_config  # noqa: E402
from jev_core.options import FileLookupResolver, ResolveError  # noqa: E402
from jev_core.primitive import PrimitiveError, parse_positionals, primitive_question  # noqa: E402
from jev_core.runner import Evaluator  # noqa: E402
from jev_splunk import KVStoreCache, key_from_splunk, service_for  # noqa: E402

OWN_APP = os.path.basename(APP_ROOT)
OPTION_TOKEN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
BATTERY_ONLY = ("battery", "questions", "state", "prefix")
PRIMITIVE_ONLY = (("choice_options", "options"), ("levels", "levels"),
                  ("criteria_true", "criteria_true"), ("criteria_false", "criteria_false"))
PRICE_PER_TOKEN = 0.042 / 1000000.0


# required_fields=["*"]: the fields a question reads are named in its arguments, which Splunk's
# field optimizer cannot see. Without it, `| jev noul tool_parameters "..." | stats count BY user`
# never extracts tool_parameters (in fast and smart mode) and every event is skipped for lack of state.
@Configuration(distributed=False, required_fields=["*"])
class JevCommand(StreamingCommand):
    """Runs on the search head only: the API key, the KV cache and the outbound HTTPS route live there."""

    battery = Option(require=False, validate=validators.Match("battery", r"^(?:[A-Za-z0-9_.-]+:)?[a-z][a-z0-9_]*$"))
    questions = Option(require=False)
    state = Option(require=False)
    prefix = Option(require=False, validate=validators.Match("field prefix", r"^[A-Za-z_][A-Za-z0-9_]*$"))
    # named choice_options because an attribute called `options` would shadow SearchCommand.options
    choice_options = Option(name="options", require=False)
    levels = Option(require=False)
    criteria_true = Option(require=False)
    criteria_false = Option(require=False)
    model = Option(require=False, validate=validators.Match("model name", r"^[A-Za-z0-9._-]+$"))
    threads = Option(require=False, validate=validators.Integer(1, 32))
    rps = Option(require=False, validate=validators.Integer(1, 20))
    timeout = Option(require=False, validate=validators.Integer(1, 300))
    maxevents = Option(require=False, validate=validators.Integer(0, 10000000))
    maxstate = Option(require=False, validate=validators.Integer(500, 120000))
    probs = Option(require=False, validate=validators.Boolean())
    cache = Option(require=False, validate=validators.Set("kv", "memo", "refresh", "off"))
    dryrun = Option(require=False, validate=validators.Boolean())
    meta = Option(require=False, validate=validators.Set("basic", "full"))
    keepstate = Option(require=False, validate=validators.Boolean())

    def __init__(self):
        super(JevCommand, self).__init__()
        self.evaluator = None
        self._fatal = None
        self._emitted = set()
        self._field_prefix = "jev_"
        self._error_field = "jev_error"
        self._started = time.time()

    # -- argument parsing ------------------------------------------------------------
    def _protocol_v2_option_parser(self, arg):
        """Only a declared, unquoted name=value token is an option; everything else is positional.

        The SDK default splits any argument containing '=', which would break
        questions such as "Is 2+2=4 stated here?".
        """
        quoted = set()
        try:
            for token in getattr(self._metadata.searchinfo, "raw_args", None) or []:
                token = str(token)
                if len(token) >= 2 and token[0] == token[-1] == '"':
                    quoted.add(token[1:-1])
        except AttributeError:
            pass
        match = OPTION_TOKEN.match(arg)
        if match and match.group(1) in self.options and arg not in quoted:
            return [match.group(1), match.group(2)]
        return [arg]

    # -- setup -------------------------------------------------------------------
    def prepare(self):
        if self.fieldnames:
            try:
                self._error_field = parse_positionals(self.fieldnames).alias + "_error"
            except PrimitiveError:
                self._error_field = "jev_error"
        try:
            self._setup()
        except (BatteryError, JevError, PrimitiveError, ResolveError) as exc:
            self._fatal = str(getattr(exc, "message", None) or exc)
            self.logger.error("prepare failed: %s", self._fatal)
        except Exception as exc:  # keep the search alive with a readable message
            self._fatal = "%s: %s" % (type(exc).__name__, exc)
            self.logger.exception("prepare crashed")

    def _setup(self):
        info = self._metadata.searchinfo
        search_app = getattr(info, "app", None) or OWN_APP
        # JEV_APPS_DIR is a development override used by the tests; Splunk never sets it.
        apps_dir = os.environ.get("JEV_APPS_DIR") or os.path.dirname(APP_ROOT)
        resolver = FileLookupResolver(apps_dir, search_app=search_app, own_app=OWN_APP)
        extra_roots = []
        search_root = os.path.join(apps_dir, search_app)
        if search_app != OWN_APP and os.path.isdir(search_root):
            extra_roots.append(search_root)   # an app can pin a model in its own jev.conf
        cfg = load_config(APP_ROOT, extra_roots=extra_roots, overrides={
            "api": {"model": self.model, "timeout": self.timeout, "requests_per_second": self.rps},
            "defaults": {"threads": self.threads, "prefix": self.prefix, "maxevents": self.maxevents,
                         "maxstate": self.maxstate, "probs": self.probs},
            "cache": {"mode": self.cache},
        })
        self._field_prefix = cfg.prefix
        if not self.fieldnames:
            self._error_field = cfg.prefix + "error"

        alias = None
        if self.fieldnames:
            for name in BATTERY_ONLY:
                if getattr(self, name) is not None:
                    raise PrimitiveError("%s= cannot be combined with | jev noul|choice|score" % name)
            spec = parse_positionals(self.fieldnames)
            mapping = parse_state_mapping(spec.fields)
            if spec.alias in [field for field, _ in mapping.values()]:
                raise PrimitiveError("as %s would overwrite the field the question reads; pick another name" % spec.alias)
            options = resolver.options(self.choice_options) if self.choice_options is not None else None
            levels = resolver.levels(self.levels) if self.levels is not None else None
            wire = primitive_question(spec.mode, list(mapping.keys()), spec.text, options=options, levels=levels,
                                      criteria_true=self.criteria_true, criteria_false=self.criteria_false)
            battery = Battery(id="primitive", questions={spec.alias: wire}, state=spec.fields,
                              required_state=list(mapping.keys()), source="spl")
            battery.text_form.add(spec.alias)
            alias = spec.alias
        else:
            for attr, name in PRIMITIVE_ONLY:
                if getattr(self, attr) is not None:
                    raise PrimitiveError("%s= only applies to | jev noul|choice|score" % name)
            battery = load_battery(resolver, self.battery, self.questions, self.state)

        api_key = self._api_key(cfg)
        # JEV_ENDPOINT is a development override used by the protocol tests; Splunk never sets it.
        endpoint = os.environ.get("JEV_ENDPOINT") or cfg.endpoint
        client = JevClient(api_key, endpoint=endpoint, model=cfg.model, timeout=cfg.timeout,
                           max_retries=cfg.max_retries, requests_per_second=cfg.requests_per_second,
                           burst=cfg.threads, proxy_url=cfg.proxy_url, ca_bundle=cfg.ca_bundle, logger=self.logger)
        mode = cfg.cache_mode
        backend = None
        if mode in ("kv", "refresh"):
            dev_cache = os.environ.get("JEV_CACHE_PATH")  # development override; Splunk never sets it
            backend = SqliteCache(dev_cache) if dev_cache else KVStoreCache(
                service_for(info, OWN_APP), collection=cfg.cache_collection, app=OWN_APP)
        self.evaluator = Evaluator(client, battery, model=cfg.model, threads=cfg.threads, maxevents=cfg.maxevents,
                                   maxstate=cfg.maxstate, prefix=cfg.prefix, probs=bool(cfg.probs),
                                   keepstate=bool(self.keepstate), logger=self.logger, cache_mode=mode,
                                   backend=backend, ttl_days=cfg.cache_ttl_days, batch_size=cfg.cache_batch_size,
                                   dryrun=bool(self.dryrun), primitive_alias=alias, meta=self.meta or "basic")
        self.logger.info("ready version=%s form=%s battery=%s questions=%d model=%s cache=%s threads=%d maxevents=%d "
                         "sid=%s app=%s user=%s", __version__, "primitive" if alias else "battery", battery.id,
                         len(battery.questions), cfg.model, mode, cfg.threads, cfg.maxevents,
                         getattr(info, "sid", ""), search_app, getattr(info, "username", ""))

    def _api_key(self, cfg):
        dev_key = os.environ.get("JEV_API_KEY")  # development override for tests; never set inside Splunk
        if dev_key:
            return dev_key
        return key_from_splunk(self._metadata.searchinfo, cfg.realm, cfg.username, app=OWN_APP)

    # -- streaming -----------------------------------------------------------------
    def stream(self, records):
        if self._fatal or self.evaluator is None:
            self._say_once("error", "jev: " + (self._fatal or "command not initialised"))
            for record in records:
                record[self._error_field] = "setup_failed"
                yield record
            return
        writer = getattr(self, "_record_writer", None)
        if writer is not None and hasattr(writer, "custom_fields"):
            writer.custom_fields.update(self.evaluator.schema)
        started = time.time()
        out = self.evaluator.process(records)
        for warning in self.evaluator.warnings:
            self._say_once("warn", warning)
        stats = self.evaluator.stats
        if out:
            self.logger.info("chunk records=%d requests=%d fresh=%d kv_hits=%d memo_hits=%d no_state=%d "
                             "budget_exceeded=%d errors=%s input_tokens=%d elapsed_s=%.1f",
                             len(out), stats.requests, stats.fresh, stats.kv_hits, stats.memo_hits,
                             stats.skipped_no_state, stats.budget_exceeded, dict(stats.errors), stats.input_tokens,
                             time.time() - started)
        for record in out:
            yield record
        if getattr(self, "_finished", False):
            self._summary()

    def _summary(self):
        evaluator = self.evaluator
        stats = evaluator.stats
        info = self._metadata.searchinfo
        label = evaluator.alias or ("battery " + evaluator.battery.id)
        reused = stats.kv_hits + stats.memo_hits
        usd = stats.input_tokens * PRICE_PER_TOKEN
        self.logger.info("summary sid=%s app=%s user=%s form=%s label=%s cache=%s kv=%s records=%d answers=%d "
                         "kv_hits=%d memo_hits=%d fresh=%d requests=%d input_tokens=%d est_usd=%.6f est_tokens=%d "
                         "errors=\"%s\" elapsed_s=%.1f",
                         getattr(info, "sid", ""), getattr(info, "app", ""), getattr(info, "username", ""),
                         "primitive" if evaluator.alias else "battery", label, evaluator.cache_mode, evaluator.kv_state,
                         stats.records, stats.fresh + reused, stats.kv_hits, stats.memo_hits, stats.fresh,
                         stats.requests, stats.input_tokens, usd, int(stats.est_tokens),
                         ",".join("%s:%d" % item for item in stats.errors.items()), time.time() - self._started)
        if evaluator.dryrun:
            self.write_info("jev %s: dry run, about %d input tokens ($%.5f) would be sent; %d answers already cached"
                            % (label, int(stats.est_tokens), stats.est_tokens * PRICE_PER_TOKEN, reused))
        else:
            self.write_info("jev %s: %d answers, %d from cache, %d judged now (%d input tokens, $%.5f)"
                            % (label, stats.fresh + reused, reused, stats.fresh, stats.input_tokens, usd))

    def _say_once(self, level, message):
        if message in self._emitted:
            return
        self._emitted.add(message)
        if level == "error":
            self.write_error(message)
        else:
            self.write_warning(message)


dispatch(JevCommand, sys.argv, sys.stdin, sys.stdout, __name__)
