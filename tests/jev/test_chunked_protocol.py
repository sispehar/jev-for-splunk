"""End-to-end: drive bin/jev.py and bin/jevtest.py over the chunked protocol against the fake Jev server."""
from __future__ import annotations

import json
import os
import time

import pytest

from chunked_driver import all_messages, all_records, run_command
from fake_jev_server import Handler, serve
from jev_core.cache import SqliteCache, make_doc

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
JEV = os.path.join(REPO, "jev_for_splunk", "bin", "jev.py")
JEVTEST = os.path.join(REPO, "jev_for_splunk", "bin", "jevtest.py")
JEVPURGE = os.path.join(REPO, "jev_for_splunk", "bin", "jevpurge.py")
APPS = os.path.join(HERE, "fixtures", "apps")
APP = "jev_test_app"

OVERCHARGED = "The customer says they were charged more than they should have been"


@pytest.fixture(scope="module")
def fake_api():
    server, port = serve()
    yield "http://127.0.0.1:%d/v1" % port
    server.shutdown()


@pytest.fixture
def env(fake_api, tmp_path):
    Handler.log.clear()
    return {"JEV_API_KEY": "fake-key", "JEV_ENDPOINT": fake_api, "JEV_APPS_DIR": APPS,
            "JEV_CACHE_PATH": str(tmp_path / "jev_cache.sqlite")}


def run(args, records=None, env=None, **kwargs):
    kwargs.setdefault("app", APP)
    return run_command(JEV, args, records, env=env, **kwargs)


def _cmd_records():
    return [
        {"_time": "1758460000", "id": "t1", "full_command": "rm -rf ~/.ssh", "description": "list files",
         "dangerouslyDisableSandbox": "true", "keep": "yes"},
        {"_time": "1758460001", "id": "t2", "full_command": "ls -la", "description": "list files", "keep": "yes"},
        {"_time": "1758460002", "id": "t3", "full_command": "", "description": "", "keep": "yes"},
        {"_time": "1758460003", "id": "t4", "full_command": "rm -rf ~/.ssh", "description": "list files",
         "dangerouslyDisableSandbox": "true", "keep": "yes"},
    ]


def _messages():
    return [
        {"message_id": "m1", "message": "My code SUMMIT20 showed 20% off but I paid the full price"},
        {"message_id": "m2", "message": "Where is my parcel? It is three days late."},
        {"message_id": "m3", "message": "My code SUMMIT20 showed 20% off but I paid the full price"},
        {"message_id": "m4", "message": ""},
    ]


# -- battery form ------------------------------------------------------------------------

def test_battery_fields_and_one_request_per_state(env):
    getinfo, chunks, stderr, _ = run(["battery=cmd_risk", "probs=true", "cache=memo"], _cmd_records(), env=env)
    # distributed=False makes the SDK report the command as "stateful": it runs on the search head only
    assert getinfo.get("type") == "stateful", (getinfo, stderr)
    # the state fields are named in arguments Splunk cannot parse, so ask for every field
    assert getinfo.get("required_fields") == ["*"], getinfo
    out = all_records(chunks)
    assert len(out) == 4 and all(r["keep"] == "yes" for r in out), stderr
    first, second, empty, dup = out
    schema_keys = [k for k in first if k.startswith("jev_")]
    assert {"jev_risk", "jev_destructive", "jev_scope", "jev_scope_p_home_directory", "jev_description_mismatch"} <= set(schema_keys)
    assert all(set(k for k in r if k.startswith("jev_")) == set(schema_keys) for r in out)
    assert first["jev_error"] == "" and 0.0 <= float(first["jev_destructive"]) <= 1.0
    assert first["jev_model"] == "jev-fake-1.0" and first["jev_battery"] == "cmd_risk" and first["jev_battery_version"] == "3"
    assert empty["jev_error"] == "no_state" and empty["jev_destructive"] == ""
    assert dup["jev_cached"] == "1" and first["jev_cached"] == "0" and dup["jev_state_hash"] == first["jev_state_hash"]
    # two distinct states -> two API calls, each carrying all of the battery's questions
    assert len(Handler.log) == 2
    sent = [b for b in Handler.log if b["state"].get("command") == "rm -rf ~/.ssh"][0]
    assert sent["state"] == {"command": "rm -rf ~/.ssh", "agent_description": "list files", "sandbox_disabled": True}
    assert set(sent["questions"]) == {"risk", "destructive", "scope", "description_mismatch"}
    assert sent["questions"]["risk"]["instructions"].startswith("The state describes one shell command")
    # a text-form question is built by the primitive builder and never gets the battery context
    assert sent["questions"]["scope"]["instructions"] == "Regarding `command`: Which part of the system does this command touch?"
    assert list(sent["questions"]["scope"]["criteria"]) == ["project", "home_directory", "system", "unclear"]
    assert sent["questions"]["scope"]["criteria"]["unclear"] is None


