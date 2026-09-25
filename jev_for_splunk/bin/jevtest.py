#!/usr/bin/env python
"""| jevtest [live=true] - self-test for the Jev for Splunk app.

Yields one row per check: check, status (ok | warn | fail), detail.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(BIN_DIR)
for _path in (os.path.join(BIN_DIR, "lib"), BIN_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import splunklib  # noqa: E402
from splunklib.searchcommands import Configuration, GeneratingCommand, Option, dispatch, validators  # noqa: E402

from jev_core import __version__  # noqa: E402
from jev_core.battery import BatteryError, load_battery  # noqa: E402
from jev_core.cache import make_doc  # noqa: E402
from jev_core.client import JevClient, JevError  # noqa: E402
from jev_core.config import load_config  # noqa: E402
from jev_core.options import FileLookupResolver  # noqa: E402
from jev_core.secrets import fingerprint  # noqa: E402
from jev_splunk import KVStoreCache, key_from_splunk, service_for  # noqa: E402

OWN_APP = os.path.basename(APP_ROOT)


@Configuration(type="events")
class JevTestCommand(GeneratingCommand):
    live = Option(require=False, validate=validators.Boolean())

    def generate(self):
        now = time.time()

        def row(check, status, detail):
            detail = str(detail)
            return {"_time": now, "_raw": "check=%s status=%s detail=%s" % (check, status, detail),
                    "check": check, "status": status, "detail": detail}

        info = self._metadata.searchinfo
        yield row("app", "ok", "%s %s at %s" % (OWN_APP, __version__, APP_ROOT))
        yield row("python", "ok", "%s (%s)" % (platform.python_version(), sys.executable))
        yield row("splunklib", "ok", getattr(splunklib, "__version__", "unknown"))

        cfg = load_config(APP_ROOT)
        yield row("config", "ok", "endpoint=%s model=%s threads=%s maxevents=%s rps=%s cache=%s ttl_days=%s sources=%s" % (
            cfg.endpoint, cfg.model, cfg.threads, cfg.maxevents, cfg.requests_per_second, cfg.cache_mode,
            cfg.cache_ttl_days, ",".join(os.path.relpath(p, APP_ROOT) for p in cfg.sources) or "built-in defaults"))

        apps_dir = os.environ.get("JEV_APPS_DIR") or os.path.dirname(APP_ROOT)
        scanner = FileLookupResolver(apps_dir, search_app=getattr(info, "app", None), own_app=OWN_APP)
        batteries = scanner.list_batteries()
        if not batteries:
            yield row("batteries", "ok", "no battery files installed (the noul/choice/score form needs none)")
        for ref in batteries:
            app, battery_id = ref.split(":", 1)
            try:
                battery = load_battery(FileLookupResolver(apps_dir, search_app=app, own_app=OWN_APP), ref)
                yield row("battery:" + ref, "ok", "v%s, %d questions, state keys: %s" % (
                    battery.version, len(battery.questions), ", ".join(battery.state.keys())))
            except (BatteryError, ValueError) as exc:
                yield row("battery:" + ref, "fail", exc)

        api_key = os.environ.get("JEV_API_KEY")
        if not api_key:
            try:
                api_key = key_from_splunk(info, cfg.realm, cfg.username, app=OWN_APP)
                yield row("api_key", "ok", "stored in storage/passwords (realm=%s user=%s fingerprint=%s)" % (
                    cfg.realm, cfg.username, fingerprint(api_key)))
            except JevError as exc:
                yield row("api_key", "fail", exc.message)
            except Exception as exc:  # no splunkd (tests)
                yield row("api_key", "fail", "could not reach splunkd: %s" % exc)
        else:
            yield row("api_key", "warn", "using JEV_API_KEY from the environment (development mode)")

        if api_key:
            endpoint = os.environ.get("JEV_ENDPOINT") or cfg.endpoint
            try:
                client = JevClient(api_key, endpoint=endpoint, model=cfg.model, timeout=cfg.timeout, max_retries=1,
                                   requests_per_second=0, proxy_url=cfg.proxy_url, ca_bundle=cfg.ca_bundle)
                started = time.time()
                models = client.list_models()
                names = [m.get("name") if isinstance(m, dict) else str(m) for m in models]
                yield row("api_models", "ok", "%s reachable in %.0f ms; models: %s" % (
                    endpoint, (time.time() - started) * 1000, ", ".join(names) or "(none listed)"))
                if self.live:
                    result = client.judge({"text": "All 42 tests passed after the fix."},
                                          {"reports_success": {"type": "noul",
                                                               "instructions": "Does `text` report that something succeeded?"}})
                    yield row("api_live", "ok", "model=%s reports_success=%.2f input_tokens=%d latency_ms=%.0f" % (
                        result.model, result.answers["reports_success"].get("noul", -1), result.input_tokens, result.latency_ms))
            except JevError as exc:
                yield row("api_models", "fail", "%s: %s" % (exc.code, exc.message))

        # KV store: write, read back and delete one probe document
        try:
            service = service_for(info, OWN_APP)
            cache = KVStoreCache(service, collection=cfg.cache_collection, app=OWN_APP)
            probe_key = "jevtest-probe"
            probe = make_doc(probe_key, "probe", {"type": "noul", "instructions": "probe"}, {"type": "noul", "noul": 1.0},
                             "probe", 0, 0, 1, 0)
            cache.put_many([probe])
            found = cache.get_many([probe_key])
            service.delete(cache.path + probe_key, owner="nobody", app=OWN_APP)
            if probe_key in found:
                yield row("kvstore", "ok", "collection %s readable and writable" % cfg.cache_collection)
            else:
                yield row("kvstore", "warn", "wrote a probe to %s but could not read it back" % cfg.cache_collection)
        except Exception as exc:
            yield row("kvstore", "warn", "judgment cache not usable (%s); | jev still works but re-judges repeats" % exc)

        # Which app answers `| jev`? Another app that also defines it (e.g. jev_agent_evals) would win silently.
        try:
            service = service_for(info, "search")
            response = service.get("configs/conf-commands/jev", owner="nobody", app="search", output_mode="json")
            entries = json.loads(response.body.read().decode("utf-8")).get("entry", [])
            owner_app = ((entries[0].get("acl") or {}).get("app") if entries else "") or "unknown"
            status = "ok" if owner_app == OWN_APP else "fail"
            yield row("command_owner", status, "| jev resolves to app %s" % owner_app)
        except Exception as exc:
            yield row("command_owner", "warn", "could not check which app defines | jev: %s" % exc)


dispatch(JevTestCommand, sys.argv, sys.stdin, sys.stdout, __name__)
