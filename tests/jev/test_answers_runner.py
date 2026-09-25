from __future__ import annotations

import pytest

from jevkit import http_error, ok_response, sample_answers
from jev_core.answers import output_schema
from jev_core.battery import load_battery
from jev_core.runner import Evaluator


def test_output_schema_shape(app_root):
    battery = load_battery(app_root, "test_battery")
    schema = output_schema(battery, "jev_", probs=False)
    assert schema[:8] == ["jev_destructive", "jev_scope", "jev_scope_confidence", "jev_risk", "jev_risk_level",
                          "jev_risk_label", "jev_risk_confidence", "jev_mismatch"]
    assert schema[-11:] == ["jev_model", "jev_input_tokens", "jev_output_tokens", "jev_latency_ms", "jev_error", "jev_battery",
                            "jev_battery_version", "jev_state_hash", "jev_truncated", "jev_cached", "jev_attempts"]
    with_probs = output_schema(battery, "x_", probs=True)
    assert "x_scope_p_project" in with_probs and "x_scope_p_unclear" in with_probs and "x_risk_p_2" in with_probs


def _records():
    return [
        {"full_command": "rm -rf /data", "description": "list files", "dangerous": "true", "other": "kept"},
        {"full_command": "rm -rf /data", "description": "list files", "dangerous": "true"},   # duplicate state -> memo
        {"full_command": "ls -la"},                                                          # no note -> mismatch skipped
        {"description": "no command"},                                                       # no_state
    ]


def test_evaluator_fills_fixed_schema_and_memoises(app_root, make_client, fake_http):
    battery = load_battery(app_root, "test_battery")
    fake_http.script.extend([ok_response(sample_answers(), input_tokens=500), ok_response(sample_answers(), input_tokens=200)])
    evaluator = Evaluator(make_client(), battery, threads=2, maxevents=100, prefix="jev_", probs=False)
    out = evaluator.process(_records())
    assert len(fake_http.requests) == 2                       # 3 judgeable records, 2 distinct states
    keys = [set(k for k in r.keys() if k.startswith("jev_")) for r in out]
    assert all(k == set(evaluator.schema) for k in keys)      # identical field set on every record
    first, dup, ls, empty = out
    assert first["jev_destructive"] == "0.9312" and first["jev_scope"] == "system" and first["jev_scope_confidence"] == "0.7800"
    assert first["jev_risk"] == "1.9000" and first["jev_risk_level"] == "2" and first["jev_risk_label"] == "high"
    assert first["jev_cached"] == "0" and dup["jev_cached"] == "1" and dup["jev_state_hash"] == first["jev_state_hash"]
    assert first["jev_input_tokens"] == "500" and first["jev_battery"] == "test_battery" and first["jev_battery_version"] == "2"
    assert first["other"] == "kept"
    assert empty["jev_error"] == "no_state" and empty["jev_destructive"] == "" and empty["jev_battery"] == "test_battery"
    # the ls record had no note, so the mismatch question was not sent
    sent_for_ls = [r for r in fake_http.requests if r["body"]["state"].get("command") == "ls -la"][0]
    assert "mismatch" not in sent_for_ls["body"]["questions"] and "destructive" in sent_for_ls["body"]["questions"]
    assert sent_for_ls["body"]["state"] == {"command": "ls -la"}
    stats = evaluator.stats.summary()
    assert stats["requests"] == 2 and stats["evaluated"] == 3 and stats["cached"] == 1 and stats["no_state"] == 1
    assert stats["input_tokens"] == 700


def test_maxevents_budget(app_root, make_client, fake_http):
    battery = load_battery(app_root, "test_battery")
    fake_http.script.append(ok_response(sample_answers()))
    evaluator = Evaluator(make_client(), battery, threads=1, maxevents=1)
    out = evaluator.process([{"full_command": "a"}, {"full_command": "b"}, {"full_command": "a"}])
    assert [r["jev_error"] for r in out] == ["", "maxevents_exceeded", ""]
    assert out[2]["jev_cached"] == "1"
    assert any("maxevents" in w for w in evaluator.warnings)
    # the budget persists across chunks
    out2 = evaluator.process([{"full_command": "c"}])
    assert out2[0]["jev_error"] == "maxevents_exceeded" and len(fake_http.requests) == 1


def test_auth_failure_stops_further_calls(app_root, make_client, fake_http):
    battery = load_battery(app_root, "test_battery")
    fake_http.script.append(http_error(401, {"error": "nope"}))
    evaluator = Evaluator(make_client(), battery, threads=1)
    out = evaluator.process([{"full_command": "a"}])
    assert out[0]["jev_error"] == "auth_failed"
    out2 = evaluator.process([{"full_command": "b"}, {"full_command": "c"}])
    assert [r["jev_error"] for r in out2] == ["auth_failed", "auth_failed"] and len(fake_http.requests) == 1
    assert any("rejected the key" in w for w in evaluator.warnings)


def test_per_record_errors_do_not_poison_others(app_root, make_client, fake_http):
    battery = load_battery(app_root, "test_battery")
    fake_http.script.extend([http_error(422, {"detail": "bad"}), ok_response(sample_answers())])
    evaluator = Evaluator(make_client(), battery, threads=1)
    out = evaluator.process([{"full_command": "a"}, {"full_command": "b"}])
    codes = sorted(r["jev_error"] for r in out)
    assert codes == ["", "validation:bad"]


def test_keepstate_and_probs(app_root, make_client, fake_http):
    battery = load_battery(app_root, "test_battery")
    fake_http.script.append(ok_response(sample_answers()))
    evaluator = Evaluator(make_client(), battery, threads=1, probs=True, keepstate=True)
    out = evaluator.process([{"full_command": "a", "params": '{"k": 1}'}])
    assert out[0]["jev_scope_p_system"] == "0.8500" and out[0]["jev_risk_p_2"] == "0.8500"
    assert out[0]["jev_state"] == '{"command": "a", "args": {"k": 1}}'