def test_inline_questions_and_state_mapping(env):
    questions = json.dumps({"urgent": {"type": "noul", "instructions": "Does `text` convey urgency?"}})
    records = [{"_raw": "Help, payouts failing for 3 days!"}, {"_raw": "All good, thanks."}]
    _, chunks, stderr, _ = run(["state=text=_raw", "questions=" + questions, "prefix=j_"], records, env=env)
    out = all_records(chunks)
    assert len(out) == 2, stderr
    assert out[0]["j_battery"] == "adhoc" and out[0]["j_error"] == "" and float(out[0]["j_urgent"]) >= 0.0
    assert {"text": "Help, payouts failing for 3 days!"} in [b["state"] for b in Handler.log]


def test_maxevents_warning_and_passthrough(env):
    records = [{"full_command": "a"}, {"full_command": "b"}, {"full_command": "c"}]
    _, chunks, stderr, _ = run(["battery=cmd_risk", "maxevents=1", "cache=memo"], records, env=env)
    assert [r["jev_error"] for r in all_records(chunks)] == ["", "maxevents_exceeded", "maxevents_exceeded"]
    assert any(level == "WARN" and "maxevents" in text for level, text in all_messages(chunks)), (chunks, stderr)


def test_reports_bad_battery(env):
    _, chunks, stderr, _ = run(["battery=does_not_exist"], [{"full_command": "a"}], env=env)
    out = all_records(chunks)
    assert out and out[0]["jev_error"] == "setup_failed"
    assert any(level == "ERROR" and "unknown battery" in text for level, text in all_messages(chunks)), (chunks, stderr)


def test_retries_after_429(env):
    questions = json.dumps({"q": {"type": "noul", "instructions": "x"}})
    # the fake server fails once with 429 (Retry-After: 1) when the state text carries __status_429__
    _, chunks, stderr, _ = run(["state=command=full_command", "questions=" + questions, "cache=memo"],
                               [{"full_command": "retry me __status_429__"}], env=env)
    out = all_records(chunks)
    assert out[0]["jev_error"] == "" and out[0]["jev_attempts"] == "2", (out, stderr)


# -- primitive form -------------------------------------------------------------------------

def test_primitive_noul_tolerates_equals_in_the_question(env):
    text = "Is 2+2=4 stated here? Treat x=y as a formula"
    _, chunks, stderr, _ = run(["noul", "message", text, "as", "stated"], _messages()[:2], env=env,
                               raw_args=["noul", "message", '"%s"' % text, "as", "stated"])
    out = all_records(chunks)
    assert len(out) == 2 and not [m for m in all_messages(chunks) if m[0] == "ERROR"], (all_messages(chunks), stderr)
    assert {"stated", "stated_error", "stated_cached", "stated_tokens", "stated_model"} <= set(out[0])
    assert not [k for k in out[0] if k.startswith("jev_")]
    assert out[0]["stated_error"] == "" and 0.0 <= float(out[0]["stated"]) <= 1.0
    sent = Handler.log[0]
    assert sent["questions"] == {"stated": {"type": "noul", "instructions": "Regarding `message`: " + text}}


def test_primitive_choice_and_score(env):
    _, chunks, stderr, _ = run(["choice", "message", "Which part of the shop is this message about?",
                                "options=lookup:areas", "probs=true", "as", "area"], _messages(), env=env)
    out = all_records(chunks)
    assert out[0]["area"] in ("discounts_pricing", "delivery", "other"), stderr
    assert "area_confidence" in out[0] and "area_p_discounts_pricing" in out[0]
    assert out[3]["area_error"] == "no_state" and out[3]["area"] == ""
    _, chunks, stderr, _ = run(["score", "message", "How upset is the customer?", "levels=Calm question; Mildly annoyed; Angry",
                                "as", "upset"], _messages(), env=env)
    out = all_records(chunks)
    assert out[0]["upset_label"] in ("Calm question", "Mildly annoyed", "Angry") and out[0]["upset_level"] in ("0", "1", "2")
    _, chunks, _, _ = run(["score", "message", "How upset is the customer?", "levels=lookup:moods", "as", "mood"],
                          _messages()[:1], env=env)
    assert all_records(chunks)[0]["mood_label"] in ("Calm question", "Mildly annoyed", "Angry")


