from __future__ import annotations

from collections import OrderedDict

from jev_core.state import build_state, canonical_json, coerce, state_hash


MAPPING = OrderedDict([("command", ("full_command", "str")), ("flag", ("dangerous", "bool")),
                       ("n", ("count", "num")), ("args", ("params", "json"))])


def test_coerce_types():
    assert coerce("true", "bool") is True and coerce("0", "bool") is False and coerce("maybe", "bool") is None
    assert coerce("3", "num") == 3 and coerce("2.5", "num") == 2.5 and coerce("x", "num") is None
    assert coerce('{"a": 1}', "json") == {"a": 1} and coerce("not json", "json") == "not json"
    assert coerce(["a", "", "b"], "str") == "a\nb"
    assert coerce("   ", "str") is None and coerce(None, "str") is None


def test_build_state_skips_blank_and_requires():
    record = {"full_command": "ls", "dangerous": "false", "count": "", "params": '{"x": [1, 2]}'}
    state, truncated, error = build_state(record, MAPPING, ["command"], 16000)
    assert error is None and truncated is False
    assert state == {"command": "ls", "flag": False, "args": {"x": [1, 2]}}
    assert build_state({"dangerous": "true"}, MAPPING, ["command"], 16000) == (None, False, "no_state")
    assert build_state({}, MAPPING, [], 16000) == (None, False, "no_state")


def test_truncation_respects_budget():
    record = {"full_command": "x" * 5000, "params": '{"k": "' + "y" * 5000 + '"}'}
    state, truncated, error = build_state(record, MAPPING, ["command"], maxstate=1200)
    assert error is None and truncated is True
    assert len(canonical_json(state)) <= 1200
    assert state["command"].endswith("[truncated]")


def test_hash_is_order_insensitive_and_model_sensitive():
    questions = {"q": {"type": "noul", "instructions": "x"}}
    a = state_hash(OrderedDict([("a", 1), ("b", 2)]), questions, "m1")
    b = state_hash(OrderedDict([("b", 2), ("a", 1)]), questions, "m1")
    assert a == b and len(a) == 64
    assert state_hash({"a": 1, "b": 2}, questions, "m2") != a
