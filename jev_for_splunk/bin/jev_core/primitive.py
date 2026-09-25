"""The primitive form of the command: one typed question per pipe stage.

    ... | jev noul message "The customer says they were charged more than they should have been" as overcharged
    ... | jev choice message "Which part of the shop is this about?" options=lookup:shop_areas as area
    ... | jev score message "How upset is the customer?" levels=lookup:shop_upset_levels as upset

The question actually sent to the model is built here and nowhere else, so a
battery file can declare the same question in "text form" and share cache
entries with the SPL above (see cache.cache_key).
"""
from __future__ import annotations

import re
from collections import OrderedDict

MODES = ("noul", "choice", "score")
ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Frozen: it is part of every cache key. Changing it re-judges every event.
PRIMITIVE_TEMPLATE = "Regarding {about}: {text}"


class PrimitiveError(ValueError):
    pass


def normalize_text(text):
    """Collapse whitespace so line continuations in .conf files cannot change the question."""
    return " ".join(str(text or "").split())


def about_phrase(keys):
    names = ["`%s`" % key for key in keys]
    if not names:
        raise PrimitiveError("a primitive question needs at least one state key")
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def primitive_question(mode, keys, text, options=None, levels=None, criteria_true=None, criteria_false=None):
    """Return the wire question (type, instructions, criteria) for one primitive."""
    if mode not in MODES:
        raise PrimitiveError("unknown question type %r; use noul, choice or score" % mode)
    body = normalize_text(text)
    if not body:
        raise PrimitiveError("the question text is empty")
    if "`" in body:
        raise PrimitiveError("the question text must not contain backticks (Splunk expands them as macros)")
    wire = OrderedDict([("type", mode), ("instructions", PRIMITIVE_TEMPLATE.format(about=about_phrase(keys), text=body))])
    if mode == "noul":
        if options is not None or levels is not None:
            raise PrimitiveError("noul takes criteria_true= and criteria_false=, not options= or levels=")
        if criteria_true or criteria_false:
            criteria = OrderedDict()
            if criteria_true:
                criteria["true"] = normalize_text(criteria_true)
            if criteria_false:
                criteria["false"] = normalize_text(criteria_false)
            wire["criteria"] = criteria
    elif mode == "choice":
        if levels is not None or criteria_true or criteria_false:
            raise PrimitiveError("choice takes options=, not levels= or criteria_*")
        if not options:
            raise PrimitiveError("choice needs options=\"a: description; b: description\" or options=lookup:<name>")
        wire["criteria"] = OrderedDict(options)
    else:
        if options is not None or criteria_true or criteria_false:
            raise PrimitiveError("score takes levels=, not options= or criteria_*")
        if not levels:
            raise PrimitiveError("score needs levels=\"lowest; ...; highest\" or levels=lookup:<name>")
        wire["criteria"] = list(levels)
    return wire


def parse_inline_options(spec):
    """'a: desc; b; c: desc' -> OrderedDict(a='desc', b=None, c='desc')."""
    options = OrderedDict()
    for part in str(spec).split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, description = part.split(":", 1)
            name, description = name.strip(), normalize_text(description)
        else:
            name, description = part, ""
        if not name:
            raise PrimitiveError("options= has an entry without a name: %r" % part)
        if name in options:
            raise PrimitiveError("options= lists %r twice" % name)
        options[name] = description or None
    return options


def parse_inline_levels(spec):
    """'calm; annoyed; angry' -> ['calm', 'annoyed', 'angry'] (lowest first)."""
    return [normalize_text(part) for part in str(spec).split(";") if normalize_text(part)]


class PrimitiveSpec(object):
    """Parsed positional arguments of `| jev <mode> [<fields>] "<text>" [as <alias>]`."""

    def __init__(self, mode, fields, text, alias):
        self.mode = mode
        self.fields = fields      # state mapping text, e.g. "message" or "msg=message,subject"
        self.text = text
        self.alias = alias


def parse_positionals(tokens):
    """Split the positional tokens (options already removed) into a PrimitiveSpec.

    Accepted shapes, after an optional trailing `as <alias>`:
        <mode> "<text>"             -> fields default to text=_raw
        <mode> <fields> "<text>"
    """
    tokens = [str(t) for t in tokens]
    alias = None
    if len(tokens) >= 2 and tokens[-2].lower() == "as":
        alias = tokens[-1]
        tokens = tokens[:-2]
        if not ALIAS_RE.match(alias):
            raise PrimitiveError("alias %r must start with a letter or underscore and use only letters, digits and _" % alias)
    if not tokens:
        raise PrimitiveError("expected noul, choice or score followed by a quoted question")
    mode = tokens[0].lower()
    if mode not in MODES:
        raise PrimitiveError("unknown question type %r; use noul, choice or score" % tokens[0])
    rest = tokens[1:]
    if len(rest) == 1:
        fields, text = "text=_raw", rest[0]
    elif len(rest) == 2:
        fields, text = rest
    elif not rest:
        raise PrimitiveError("%s needs a question in double quotes" % mode)
    else:
        # splunkd split an unquoted question into words, or an option name is misspelled
        for token in rest:
            if "=" in token and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                raise PrimitiveError("unknown option %r" % token.split("=", 1)[0])
        raise PrimitiveError("put the question in double quotes: | jev %s <field> \"<question>\" as <name>" % mode)
    return PrimitiveSpec(mode, fields, text, alias or "jev_" + mode)
