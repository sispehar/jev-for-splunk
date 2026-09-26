"""Per-question judgment cache: keys, documents and two stdlib backends.

A cache entry is one answer to one question about one state, under one model
name. The question id is not part of the key (TypeSafe never sends ids to the
model), so a battery question and a primitive call with the same state and
wording share an entry. The Splunk KV store backend lives in bin/jev_splunk.py
so this module stays free of splunklib.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict

from .state import canonical_json

CACHE_SCHEMA = 1


class CacheUnavailable(Exception):
    """The backend cannot be read (KV store down, collection missing, network)."""


class CacheReadOnly(Exception):
    """The backend can be read but not written (permissions)."""


def question_json(wire):
    """Canonical text of the question as the model sees it. Criteria keep their order."""
    q = OrderedDict([("type", wire.get("type")), ("instructions", wire.get("instructions"))])
    if wire.get("criteria") is not None:
        q["criteria"] = wire["criteria"]
    return json.dumps(q, ensure_ascii=False, separators=(",", ":"))


def question_hash(wire):
    return hashlib.sha256(question_json(wire).encode("utf-8")).hexdigest()


def cache_key(model, state, wire):
    digest = hashlib.sha256(("jev-cache-v%d" % CACHE_SCHEMA).encode("utf-8") + b"\x00")
    for part in (str(model), canonical_json(state), question_json(wire)):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def make_doc(key, model, wire, answer, resolved_model, input_tokens, request_tokens, request_questions,
             latency_ms, created=None):
    """The stored form of one answer. It never contains the event text."""
    answer = dict(answer or {})
    answer.pop("legend", None)  # rebuilt from criteria when needed
    return OrderedDict([
        ("_key", key),
        ("model", str(model)),
        ("resolved_model", str(resolved_model or "")),
        ("qtype", wire.get("type")),
        ("qhash", question_hash(wire)),
        ("question", question_json(wire)[:4000]),
        ("answer", json.dumps(answer, ensure_ascii=False, separators=(",", ":"))),
        ("input_tokens", round(float(input_tokens or 0), 3)),
        ("request_tokens", int(request_tokens or 0)),
        ("request_questions", int(request_questions or 1)),
        ("latency_ms", int(round(float(latency_ms or 0)))),
        ("created", int(created if created is not None else time.time())),
        ("schema", CACHE_SCHEMA),
    ])


def doc_answer(doc):
    try:
        return json.loads(doc.get("answer") or "{}")
    except (TypeError, ValueError):
        return None


def fresh_enough(doc, ttl_days, now=None):
    if not ttl_days:
        return True
    try:
        created = float(doc.get("created") or 0)
    except (TypeError, ValueError):
        return False
    return created >= (now if now is not None else time.time()) - float(ttl_days) * 86400.0


class MemoryCache(object):
    """A dict-backed persistent-looking cache for tests."""

    persistent = True
    name = "memory"

    def __init__(self):
        self.docs = {}
        self.gets = 0
        self.puts = 0
        self.fail_get = None
        self.fail_put = None

    def get_many(self, keys, ttl_days=0):
        self.gets += 1
        if self.fail_get:
            raise self.fail_get
        return {k: self.docs[k] for k in keys if k in self.docs and fresh_enough(self.docs[k], ttl_days)}

    def put_many(self, docs):
        self.puts += 1
        if self.fail_put:
            raise self.fail_put
        for doc in docs:
            self.docs[doc["_key"]] = dict(doc)

    def purge(self, before):
        """Delete answers created before the epoch `before`; returns how many."""
        old = [key for key, doc in self.docs.items() if float(doc.get("created") or 0) < before]
        for key in old:
            del self.docs[key]
        return len(old)


class SqliteCache(object):
    """File-backed cache for the CLI and local development (JEV_CACHE_PATH)."""

    persistent = True
    name = "sqlite"

    def __init__(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jev_cache (key TEXT PRIMARY KEY, created REAL, doc TEXT)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get_many(self, keys, ttl_days=0):
        keys = list(keys)
        found = {}
        if not keys:
            return found
        try:
            with self._lock, self._connect() as conn:
                for start in range(0, len(keys), 500):
                    part = keys[start:start + 500]
                    marks = ",".join("?" * len(part))
                    for key, doc in conn.execute("SELECT key, doc FROM jev_cache WHERE key IN (%s)" % marks, part):
                        parsed = json.loads(doc)
                        if fresh_enough(parsed, ttl_days):
                            found[key] = parsed
        except sqlite3.Error as exc:
            raise CacheUnavailable("sqlite: %s" % exc)
        return found

    def put_many(self, docs):
        rows = [(d["_key"], float(d.get("created") or time.time()), json.dumps(d, ensure_ascii=False)) for d in docs]
        if not rows:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.executemany("INSERT OR REPLACE INTO jev_cache (key, created, doc) VALUES (?, ?, ?)", rows)
        except sqlite3.Error as exc:
            raise CacheReadOnly("sqlite: %s" % exc)

    def purge(self, before):
        """Delete answers created before the epoch `before`; returns how many."""
        try:
            with self._lock, self._connect() as conn:
                return conn.execute("DELETE FROM jev_cache WHERE created < ?", (float(before),)).rowcount
        except sqlite3.Error as exc:
            raise CacheReadOnly("sqlite: %s" % exc)

    def count(self):
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM jev_cache").fetchone()[0]
