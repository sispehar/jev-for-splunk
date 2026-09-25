from __future__ import annotations

import importlib.util
import os
import sys
from collections import OrderedDict

import pytest

from jev_core.primitive import (PRIMITIVE_TEMPLATE, PrimitiveError, about_phrase, normalize_text, parse_inline_levels,
                                parse_inline_options, parse_positionals, primitive_question)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_template_is_frozen():
    # part of every cache key: changing it silently re-judges every event
    assert PRIMITIVE_TEMPLATE == "Regarding {about}: {text}"


@pytest.mark.parametrize("tokens,mode,fields,text,alias", [
    (["noul", "message", "Is it late?", "as", "late"], "noul", "message", "Is it late?", "late"),
    (["NOUL", "message", "Is it late?", "AS", "Late_1"], "noul", "message", "Is it late?", "Late_1"),
    (["choice", "Is it late?"], "choice", "text=_raw", "Is it late?", "jev_choice"),
    (["score", "msg=message,subject", "How upset?"], "score", "msg=message,subject", "How upset?", "jev_score"),
])
def test_parse_positionals(tokens, mode, fields, text, alias):
    spec = parse_positionals(tokens)
    assert (spec.mode, spec.fields, spec.text, spec.alias) == (mode, fields, text, alias)


@pytest.mark.parametrize("tokens,message", [
    ([], "expected noul"),
    (["guess", "message", "x"], "unknown question type"),
    (["noul"], "needs a question"),
    (["noul", "message", "Is", "it", "late?"], "double quotes"),
    (["noul", "message", "Is it?", "optoins=x"], "unknown option 'optoins'"),
    (["noul", "message", "Is it?", "as", "1bad"], "alias"),
])
def test_parse_positionals_errors(tokens, message):
    with pytest.raises(PrimitiveError) as info:
        parse_positionals(tokens)
    assert message in str(info.value)


def test_about_phrase_and_whitespace():
    assert about_phrase(["message"]) == "`message`"
    assert about_phrase(["a", "b"]) == "`a` and `b`"
    assert about_phrase(["a", "b", "c"]) == "`a`, `b` and `c`"
    assert normalize_text("  The  customer\n   says\tso ") == "The customer says so"


def test_question_builder_shapes():
    noul = primitive_question("noul", ["message"], "The customer  says\n so")
    assert noul == OrderedDict([("type", "noul"), ("instructions", "Regarding `message`: The customer says so")])
    noul_c = primitive_question("noul", ["message"], "Late?", criteria_true="Says it is late", criteria_false="Does not")
    assert noul_c["criteria"] == {"true": "Says it is late", "false": "Does not"}
    choice = primitive_question("choice", ["message"], "Which area?", options=OrderedDict([("a", "A things"), ("b", None)]))
    assert list(choice["criteria"].items()) == [("a", "A things"), ("b", None)]
    score = primitive_question("score", ["a", "b"], "How upset?", levels=["calm", "angry"])
    assert score["instructions"] == "Regarding `a` and `b`: How upset?" and score["criteria"] == ["calm", "angry"]


@pytest.mark.parametrize("kwargs,message", [
    (dict(mode="choice", keys=["m"], text="x"), "options="),
    (dict(mode="score", keys=["m"], text="x"), "levels="),
    (dict(mode="noul", keys=["m"], text="x", options={"a": None, "b": None}), "noul takes"),
    (dict(mode="noul", keys=["m"], text="use `macro`"), "backticks"),
    (dict(mode="noul", keys=["m"], text="   "), "empty"),
    (dict(mode="noul", keys=[], text="x"), "state key"),
])
def test_question_builder_errors(kwargs, message):
    with pytest.raises(PrimitiveError) as info:
        primitive_question(**kwargs)
    assert message in str(info.value)


def test_inline_options_and_levels():
    options = parse_inline_options("discounts: Promo codes; prices ; delivery:  Late or lost parcels ;")
    assert list(options.items()) == [("discounts", "Promo codes"), ("prices", None), ("delivery", "Late or lost parcels")]
    with pytest.raises(PrimitiveError):
        parse_inline_options("a; a")
    assert parse_inline_levels("Calm; ; Mildly  annoyed ;Angry") == ["Calm", "Mildly annoyed", "Angry"]


# -- the SDK option parser override (imported from bin/jev.py; dispatch only runs under __main__) ------

def _load_jev_module():
    path = os.path.join(REPO, "jev_for_splunk", "bin", "jev.py")
    spec = importlib.util.spec_from_file_location("jev_command_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Info(object):
    def __init__(self, raw_args):
        self.raw_args = raw_args


class _Meta(object):
    def __init__(self, raw_args):
        self.searchinfo = _Info(raw_args)


def test_option_parser_override():
    module = _load_jev_module()
    command = module.JevCommand()
    command._metadata = _Meta(['noul', 'message', '"levels=inside the question"', 'levels=a; b'])
    parse = command._protocol_v2_option_parser
    assert parse("levels=a; b") == ["levels", "a; b"]                       # declared option
    assert parse("options=lookup:shop_areas") == ["options", "lookup:shop_areas"]
    assert parse("levels=inside the question") == ["levels=inside the question"]   # it was quoted: positional
    assert parse("Is 2+2=4 stated?") == ["Is 2+2=4 stated?"]                 # not an identifier before '='
    assert parse("colour=red") == ["colour=red"]                             # undeclared name stays positional
    command._metadata = _Meta(None)                                          # raw_args missing: names still decide
    assert parse("threads=4") == ["threads", "4"]
