"""Turn Jev answers into a fixed set of Splunk fields.

The chunked protocol takes the output field list from the first record of each
chunk and silently drops keys that appear later, so every record gets every
field of the schema, empty when there is nothing to say.

Two naming schemes:
- battery mode: <prefix><question id> plus shared bookkeeping <prefix>{model, error, ...};
- primitive mode: <alias> plus per-alias bookkeeping <alias>_{error, cached, tokens, model},
  so chained `| jev noul ... as a | jev choice ... as b` never collide.
"""
from __future__ import annotations

import re

META_FIELDS = ("model", "input_tokens", "output_tokens", "latency_ms", "error", "battery",
               "battery_version", "state_hash", "truncated", "cached", "attempts")
PRIMITIVE_META = ("error", "cached", "tokens", "model")
PRIMITIVE_META_FULL = ("latency_ms", "attempts", "key", "truncated")

_SANITIZE = re.compile(r"[^A-Za-z0-9_]+")


def sanitize(name):
    cleaned = _SANITIZE.sub("_", str(name)).strip("_")
    return cleaned or "x"


def question_fields(qid, question, prefix, probs, base=None):
    base = base if base is not None else prefix + qid
    qtype = question["type"]
    fields = [base]
    if qtype == "choice":
        fields.append(base + "_confidence")
        if probs:
            fields.extend(base + "_p_" + sanitize(option) for option in question["criteria"].keys())
    elif qtype == "score":
        fields.extend([base + "_level", base + "_label", base + "_confidence"])
        if probs:
            fields.extend(base + "_p_" + str(index) for index in range(len(question["criteria"])))
    return fields


def _dedupe(fields):
    seen = set()
    ordered = []
    for field in fields:
        if field not in seen:
            seen.add(field)
            ordered.append(field)
    return ordered


def output_schema(battery, prefix="jev_", probs=False):
    """Battery-mode schema: answers first, then the shared bookkeeping fields."""
    fields = []
    for qid, question in battery.questions.items():
        fields.extend(question_fields(qid, question, prefix, probs))
    fields.extend(prefix + name for name in META_FIELDS)
    return _dedupe(fields)


def primitive_schema(question, alias, probs=False, meta="basic"):
    fields = question_fields(alias, question, "", probs, base=alias)
    fields.extend(alias + "_" + name for name in PRIMITIVE_META)
    if meta == "full":
        fields.extend(alias + "_" + name for name in PRIMITIVE_META_FULL)
    return _dedupe(fields)


def _fmt(number, digits=4):
    try:
        return ("%." + str(digits) + "f") % float(number)
    except (TypeError, ValueError):
        return ""


def blank_fields(record, schema):
    for field in schema:
        record[field] = ""


def _argmax_level(probabilities):
    best_key, best_value = None, -1.0
    for key, value in (probabilities or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > best_value:
            best_key, best_value = key, number
    return best_key


def fill_answer(record, question, answer, base, probs=False):
    """Write one answer under `base`. Unknown or missing answers stay blank."""
    if not isinstance(answer, dict):
        return
    qtype = question["type"]
    if qtype == "noul":
        record[base] = _fmt(answer.get("noul"))
    elif qtype == "choice":
        record[base] = str(answer.get("choice", ""))
        record[base + "_confidence"] = _fmt(answer.get("confidence"))
        if probs:
            for option, value in (answer.get("probabilities") or {}).items():
                record[base + "_p_" + sanitize(option)] = _fmt(value)
    elif qtype == "score":
        record[base] = _fmt(answer.get("score"))
        level = _argmax_level(answer.get("probabilities"))
        record[base + "_level"] = "" if level is None else str(level)
        legend = answer.get("legend") or {}
        label = legend.get(str(level)) if level is not None else None
        if label is None and level is not None:
            try:
                label = question["criteria"][int(level)]
            except (ValueError, IndexError, TypeError):
                label = ""
        record[base + "_label"] = str(label or "")
        record[base + "_confidence"] = _fmt(answer.get("confidence"))
        if probs:
            for index, value in (answer.get("probabilities") or {}).items():
                record[base + "_p_" + sanitize(index)] = _fmt(value)


def fill_answers(record, battery, answers, prefix="jev_", probs=False):
    """Write one battery's answers into `record`."""
    for qid, question in battery.questions.items():
        fill_answer(record, question, (answers or {}).get(qid), prefix + qid, probs)


def fill_meta(record, prefix, battery, model="", input_tokens="", output_tokens="", latency_ms="",
              error="", state_hash="", truncated=False, cached=False, attempts=""):
    record[prefix + "model"] = model or ""
    record[prefix + "input_tokens"] = "" if input_tokens == "" else str(int(round(float(input_tokens))))
    record[prefix + "output_tokens"] = "" if output_tokens == "" else str(int(round(float(output_tokens))))
    record[prefix + "latency_ms"] = "" if latency_ms == "" else str(int(round(float(latency_ms))))
    record[prefix + "error"] = error or ""
    record[prefix + "battery"] = battery.id
    record[prefix + "battery_version"] = str(battery.version)
    record[prefix + "state_hash"] = state_hash or ""
    record[prefix + "truncated"] = "1" if truncated else "0"
    record[prefix + "cached"] = "1" if cached else "0"
    record[prefix + "attempts"] = "" if attempts == "" else str(int(attempts))


def fill_primitive_meta(record, alias, meta="basic", error="", cached=False, tokens="", model="",
                        latency_ms="", attempts="", key="", truncated=False):
    record[alias + "_error"] = error or ""
    record[alias + "_cached"] = "1" if cached else "0"
    record[alias + "_tokens"] = "" if tokens == "" else "%.1f" % float(tokens)
    record[alias + "_model"] = model or ""
    if meta == "full":
        record[alias + "_latency_ms"] = "" if latency_ms == "" else str(int(round(float(latency_ms))))
        record[alias + "_attempts"] = "" if attempts == "" else str(int(attempts))
        record[alias + "_key"] = key or ""
        record[alias + "_truncated"] = "1" if truncated else "0"