def test_primitive_argument_errors(env):
    cases = [
        (["choice", "message", "Which part?", "as", "area"], "options="),
        (["noul", "message", "Is", "it", "true?"], "double quotes"),
        (["noul", "message", "Is it?", "optoins=x"], "unknown option 'optoins'"),
        (["noul", "message", "Is it?", "battery=cmd_risk"], "cannot be combined"),
        (["noul", "message", "Is it?", "as", "message"], "overwrite"),
    ]
    for args, expected in cases:
        _, chunks, stderr, _ = run(args, _messages()[:1], env=env)
        errors = [text for level, text in all_messages(chunks) if level == "ERROR"]
        assert errors and expected in errors[0], (args, errors, stderr)
        record = all_records(chunks)[0]
        assert any(k.endswith("_error") and v == "setup_failed" for k, v in record.items()), record


def test_chained_primitives_do_not_collide(env):
    _, chunks, _, _ = run(["noul", "message", OVERCHARGED, "as", "overcharged"], _messages(), env=env)
    first = all_records(chunks)
    _, chunks, stderr, _ = run(["choice", "message", "Which part of the shop is this message about?",
                                "options=lookup:areas", "as", "area"], first, env=env)
    second = all_records(chunks)
    assert second[0]["overcharged"] == first[0]["overcharged"] and second[0]["overcharged_error"] == ""
    assert second[0]["area_error"] == "" and second[0]["area"], stderr


# -- cache ------------------------------------------------------------------------------

def test_multi_chunk_dedupes_across_chunks_and_summarises_once(env):
    rows = _messages()[:3]
    batches = [rows[:2], rows[2:] + rows[:1], rows[1:2]]
    _, chunks, stderr, _ = run(["noul", "message", OVERCHARGED, "as", "oc", "cache=memo"], env=env, chunks=batches)
    assert len(chunks) == 3, stderr
    assert len(Handler.log) == 2                          # two distinct messages across three chunks
    out = all_records(chunks)
    assert [r["oc_cached"] for r in out] == ["0", "0", "1", "1", "1"]
    schemas = [set(rows[0].keys()) for _, rows in chunks if rows]
    assert all(s == schemas[0] for s in schemas)
    infos = [(i, text) for i, (meta, _) in enumerate(chunks) for level, text in
             [(m[0], m[1]) for m in (meta.get("inspector") or {}).get("messages", [])] if level == "INFO"]
    assert infos and all(i == 2 for i, _ in infos) and "judged now" in infos[-1][1], infos


def test_battery_warms_the_cache_for_primitives(env):
    # warm-up: one request per distinct message carrying all three text-form questions
    _, chunks, stderr, _ = run(["battery=msg_battery"], _messages(), env=env)
    assert len(Handler.log) == 2, stderr
    assert set(Handler.log[0]["questions"]) == {"overcharged", "area", "upset"}
    before = len(Handler.log)
    # the dashboard form: three readable primitives with the same wording -> zero requests
    records = _messages()
    for args in (["noul", "message", OVERCHARGED, "as", "overcharged"],
                 ["choice", "message", "Which part of the shop is this message about?", "options=lookup:areas", "as", "area"],
                 ["score", "message", "How upset is the customer?", "levels=Calm; Annoyed; Angry", "as", "upset"]):
        _, chunks, stderr, _ = run(args, records, env=env)
        records = all_records(chunks)
        alias = args[-1]
        assert [r[alias + "_cached"] for r in records[:3]] == ["1", "1", "1"], (alias, records, stderr)
    assert len(Handler.log) == before
    assert records[0]["overcharged"] != "" and records[0]["area"] != "" and records[0]["upset_label"] != ""


