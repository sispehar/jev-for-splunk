"""The parts that need splunkd: the TypeSafe key in storage/passwords and the KV store cache.

Kept out of jev_core so the core stays importable (and testable) without splunklib.
"""
from __future__ import annotations

import json
import socket
from urllib.parse import quote, urlsplit

from jev_core.cache import CacheReadOnly, CacheUnavailable, fresh_enough
from jev_core.client import JevError

OWN_APP = "jev_for_splunk"
READ_BATCH = 500
WRITE_BATCH = 1000


def service_for(searchinfo, app=OWN_APP):
    from splunklib.client import Service  # vendored in bin/lib

    uri = urlsplit(getattr(searchinfo, "splunkd_uri", "") or "https://127.0.0.1:8089")
    return Service(scheme=uri.scheme or "https", host=uri.hostname or "127.0.0.1", port=uri.port or 8089,
                   token=getattr(searchinfo, "session_key", None), owner="nobody", app=app)


def key_from_splunk(searchinfo, realm, username, app=OWN_APP, service=None):
    """Read the single credential `realm:username:` from the app namespace with the user's session."""
    from splunklib.binding import HTTPError, UrlEncoded

    service = service or service_for(searchinfo, app)
    # Marked as already encoded: splunklib quotes a plain str again, and %253A is a 404.
    name = UrlEncoded(quote("%s:%s:" % (realm, username), safe=""), skip_encode=True)
    try:
        response = service.get("storage/passwords/" + name, owner="nobody", app=app, output_mode="json")
        data = json.loads(response.body.read().decode("utf-8"))
        for entry in data.get("entry", []):
            secret = (entry.get("content") or {}).get("clear_password") or ""
            if secret:
                return secret
    except HTTPError as exc:
        if exc.status in (401, 403):
            raise JevError("auth_failed", "your role needs the list_storage_passwords capability to run | jev")
        if exc.status != 404:
            raise JevError("api_key_missing", "could not read storage/passwords: %s" % exc)
    raise JevError("api_key_missing",
                   "no TypeSafe API key stored yet; open the Jev for Splunk Setup page (realm=%s user=%s)" % (realm, username))


class KVStoreCache(object):
    """jev_core cache backend on a KV store collection, via batch_find / batch_save."""

    persistent = True
    name = "kvstore"

    def __init__(self, service, collection="jev_cache", app=OWN_APP):
        self.service = service
        self.collection = collection
        self.app = app
        self.path = "storage/collections/data/%s/" % quote(collection, safe="")

    def _post(self, sub, payload, writing):
        from splunklib.binding import HTTPError

        try:
            response = self.service.post(self.path + sub, owner="nobody", app=self.app,
                                         headers=[("Content-Type", "application/json")],
                                         body=json.dumps(payload, ensure_ascii=False))
            text = response.body.read().decode("utf-8")
            return json.loads(text) if text else None
        except HTTPError as exc:
            if writing and exc.status in (401, 403):
                raise CacheReadOnly("HTTP %d writing collections/%s" % (exc.status, self.collection))
            raise CacheUnavailable("HTTP %d on collections/%s" % (exc.status, self.collection))
        except (socket.error, OSError, ValueError) as exc:
            raise CacheUnavailable("%s: %s" % (type(exc).__name__, exc))

    def get_many(self, keys, ttl_days=0):
        keys = list(keys)
        found = {}
        for start in range(0, len(keys), READ_BATCH):
            part = keys[start:start + READ_BATCH]
            query = {"query": {"$or": [{"_key": key} for key in part]}, "limit": len(part)}
            result = self._post("batch_find", [query], writing=False) or []
            for docs in result:
                for doc in docs or []:
                    if isinstance(doc, dict) and doc.get("_key") and fresh_enough(doc, ttl_days):
                        found[doc["_key"]] = doc
        return found

    def put_many(self, docs):
        docs = list(docs)
        for start in range(0, len(docs), WRITE_BATCH):
            self._post("batch_save", docs[start:start + WRITE_BATCH], writing=True)
