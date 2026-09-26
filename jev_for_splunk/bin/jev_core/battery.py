"""Question batteries: named sets of typed Jev questions plus a state mapping.

A battery file (<app>/default/batteries/<id>.json, overridable in local/) looks like:

{
  "id": "shop_messages", "version": 1, "description": "...",
  "state": {"message": "message"},
  "required_state": ["message"],
  "context": "Shared framing prepended to every *full-form* question.",
  "questions": {
    "overcharged": {"type": "noul", "text": "The customer says they were charged more than they should have been"},
    "area": {"type": "choice", "text": "Which part of the shop is this message about?", "options": "lookup:shop_areas"},
    "risk": {"type": "score", "instructions": "...", "criteria": ["...", "..."]},
    "destructive": {"type": "noul", "instructions": "...", "criteria": {"true": "...", "false": "..."}, "requires": ["command"]}
  }
}

Questions come in two forms:
- full form: `instructions` (+ `criteria`), sent as written with `context` prepended;
- text form: `text` (+ `options` / `levels` / `criteria_true` / `criteria_false`,
  optional `about`), built by jev_core.primitive exactly like the SPL
  `| jev noul|choice|score <field> "<text>"`, so both share cache entries.
  `context` is never added to text-form questions.

`requires` (optional, per question) lists state keys the question needs; when
one is missing from a record's state the question is dropped for that record.
"""
from __future__ import annotations

import glob
import json
import os
import re
from collections import OrderedDict

from .options import FileLookupResolver, ResolveError
from .primitive import PrimitiveError, primitive_question

QUESTION_TYPES = ("noul", "choice", "score")
STATE_TYPES = ("str", "bool", "num", "json")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_CHOICE_OPTIONS = 255
SCORE_LEVELS = (2, 10)
TEXT_FORM_KEYS = {"type", "text", "about", "options", "levels", "criteria_true", "criteria_false", "requires"}


class BatteryError(ValueError):
    pass


def parse_state_mapping(text):
    """'command=full_command,flag=dangerouslyDisableSandbox:bool' -> OrderedDict key -> (field, type)."""
    mapping = OrderedDict()
    if not text:
        return mapping
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, spec = part.split("=", 1)
        else:
            key, spec = part, part
        key = key.strip()
        spec = spec.strip()
        if ":" in spec:
            field, kind = spec.rsplit(":", 1)
            field, kind = field.strip(), kind.strip().lower()
        else:
            field, kind = spec, "str"
        if not KEY_RE.match(key):
            # a bare field such as data.message doubles as its own key; say how to name it
            hint = "" if "=" in part else "; name it, for example msg=%s" % part
            raise BatteryError("state key %r must match %s%s" % (key, KEY_RE.pattern, hint))
        if kind not in STATE_TYPES:
            raise BatteryError("state type %r for %r must be one of %s" % (kind, key, ", ".join(STATE_TYPES)))
        if not field:
            raise BatteryError("state key %r has an empty field name" % key)
        mapping[key] = (field, kind)
    return mapping


def _normalize_mapping(raw):
    if raw is None:
        return OrderedDict()
    if isinstance(raw, str):
        return parse_state_mapping(raw)
    mapping = OrderedDict()
    for key, spec in raw.items():
        mapping.update(parse_state_mapping("%s=%s" % (key, spec)))
    return mapping


class Battery(object):
    def __init__(self, id, questions, state=None, required_state=None, context="",
                 description="", applies_to="", version=1, source=""):
        self.id = id
        self.version = version
        self.description = description
        self.applies_to = applies_to
        self.state = _normalize_mapping(state)
        self.required_state = list(required_state or [])
        self.context = context or ""
        self.questions = OrderedDict(questions)
        self.text_form = set()
        self.source = source

    def question_ids(self):
        return list(self.questions.keys())

    def to_dict(self):
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "applies_to": self.applies_to,
            "state": OrderedDict((k, "%s:%s" % v) for k, v in self.state.items()),
            "required_state": list(self.required_state),
            "context": self.context,
            "questions": self.questions,
        }