def test_cache_off_sends_duplicates(env):
    rows = _messages()[:1] * 3
    run(["noul", "message", OVERCHARGED, "as", "oc", "cache=off"], rows, env=env)
    assert len(Handler.log) == 3


def test_dryrun_sends_nothing(env):
    _, chunks, stderr, _ = run(["battery=msg_battery", "dryrun=true"], _messages(), env=env)
    out = all_records(chunks)
    assert len(Handler.log) == 0, stderr
    assert out[0]["jev_error"] == "dryrun" and float(out[0]["jev_input_tokens"]) > 0
    assert any(level == "INFO" and "dry run" in text for level, text in all_messages(chunks))


def test_dryrun_needs_no_key(env):
    # no JEV_API_KEY and no splunkd to read one from: a dry run still estimates, a real run cannot
    env = {k: v for k, v in env.items() if k != "JEV_API_KEY"}
    _, chunks, stderr, _ = run(["noul", "message", OVERCHARGED, "as", "oc", "dryrun=true"], _messages(), env=env)
    out = all_records(chunks)
    assert out[0]["oc_error"] == "dryrun" and float(out[0]["oc_tokens"]) > 0, (all_messages(chunks), stderr)
    assert not [text for level, text in all_messages(chunks) if level == "ERROR"] and len(Handler.log) == 0
    _, chunks, _, _ = run(["noul", "message", OVERCHARGED, "as", "oc"], _messages()[:1], env=env)
    assert all_records(chunks)[0]["oc_error"] == "setup_failed"


def test_summary_names_the_fields_that_left_splunk(env, tmp_path):
    (tmp_path / "var" / "log" / "splunk").mkdir(parents=True)
    env = dict(env, SPLUNK_HOME=str(tmp_path))
    run(["battery=cmd_risk", "cache=memo"], _cmd_records()[:1], env=env)
    summary = [line for line in (tmp_path / "var" / "log" / "splunk" / "jev.log").read_text().splitlines()
               if " summary " in line]
    assert summary and 'state_fields="full_command,description,dangerouslyDisableSandbox"' in summary[-1], summary
    assert "requests=1" in summary[-1] and 'label="battery cmd_risk"' in summary[-1]


# -- purge ------------------------------------------------------------------------------

def test_jevpurge_deletes_only_expired_answers(env):
    cache = SqliteCache(env["JEV_CACHE_PATH"])
    wire = {"type": "noul", "instructions": "x"}
    now = time.time()
    cache.put_many([make_doc(key, "m", wire, {"type": "noul", "noul": 0.5}, "m", 1, 1, 1, 1, created=now - age * 86400)
                    for key, age in (("old", 40), ("new", 1))])
    getinfo, chunks, stderr, _ = run_command(JEVPURGE, ["days=30"], [], env=env, app=APP)
    assert getinfo.get("generating") is True, (getinfo, stderr)
    row = all_records(chunks)[0]
    assert row["status"] == "ok" and row["deleted"] == "1" and row["days"] == "30", (row, stderr)
    assert list(cache.get_many(["old", "new"])) == ["new"]
    getinfo, _, _, _ = run_command(JEVPURGE, ["days=0"], [], env=env, app=APP)   # refused before any chunk
    assert ["ERROR", "Illegal value: days=0"] in getinfo["inspector"]["messages"], getinfo


# -- self test ------------------------------------------------------------------------------

def test_jevtest_generates_rows(env):
    getinfo, chunks, stderr, _ = run_command(JEVTEST, ["live=true"], [], env=env, app=APP)
    assert getinfo.get("type") == "events" and getinfo.get("generating") is True, (getinfo, stderr)
    checks = {r["check"]: r for r in all_records(chunks)}
    assert checks["python"]["status"] == "ok"
    assert checks["battery:jev_test_app:cmd_risk"]["status"] == "ok"
    assert checks["battery:jev_test_app:msg_battery"]["status"] == "ok"
    assert checks["api_key"]["status"] == "warn"           # env override in development mode
    assert checks["api_models"]["status"] == "ok" and "jev-fake-1.0" in checks["api_models"]["detail"]
    assert checks["api_live"]["status"] == "ok" and "model=jev-fake-1.0" in checks["api_live"]["detail"]
    assert checks["kvstore"]["status"] == "warn"           # no splunkd in the test
    assert checks["command_owner"]["status"] == "warn"
