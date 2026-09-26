#!/usr/bin/env python
"""GET /servicesNS/nobody/jev_for_splunk/jev_for_splunk/key - the TypeSafe key, for | jev.

splunkd runs this only for users who hold use_jev (restmap.conf [script:jev_key]) and passes it a
system token. The roles that run | jev (jev_user) therefore do not need list_storage_passwords,
which would open every stored secret to them. It serves this app's key and nothing else: the realm
and user come from jev.conf, which only admins can write.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys

if sys.platform == "win32":  # persistent handlers talk to splunkd over binary stdin/stdout
    import msvcrt
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        msvcrt.setmode(_stream.fileno(), os.O_BINARY)

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(BIN_DIR)
for _path in (os.path.join(BIN_DIR, "lib"), BIN_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from splunk.persistconn.application import PersistentServerConnectionApplication  # noqa: E402

from jev_core.client import JevError  # noqa: E402
from jev_core.config import load_config  # noqa: E402
from jev_splunk import key_from_splunk  # noqa: E402

OWN_APP = os.path.basename(APP_ROOT)


def _logger():
    """One line per key served, in jev.log next to the search commands' lines."""
    logger = logging.getLogger("JevKeyEndpoint")
    if not logger.handlers:
        home = os.environ.get("SPLUNK_HOME")
        if home:
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(home, "var", "log", "splunk", "jev.log"), "a", 5242880, 5, "utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s level=%(levelname)s logger=%(name)s pid=%(process)d %(message)s"))
        else:
            handler = logging.NullHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class _Splunkd(object):
    """The two searchinfo attributes jev_splunk.service_for reads, filled from the request."""

    def __init__(self, uri, token):
        self.splunkd_uri = uri
        self.session_key = token


def respond(request, logger=None):
    """The reply to one parsed request. Kept apart from the handler class so tests need no splunkd."""
    logger = logger or _logger()
    user = (request.get("session") or {}).get("user", "")
    if request.get("method", "GET") != "GET":
        return {"status": 405, "payload": {"error": "use GET"}}
    token = request.get("system_authtoken")
    if not token:
        return {"status": 500, "payload": {"error": "passSystemAuth is off for this endpoint"}}
    uri = (request.get("server") or {}).get("rest_uri") or "https://127.0.0.1:8089"
    try:
        cfg = load_config(APP_ROOT)
        key = key_from_splunk(_Splunkd(uri, token), cfg.realm, cfg.username, app=OWN_APP)
    except JevError as exc:
        if exc.code == "api_key_missing":
            return {"status": 200, "payload": {"key": ""}}
        logger.error("key endpoint failed user=%s error=%s", user, exc.message)
        return {"status": 500, "payload": {"error": exc.message}}
    except Exception as exc:  # a config error; never echo the request, which carries the token
        logger.error("key endpoint failed user=%s error=%s: %s", user, type(exc).__name__, exc)
        return {"status": 500, "payload": {"error": "%s: %s" % (type(exc).__name__, exc)}}
    logger.info("key served user=%s", user)
    return {"status": 200, "payload": {"key": key}}


class KeyHandler(PersistentServerConnectionApplication):
    def __init__(self, command_line, command_arg):
        super(KeyHandler, self).__init__()

    def handle(self, in_string):
        try:
            request = json.loads(in_string)
        except ValueError:
            return {"status": 400, "payload": {"error": "the request is not JSON"}}
        return respond(request)
