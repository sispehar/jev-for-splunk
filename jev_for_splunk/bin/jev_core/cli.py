"""Command line access to the same code the Splunk command runs.

  python -m jev_core.cli models
  python -m jev_core.cli batteries
  python -m jev_core.cli validate
  python -m jev_core.cli judge --battery shop_messages --records messages.ndjson [--cache jev_cache.sqlite]
  python -m jev_core.cli ask noul message "The customer says they were charged more than they should have been" \
      --as overcharged --records messages.ndjson [--cache jev_cache.sqlite]

--apps-dir defaults to the directory that holds this app (the repository root in
a checkout, $SPLUNK_HOME/etc/apps on a Splunk host), so batteries and lookups of
sibling apps resolve exactly as they do inside Splunk. The API key comes from
TYPESAFE_API_KEY or the nearest .env file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict

from .battery import Battery, BatteryError, load_battery, parse_state_mapping, wire_questions
from .cache import SqliteCache
from .client import JevClient, JevError
from .config import load_config
from .options import FileLookupResolver, ResolveError
from .primitive import PrimitiveError, primitive_question
from .runner import Evaluator
from .secrets import key_from_environment
from .state import build_state

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
DEFAULT_APPS_DIR = os.path.dirname(APP_ROOT)


def _client(cfg, args):
    key = key_from_environment(start_dir=os.getcwd())
    if not key:
        sys.exit("no API key: set TYPESAFE_API_KEY or put it in .env")
    return JevClient(key, endpoint=os.environ.get("JEV_ENDPOINT") or cfg.endpoint, model=args.model or cfg.model,
                     timeout=cfg.timeout, max_retries=cfg.max_retries,
                     requests_per_second=args.rps or cfg.requests_per_second, burst=args.threads,
                     proxy_url=cfg.proxy_url, ca_bundle=cfg.ca_bundle)


def _resolver(args):
    return FileLookupResolver(args.apps_dir, search_app=args.app, own_app=os.path.basename(APP_ROOT))


def _config(args):
    extra = [os.path.join(args.apps_dir, args.app)] if args.app else []
    return load_config(APP_ROOT, extra_roots=[e for e in extra if os.path.isdir(e) and os.path.abspath(e) != APP_ROOT])


def _records(path):
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line, object_pairs_hook=OrderedDict))
    return records


def cmd_models(args, cfg):
    client = _client(cfg, args)
    for model in client.list_models():
        if isinstance(model, dict):
            print("%-14s %-12s %s" % (model.get("name"), model.get("release_date", ""), model.get("description", "")))
        else:
            print(model)


def cmd_batteries(args, cfg):
    resolver = _resolver(args)
    for ref in resolver.list_batteries():
        app = ref.split(":", 1)[0]
        try:
            battery = load_battery(FileLookupResolver(args.apps_dir, search_app=app), ref)
            print("%-32s v%-3s %2d questions  %s" % (ref, battery.version, len(battery.questions), battery.description))
        except (BatteryError, ValueError) as exc:
            print("%-32s INVALID: %s" % (ref, exc))


def cmd_validate(args, cfg):
    bad = 0
    refs = _resolver(args).list_batteries()
    for ref in refs:
        app = ref.split(":", 1)[0]
        try:
            load_battery(FileLookupResolver(args.apps_dir, search_app=app), ref)
            print("ok       %s" % ref)
        except (BatteryError, ValueError) as exc:
            bad += 1
            print("INVALID  %s: %s" % (ref, exc))
    if not refs:
        print("no batteries under %s" % args.apps_dir)
    sys.exit(1 if bad else 0)


def _run(args, cfg, battery, alias=None):
    if args.records:
        records = _records(args.records)
    elif getattr(args, "state", None):
        state = json.loads(args.state, object_pairs_hook=OrderedDict)
        record = OrderedDict()
        for key, value in state.items():
            record[battery.state[key][0] if key in battery.state else key] = value if isinstance(value, str) else json.dumps(value)
        records = [record]
    else:
        sys.exit("give --records file.ndjson (or --state '<json>' for judge)")
    client = _client(cfg, args)
    backend = SqliteCache(args.cache) if args.cache else None
    evaluator = Evaluator(client, battery, model=args.model or cfg.model, threads=args.threads,
                          maxevents=args.maxevents, maxstate=cfg.maxstate, prefix=cfg.prefix, probs=args.probs,
                          cache_mode="kv" if backend else "memo", backend=backend, ttl_days=cfg.cache_ttl_days,
                          dryrun=args.dryrun, primitive_alias=alias)
    out = []
    for start in range(0, len(records), 500):
        out.extend(evaluator.process(records[start:start + 500]))
    if args.show_request and records:
        state, _, _ = build_state(records[0], battery.state, battery.required_state, cfg.maxstate)
        print(json.dumps({"state": state, "model": evaluator.model, "questions": wire_questions(battery, state)},
                         indent=2, ensure_ascii=False), file=sys.stderr)
    keep = set(evaluator.schema) | {"_time", "message_id", "change_id"}
    for record in out:
        if args.only_jev:
            record = OrderedDict((k, v) for k, v in record.items() if k in keep)
        print(json.dumps(record, ensure_ascii=False))
    print(json.dumps(evaluator.stats.summary()), file=sys.stderr)
    for warning in evaluator.warnings:
        print("warning: " + warning, file=sys.stderr)


def cmd_judge(args, cfg):
    try:
        battery = load_battery(_resolver(args), args.battery, args.questions, args.state_map)
    except BatteryError as exc:
        sys.exit("battery error: %s" % exc)
    _run(args, cfg, battery)


def cmd_ask(args, cfg):
    resolver = _resolver(args)
    try:
        mapping = parse_state_mapping(args.fields)
        options = resolver.options(args.options) if args.options is not None else None
        levels = resolver.levels(args.levels) if args.levels is not None else None
        wire = primitive_question(args.mode, list(mapping.keys()), args.text, options=options, levels=levels,
                                  criteria_true=args.criteria_true, criteria_false=args.criteria_false)
    except (BatteryError, PrimitiveError, ResolveError) as exc:
        sys.exit("question error: %s" % exc)
    alias = args.alias or "jev_" + args.mode
    battery = Battery(id="primitive", questions={alias: wire}, state=args.fields, required_state=list(mapping.keys()))
    battery.text_form.add(alias)
    _run(args, cfg, battery, alias=alias)


def _common(sub):
    sub.add_argument("--records", help="NDJSON file of Splunk-like records")
    sub.add_argument("--cache", help="sqlite file used as the persistent judgment cache")
    sub.add_argument("--threads", type=int, default=8)
    sub.add_argument("--rps", type=float, default=None, help="requests per second (default from jev.conf)")
    sub.add_argument("--maxevents", type=int, default=5000)
    sub.add_argument("--probs", action="store_true")
    sub.add_argument("--dryrun", action="store_true")
    sub.add_argument("--only-jev", action="store_true", help="print only the answer fields and ids")
    sub.add_argument("--show-request", action="store_true", help="print the first request payload to stderr")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev_core.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apps-dir", default=DEFAULT_APPS_DIR, help="directory holding the Splunk apps")
    parser.add_argument("--app", default=None, help="app context (where lookups and batteries are looked up first)")
    parser.add_argument("--model", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("models", help="list models the key can use")
    sub.add_parser("batteries", help="list batteries in every app")
    sub.add_parser("validate", help="validate every battery file in every app")
    judge = sub.add_parser("judge", help="evaluate records against a battery")
    judge.add_argument("--battery")
    judge.add_argument("--questions", help="inline questions JSON")
    judge.add_argument("--state-map", dest="state_map", help='state mapping, e.g. "message=message"')
    judge.add_argument("--state", help="one state object as JSON (keys = battery state keys)")
    _common(judge)
    ask = sub.add_parser("ask", help="evaluate records against one primitive question")
    ask.add_argument("mode", choices=["noul", "choice", "score"])
    ask.add_argument("fields", help='state fields, e.g. "message" or "msg=message,subject"')
    ask.add_argument("text", help="the question")
    ask.add_argument("--options")
    ask.add_argument("--levels")
    ask.add_argument("--criteria-true", dest="criteria_true")
    ask.add_argument("--criteria-false", dest="criteria_false")
    ask.add_argument("--as", dest="alias")
    _common(ask)
    args = parser.parse_args(argv)
    cfg = _config(args)
    try:
        {"models": cmd_models, "batteries": cmd_batteries, "validate": cmd_validate, "judge": cmd_judge,
         "ask": cmd_ask}[args.command](args, cfg)
    except JevError as exc:
        sys.exit("jev error [%s]: %s" % (exc.code, exc.message))


if __name__ == "__main__":
    main()
