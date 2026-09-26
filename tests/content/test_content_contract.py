"""The shipped SPL follows the rules the app depends on.

- views never name an index (macros do), never use | rest or real-time windows;
- every macro they call exists; every chained search starts with a pipe;
- every view in the nav exists;
- generated files (the health view and nav) are up to date;
- the key endpoint requires use_jev, and no scheduled search calls the API.
"""
from __future__ import annotations

import configparser
import glob
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "jev_for_splunk")


def _views():
    for path in glob.glob(os.path.join(APP, "default", "data", "ui", "views", "*.xml")):
        text = open(path, encoding="utf-8").read()
        if "<![CDATA[" not in text:
            yield path, None
            continue
        yield path, json.loads(text.split("<![CDATA[", 1)[1].rsplit("]]>", 1)[0])


def _view_searches():
    for path, definition in _views():
        if not definition:
            continue
        for key, ds in definition.get("dataSources", {}).items():
            yield path, key, ds


def _macros():
    text = open(os.path.join(APP, "default", "macros.conf"), encoding="utf-8").read()
    return {match.group(1) for match in re.finditer(r"^\[([A-Za-z0-9_]+)(?:\((\d+)\))?\]", text, re.M)}


def test_no_literal_index_rest_or_realtime():
    searches = list(_view_searches())
    assert searches
    for path, key, ds in searches:
        spl = ds["options"]["query"]
        assert not re.search(r"(^|[\s(])index\s*=", spl), (path, key)
        assert not re.search(r"\|\s*rest\b", spl), (path, key)
        params = ds.get("options", {}).get("queryParameters", {})
        assert not str(params.get("earliest", "")).startswith("rt"), (path, key)


def test_every_macro_is_defined():
    known = _macros()
    for path, key, ds in _view_searches():
        for macro in re.findall(r"`([A-Za-z0-9_]+)(?:\([^`]*\))?`", ds["options"]["query"]):
            assert macro in known, (path, key, macro)


def test_chains_start_with_a_pipe_and_extend_a_search():
    for path, key, ds in _view_searches():
        if ds["type"] == "ds.chain":
            assert ds["options"]["query"].lstrip().startswith("|"), (path, key)
            assert "[" not in ds["options"]["query"], (path, key)


def test_nav_views_exist():
    nav = open(os.path.join(APP, "default", "data", "ui", "nav", "default.xml"), encoding="utf-8").read()
    for view in re.findall(r'<view name="([^"]+)"', nav):
        if view == "search":
            continue
        assert os.path.exists(os.path.join(APP, "default", "data", "ui", "views", view + ".xml")), view


def test_generated_files_are_current():
    out = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "gen_dashboards.py"), "--check"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout


def _conf(name):
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(os.path.join(APP, "default", name + ".conf"), encoding="utf-8")
    return parser


def test_the_key_endpoint_stays_behind_use_jev():
    # The endpoint hands out the TypeSafe key. Without the capability, any logged-in user could read it.
    from jev_splunk import KEY_ENDPOINT

    stanza = _conf("restmap")["script:jev_key"]
    assert stanza["capability"] == "use_jev" and stanza["requireAuthentication"] == "true"
    assert stanza["passSystemAuth"] == "true" and stanza["match"] == "/" + KEY_ENDPOINT
    authorize = _conf("authorize")
    assert authorize.has_section("capability::use_jev") and authorize["role_jev_user"]["use_jev"] == "enabled"
    meta = open(os.path.join(APP, "metadata", "default.meta"), encoding="utf-8").read()
    cache_acl = re.search(r"^\[collections/jev_cache\]\naccess = (.*)$", meta, re.M).group(1)
    assert cache_acl == "read : [ admin, sc_admin, jev_user ], write : [ admin, sc_admin, jev_user ]"


def test_scheduled_searches_never_call_the_api():
    # Everything that calls TypeSafe ships disabled; the only scheduled search is the cache purge.
    for name in _conf("savedsearches").sections():
        search = _conf("savedsearches")[name]
        if search.get("enableSched", "0") == "1" and search.get("disabled", "0") != "1":
            assert not re.search(r"\|\s*jev\b", search["search"]), name   # jevpurge and jevtest do not match
