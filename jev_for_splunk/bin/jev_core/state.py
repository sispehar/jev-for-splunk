"""Turn a Splunk record into a compact Jev `state` object, and hash it."""
from __future__ import annotations

import hashlib
import json

from .config import parse_bool

TRUNCATION_MARK = " …[truncated]"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _scalar(value):
    """Splunk hands multivalue fields over as lists; join them so the model sees one text."""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value if v is not None and str(v) != ""]
        return "\n".join(parts) if parts else None
    if value is None:
        return None
    text = str(value)
    return text if text.strip() != "" else None


def coerce(value, kind):
    text = _scalar(value)
    if text is None:
        return None
    if kind == "str":
        return text
    if kind == "bool":
        return parse_bool(text, default=None)
    if kind == "num":
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    if kind == "json":
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def build_state(record, mapping, required=(), maxstate=16000):
    """Return (state, truncated, error). `state` is None when `error` is set.

    mapping: OrderedDict key -> (field, type). Missing/blank fields are left
    out; a missing required key yields error 'no_state'.
    """
    state = {}
    for key, (field, kind) in mapping.items():
        value = coerce(record.get(field), kind)
        if value is None:
            continue
        state[key] = value
    if not state:
        return None, False, "no_state"
    for key in required:
        if key not in state:
            return None, False, "no_state"
    truncated = False
    if maxstate and len(canonical_json(state)) > maxstate:
        state = _truncate(state, maxstate)
        truncated = True
    return state, truncated, None


def _string_leaves(node, path=()):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            for item in _string_leaves(value, path + (key,)):
                yield item
    elif isinstance(node, list):
        for index, value in enumerate(node):
            for item in _string_leaves(value, path + (index,)):
                yield item


def _set_leaf(node, path, value):
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _truncate(state, maxstate):
    """Cut string values proportionally until the serialized state fits."""
    leaves = list(_string_leaves(state))
    total = sum(len(text) for _, text in leaves)
    if total == 0:
        return state
    overhead = len(canonical_json(state)) - total
    budget = max(maxstate - overhead - len(TRUNCATION_MARK) * len(leaves), 0)
    if budget <= 0:
        budget = max(maxstate // 2, 1)
    ratio = float(budget) / float(total)
    for path, text in leaves:
        keep = int(len(text) * ratio)
        if keep < len(text):
            _set_leaf(state, path, text[:max(keep, 0)].rstrip() + TRUNCATION_MARK)
    # A single oversized leaf can still overshoot because of the fixed mark; hard-cap it.
    serialized = canonical_json(state)
    if len(serialized) > maxstate and leaves:
        path, _ = max(leaves, key=lambda item: len(item[1]))
        current = _get_leaf(state, path)
        excess = len(serialized) - maxstate
        _set_leaf(state, path, current[: max(len(current) - excess - len(TRUNCATION_MARK), 0)] + TRUNCATION_MARK)
    return state


def _get_leaf(node, path):
    for key in path:
        node = node[key]
    return node


def state_hash(state, questions, model):
    digest = hashlib.sha256()
    digest.update(canonical_json(state).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_json(questions).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(model).encode("utf-8"))
    return digest.hexdigest()
