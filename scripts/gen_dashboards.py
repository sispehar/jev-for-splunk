#!/usr/bin/env python3
"""Generate the app's health view and nav from one place.

    python3 scripts/gen_dashboards.py          # regenerate
    python3 scripts/gen_dashboards.py --check  # exit 1 if a generated file is out of date
"""
from __future__ import annotations

import os
import sys
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from studio import Dashboard  # noqa: E402

JEV = os.path.join(REPO, "jev_for_splunk")


def jev_health():
    d = Dashboard("jev_health", "Health", "Jev for Splunk: health", "Self-test, usage, cost and cache effect.",
                  time_default="-24h@h,now")
    d.ds("ds_test", "| jevtest | table check status detail", timed=False)
    d.table("table_test", "Self-test (| jevtest; add live=true on the Setup page for a paid round trip)", "ds_test", {"count": 20})
    d.row(["table_test"], 340)
    d.ds("ds_usage", '`jev_internal_log` "summary" | timechart span=1h sum(fresh) AS "judged now" sum(kv_hits) AS '
                     '"from the KV cache" sum(memo_hits) AS "reused in the search"')
    d.viz("chart_usage", "splunk.column", "Answers per hour: fresh vs cached", "ds_usage", {"stackMode": "stacked"})
    d.ds("ds_cost", '`jev_internal_log` "summary" | stats sum(input_tokens) AS tokens BY app user | eval usd=`jev_cost_usd(tokens)` '
                    "| sort - tokens")
    d.table("table_cost", "Tokens and cost by app and user", "ds_cost")
    d.row_weighted([("chart_usage", 3), ("table_cost", 2)], 300)
    d.ds("ds_sent", '`jev_internal_log` "summary" requests>0 | stats sum(requests) AS requests sum(fresh) AS "answers judged" '
                    "latest(_time) AS last BY user app label state_fields | convert ctime(last) | sort - requests")
    d.table("table_sent", "What left Splunk: the fields each search sent to TypeSafe, by user and app", "ds_sent")
    d.row(["table_sent"], 240)
    d.ds("ds_errors", '`jev_internal_log` (level=ERROR OR level=WARNING) | stats count latest(_raw) AS example BY logger '
                      "| sort - count")
    d.table("table_errors", "Warnings and errors in jev.log", "ds_errors")
    d.row(["table_errors"], 240)
    return d


NAV_JEV = """<nav search_view="search" color="#28b6a4">
  <view name="jev_setup" default="true" />
  <view name="jev_health" />
  <view name="search" />
</nav>
"""


def outputs():
    files = OrderedDict()
    files[os.path.join(JEV, "default", "data", "ui", "views", "jev_health.xml")] = jev_health().xml()
    files[os.path.join(JEV, "default", "data", "ui", "nav", "default.xml")] = NAV_JEV
    return files


def main(argv):
    check = "--check" in argv
    stale = []
    for path, content in outputs().items():
        current = open(path, "r", encoding="utf-8").read() if os.path.exists(path) else None
        if current != content:
            stale.append(path)
            if not check:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
    for path in stale:
        print(("stale: " if check else "wrote: ") + os.path.relpath(path, REPO))
    return 1 if (check and stale) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