def is_text_form(question):
    return isinstance(question, dict) and "text" in question and "instructions" not in question


def validate_question(qid, question):
    if not QID_RE.match(qid or ""):
        raise BatteryError("question id %r must match %s" % (qid, QID_RE.pattern))
    if not isinstance(question, dict):
        raise BatteryError("question %r must be an object" % qid)
    qtype = question.get("type")
    if qtype not in QUESTION_TYPES:
        raise BatteryError("question %r has type %r; expected one of %s" % (qid, qtype, ", ".join(QUESTION_TYPES)))
    if is_text_form(question):
        unknown = set(question) - TEXT_FORM_KEYS
        if unknown:
            raise BatteryError("text-form question %r has unknown keys: %s" % (qid, ", ".join(sorted(unknown))))
        return
    instructions = question.get("instructions")
    if instructions in (None, "", [], {}):
        raise BatteryError("question %r has no instructions" % qid)
    criteria = question.get("criteria")
    if qtype == "choice":
        if not isinstance(criteria, dict) or len(criteria) < 2:
            raise BatteryError("choice %r needs a criteria object with at least 2 options" % qid)
        if len(criteria) > MAX_CHOICE_OPTIONS:
            raise BatteryError("choice %r has %d options; the API allows %d" % (qid, len(criteria), MAX_CHOICE_OPTIONS))
    elif qtype == "score":
        if not isinstance(criteria, list) or not (SCORE_LEVELS[0] <= len(criteria) <= SCORE_LEVELS[1]):
            raise BatteryError("score %r needs a criteria list with %d to %d levels" % (qid, SCORE_LEVELS[0], SCORE_LEVELS[1]))
    elif qtype == "noul" and criteria is not None:
        if not isinstance(criteria, dict) or set(criteria.keys()) - {"true", "false"}:
            raise BatteryError("noul %r criteria may only have 'true' and 'false' keys" % qid)
    requires = question.get("requires")
    if requires is not None and not (isinstance(requires, list) and all(isinstance(r, str) for r in requires)):
        raise BatteryError("question %r 'requires' must be a list of state keys" % qid)


def compile_text_form(battery, qid, question, resolver):
    """Turn a text-form question into its wire form with the primitive builder."""
    about = question.get("about") or list(battery.state.keys())
    if isinstance(about, str):
        about = [a.strip() for a in about.split(",") if a.strip()]
    missing = [key for key in about if key not in battery.state]
    if missing:
        raise BatteryError("question %r is about %s, which the battery's state mapping lacks" % (qid, ", ".join(missing)))
    try:
        options = resolver.options(question["options"]) if question.get("options") is not None else None
        levels = resolver.levels(question["levels"]) if question.get("levels") is not None else None
        wire = primitive_question(question["type"], about, question["text"], options=options, levels=levels,
                                  criteria_true=question.get("criteria_true"),
                                  criteria_false=question.get("criteria_false"))
    except (PrimitiveError, ResolveError) as exc:
        raise BatteryError("question %r: %s" % (qid, exc))
    if question.get("requires"):
        wire["requires"] = list(question["requires"])
    return wire


def validate_battery(battery, resolver=None):
    if not ID_RE.match(battery.id or ""):
        raise BatteryError("battery id %r must match %s" % (battery.id, ID_RE.pattern))
    if not battery.questions:
        raise BatteryError("battery %r has no questions" % battery.id)
    for qid, question in list(battery.questions.items()):
        validate_question(qid, question)
        if is_text_form(question):
            if resolver is None:
                resolver = FileLookupResolver(None, scan=False)
            battery.questions[qid] = compile_text_form(battery, qid, question, resolver)
            battery.text_form.add(qid)
            validate_question(qid, battery.questions[qid])
    for key in battery.required_state:
        if key not in battery.state:
            raise BatteryError("battery %r requires state key %r that is not in its state mapping" % (battery.id, key))
    return battery


