"""bin/jev_key.py, the use_jev key endpoint, with splunkd's persistconn module stubbed out."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

import pytest

from jev_core.client import JevError

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HANDLER = os.path.join(REPO, "jev_for_splunk", "bin", "jev_key.py")


class _Log(object):
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args)

    error = info


@pytest.fixture
def endpoint(monkeypatch):
    application = types.ModuleType("splunk.persistconn.application")
    application.PersistentServerConnectionApplication = type("PersistentServerConnectionApplication", (object,), {})
    for name, module in (("splunk", types.ModuleType("splunk")),
                         ("splunk.persistconn", types.ModuleType("splunk.persistconn")),
                         ("splunk.persistconn.application", application)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("jev_key_under_test", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(**changes):
    request = {"method": "GET", "system_authtoken": "system-token", "server": {"rest_uri": "https://127.0.0.1:8089"},
               "session": {"user": "alice", "authtoken": "alice-token"}}
    request.update(changes)
    return request


def test_serves_this_apps_key_read_with_the_system_token(endpoint, monkeypatch):
    seen = {}

    def fake_key(info, realm, username, app=None, service=None):
        seen.update(uri=info.splunkd_uri, token=info.session_key, realm=realm, username=username, app=app)
        return "sk-live"

    monkeypatch.setattr(endpoint, "key_from_splunk", fake_key)
    log = _Log()
    assert endpoint.respond(_request(), logger=log) == {"status": 200, "payload": {"key": "sk-live"}}
    # the realm and user come from jev.conf, never from the request
    assert seen == {"uri": "https://127.0.0.1:8089", "token": "system-token", "realm": "jev_for_splunk",
                    "username": "typesafe_api_key", "app": "jev_for_splunk"}
    assert log.lines == ["key served user=alice"]


def test_refuses_other_methods_and_a_missing_system_token(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "key_from_splunk", lambda *args, **kwargs: "sk")
    assert endpoint.respond(_request(method="POST"), logger=_Log())["status"] == 405
    request = _request()
    del request["system_authtoken"]
    assert endpoint.respond(request, logger=_Log())["status"] == 500


def test_no_key_is_an_empty_answer_and_errors_never_echo_the_token(endpoint, monkeypatch):
    def missing(*args, **kwargs):
        raise JevError("api_key_missing", "none stored")

    monkeypatch.setattr(endpoint, "key_from_splunk", missing)
    assert endpoint.respond(_request(), logger=_Log()) == {"status": 200, "payload": {"key": ""}}

    def broken(*args, **kwargs):
        raise JevError("storage_error", "could not read storage/passwords: HTTP 500")

    monkeypatch.setattr(endpoint, "key_from_splunk", broken)
    log = _Log()
    reply = endpoint.respond(_request(), logger=log)
    assert reply["status"] == 500 and "system-token" not in json.dumps(reply) + " ".join(log.lines)


def test_the_handler_parses_splunkd_json(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "key_from_splunk", lambda *args, **kwargs: "sk")
    monkeypatch.setattr(endpoint, "_logger", _Log)
    handler = endpoint.KeyHandler("", "")
    assert handler.handle("not json")["status"] == 400
    assert handler.handle(json.dumps(_request()))["payload"] == {"key": "sk"}
