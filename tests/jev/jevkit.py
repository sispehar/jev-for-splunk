"""Test helpers: a scripted fake urlopen, answer builders and the unit-test battery."""
from __future__ import annotations

import email.message
import io
import json
import os
import urllib.error
from collections import OrderedDict

from jev_core.client import JevClient


class FakeResponse(object):
    def __init__(self, body, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def http_error(status, body=None, headers=None):
    hdrs = email.message.Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    return urllib.error.HTTPError("https://api.example/v1/systemone", status, "err", hdrs, io.BytesIO(payload))


class FakeHTTP(object):
    """Queue of responses/exceptions; records every request body."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []
        self.sleeps = []

    def urlopen(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append({"method": request.get_method(), "url": request.full_url, "body": body,
                              "headers": dict(request.header_items())})
        if not self.script:
            raise AssertionError("no scripted response left for %s" % request.full_url)
        item = self.script.pop(0)
        if callable(item):
            item = item(body)
        if isinstance(item, BaseException):
            raise item
        return item

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def noul_answer(value):
    return {"type": "noul", "noul": value}


def choice_answer(choice, probabilities, confidence=0.9):
    return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence}


def score_answer(score, probabilities, legend, confidence=0.9):
    return {"type": "score", "score": score, "probabilities": probabilities, "legend": legend, "confidence": confidence}


def ok_response(answers, model="jev-1.13.0", input_tokens=300, output_tokens=20):
    return FakeResponse({"model": model, "answers": answers, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}})


TEST_BATTERY = OrderedDict([
    ("id", "test_battery"),
    ("version", 2),
    ("description", "battery used by the unit tests"),
    ("state", OrderedDict([("command", "full_command"), ("note", "description"), ("flag", "dangerous:bool"), ("args", "params:json")])),
    ("required_state", ["command"]),
    ("context", "CTX"),
    ("questions", OrderedDict([
        ("destructive", {"type": "noul", "instructions": "Is `command` destructive?", "criteria": {"true": "yes", "false": "no"}}),
        ("scope", {"type": "choice", "instructions": "Scope of `command`?", "criteria": OrderedDict([("project", "p"), ("system", "s"), ("unclear", None)])}),
        ("risk", {"type": "score", "instructions": "Risk of `command`?", "criteria": ["none", "low", "high"]}),
        ("mismatch", {"type": "noul", "instructions": "Does `note` match `command`?", "requires": ["note"]}),
    ])),
])


def sample_answers():
    return {
        "destructive": noul_answer(0.9312),
        "scope": choice_answer("system", {"project": 0.1, "system": 0.85, "unclear": 0.05}, 0.78),
        "risk": score_answer(1.9, {"0": 0.05, "1": 0.1, "2": 0.85}, {"0": "none", "1": "low", "2": "high"}, 0.8),
        "mismatch": noul_answer(0.2),
    }