def battery_dirs(app_root):
    return [os.path.join(app_root, "default", "batteries"), os.path.join(app_root, "local", "batteries")]


def list_batteries(app_root):
    """{id: path} inside one app; a local/ file with the same id replaces the default one."""
    found = OrderedDict()
    for directory in battery_dirs(app_root):
        for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
            found[os.path.splitext(os.path.basename(path))[0]] = path
    return found


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle, object_pairs_hook=OrderedDict)
        except ValueError as exc:
            raise BatteryError("%s is not valid JSON: %s" % (path, exc))


def battery_from_dict(data, source=""):
    if not isinstance(data, dict):
        raise BatteryError("battery %s must be a JSON object" % (source or ""))
    return Battery(
        id=data.get("id") or os.path.splitext(os.path.basename(source))[0],
        questions=data.get("questions") or OrderedDict(),
        state=data.get("state"),
        required_state=data.get("required_state"),
        context=data.get("context", ""),
        description=data.get("description", ""),
        applies_to=data.get("applies_to", ""),
        version=data.get("version", 1),
        source=source,
    )


def _as_resolver(source):
    if source is None:
        return None
    if isinstance(source, str):
        return FileLookupResolver.for_app_root(source)
    return source


def load_battery(source, battery_id=None, inline_questions=None, state_override=None):
    """Resolve a battery from files and/or inline JSON, then validate it.

    source:           an app directory (str) or a FileLookupResolver.
    battery_id:       'id' or 'app:id'.
    inline_questions: JSON text or dict merged over the file's questions (by id).
    state_override:   mapping text or dict merged over the file's state mapping.
    Without battery_id the result is an ad-hoc battery called 'adhoc'.
    """
    resolver = _as_resolver(source)
    if battery_id:
        if resolver is None:
            raise BatteryError("unknown battery %r; no apps directory" % battery_id)
        try:
            path = resolver.battery_path(battery_id)
        except ResolveError as exc:
            available = ", ".join(sorted(k.split(":", 1)[1] for k in resolver.list_batteries())) or "none"
            raise BatteryError("%s; available: %s" % (exc, available))
        battery = battery_from_dict(read_json(path), path)
    else:
        battery = Battery(id="adhoc", questions=OrderedDict(), source="inline")
    if inline_questions:
        if isinstance(inline_questions, str):
            try:
                inline = json.loads(inline_questions, object_pairs_hook=OrderedDict)
            except ValueError as exc:
                raise BatteryError("questions= is not valid JSON: %s" % exc)
        else:
            inline = inline_questions
        if not isinstance(inline, dict):
            raise BatteryError("questions= must be a JSON object keyed by question id")
        for qid, question in inline.items():
            battery.questions[qid] = question
    if state_override:
        battery.state.update(_normalize_mapping(state_override))
    if not battery.state:
        raise BatteryError("battery %r has no state mapping; pass state=\"key=field,...\"" % battery.id)
    return validate_battery(battery, resolver)


def _with_context(context, instructions):
    if not context:
        return instructions
    if isinstance(instructions, str):
        return context.rstrip() + "\n\n" + instructions
    if isinstance(instructions, dict):
        merged = OrderedDict([("context", context)])
        merged.update(instructions)
        return merged
    return [context] + list(instructions)


def wire_questions(battery, state=None):
    """Build the API `questions` payload; drop questions whose `requires` keys are absent from `state`."""
    payload = OrderedDict()
    for qid, question in battery.questions.items():
        requires = question.get("requires") or []
        if state is not None and any(key not in state for key in requires):
            continue
        wire = OrderedDict([("type", question["type"])])
        if qid in battery.text_form:
            wire["instructions"] = question["instructions"]
        else:
            wire["instructions"] = _with_context(battery.context, question["instructions"])
        if question.get("criteria") is not None:
            wire["criteria"] = question["criteria"]
        payload[qid] = wire
    return payload
