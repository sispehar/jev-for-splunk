from __future__ import annotations

import json
import os
from collections import OrderedDict

import pytest

from jev_core.battery import (BatteryError, list_batteries, load_battery, parse_state_mapping, validate_battery,
                              battery_from_dict, wire_questions)


def test_parse_state_mapping_types_and_defaults():
    mapping = parse_state_mapping("command=full_command, flag=dangerous:bool ,n=count:num,blob=params:json,plain")
    assert list(mapping.items()) == [
        ("command", ("full_command", "str")), ("flag", ("dangerous", "bool")), ("n", ("count", "num")),
        ("blob", ("params", "json")), ("plain", ("plain", "str")),
    ]


@pytest.mark.parametrize("text", ["Bad-Key=field", "k=field:date", "k="])
def test_parse_state_mapping_rejects(text):
    with pytest.raises(BatteryError):
        parse_state_mapping(text)


def test_dotted_field_error_says_how_to_name_it():
    with pytest.raises(BatteryError) as info:
        parse_state_mapping("data.message")
    assert "msg=data.message" in str(info.value)
    assert parse_state_mapping("msg=data.message")["msg"] == ("data.message", "str")


def test_load_battery_from_default_dir(app_root):
    battery = load_battery(app_root, "test_battery")
    assert battery.id == "test_battery" and battery.version == 2
    assert battery.state["flag"] == ("dangerous", "bool")
    assert battery.required_state == ["command"]
    assert list(battery.questions) == ["destructive", "scope", "risk", "mismatch"]


def test_local_overrides_default(app_root):
    local = os.path.join(app_root, "local", "batteries")
    os.makedirs(local)
    data = json.loads(open(os.path.join(app_root, "default", "batteries", "test_battery.json")).read())
    data["version"] = 9
    open(os.path.join(local, "test_battery.json"), "w").write(json.dumps(data))
    assert list_batteries(app_root)["test_battery"].startswith(local)
    assert load_battery(app_root, "test_battery").version == 9


def test_inline_questions_and_state_merge(app_root):
    inline = json.dumps({"extra": {"type": "noul", "instructions": "Extra?"},
                         "risk": {"type": "score", "instructions": "Overridden", "criteria": ["a", "b"]}})
    battery = load_battery(app_root, "test_battery", inline_questions=inline, state_override="command=cmd,more=other")
    assert "extra" in battery.questions
    assert battery.questions["risk"]["criteria"] == ["a", "b"]
    assert battery.state["command"] == ("cmd", "str") and battery.state["more"] == ("other", "str")


def test_adhoc_battery_requires_state_mapping(app_root):
    with pytest.raises(BatteryError):
        load_battery(app_root, None, inline_questions='{"q": {"type": "noul", "instructions": "x"}}')
    battery = load_battery(app_root, None, inline_questions='{"q": {"type": "noul", "instructions": "x"}}', state_override="text=_raw")
    assert battery.id == "adhoc" and battery.state["text"] == ("_raw", "str")


def test_unknown_battery(app_root):
    with pytest.raises(BatteryError):
        load_battery(app_root, "nope")


@pytest.mark.parametrize("question", [
    {"type": "noul"},
    {"type": "guess", "instructions": "x"},
    {"type": "choice", "instructions": "x", "criteria": {"only": None}},
    {"type": "choice", "instructions": "x", "criteria": {str(i): None for i in range(256)}},
    {"type": "score", "instructions": "x", "criteria": ["one"]},
    {"type": "score", "instructions": "x", "criteria": [str(i) for i in range(11)]},
    {"type": "noul", "instructions": "x", "criteria": {"maybe": "?"}},
    {"type": "noul", "instructions": "x", "requires": "note"},
])
def test_validation_rejects_bad_questions(question):
    battery = battery_from_dict({"id": "b", "state": {"t": "_raw"}, "questions": {"q": question}})
    with pytest.raises(BatteryError):
        validate_battery(battery)


def test_validation_rejects_bad_ids():
    with pytest.raises(BatteryError):
        validate_battery(battery_from_dict({"id": "Bad Id", "state": {"t": "_raw"}, "questions": {"q": {"type": "noul", "instructions": "x"}}}))
    with pytest.raises(BatteryError):
        validate_battery(battery_from_dict({"id": "b", "state": {"t": "_raw"}, "questions": {"Q-1": {"type": "noul", "instructions": "x"}}}))
    with pytest.raises(BatteryError):
        validate_battery(battery_from_dict({"id": "b", "state": {"t": "_raw"}, "required_state": ["missing"],
                                            "questions": {"q": {"type": "noul", "instructions": "x"}}}))


def test_wire_questions_prepends_context_and_drops_missing_requires(app_root):
    battery = load_battery(app_root, "test_battery")
    wired = wire_questions(battery, {"command": "ls"})
    assert "mismatch" not in wired
    assert wired["destructive"]["instructions"].startswith("CTX\n\n")
    assert wired["destructive"]["criteria"] == {"true": "yes", "false": "no"}
    assert "requires" not in wired["destructive"]
    wired_all = wire_questions(battery, {"command": "ls", "note": "list"})
    assert set(wired_all) == {"destructive", "scope", "risk", "mismatch"}
    # structured instructions get the context as a leading field
    battery.questions["structured"] = {"type": "noul", "instructions": OrderedDict([("question", "Q?"), ("data", {"k": 1})])}
    wired_struct = wire_questions(battery, {"command": "ls"})["structured"]["instructions"]
    assert list(wired_struct.keys()) == ["context", "question", "data"]
