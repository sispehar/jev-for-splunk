"""The parts that need splunkd: the TypeSafe key (storage/passwords or the use_jev endpoint) and the KV store cache.

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
# restmap.conf [script:jev_key]: splunkd serves it only to holders of use_jev (bin/jev_key.py)
KEY_ENDPOINT = "jev_for_splunk/key"
NO_ACCESS = ("your role needs the use_jev capability (the jev_user role has it) or list_storage_passwords "
             "to run | jev")


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
            raise JevError("auth_failed", NO_ACCESS)
        if exc.status != 404:
            raise JevError("storage_error", "could not read storage/passwords: %s" % exc)
    raise JevError("api_key_missing",
                   "no TypeSafe API key stored yet; open the Jev for Splunk Setup page (realm=%s user=%s)" % (realm, username))


def key_from_endpoint(searchinfo, app=OWN_APP, service=None):
    """Read the key through this app's endpoint, which needs use_jev instead of list_storage_passwords."""
    from splunklib.binding import HTTPError

    service = service or service_for(searchinfo, app)
    try:
        response = service.get(KEY_ENDPOINT, owner="nobody", app=app, output_mode="json")
        data = json.loads(response.body.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        if exc.status in (401, 403):
            raise JevError("auth_failed", NO_ACCESS)
        raise JevError("endpoint_unavailable", "the key endpoint answered HTTP %d" % exc.status)
    except ValueError:
        raise JevError("endpoint_unavailable", "the key endpoint did not answer with JSON")
    if not isinstance(data, dict) or not data.get("key"):
        raise JevError("api_key_missing", "no TypeSafe API key stored yet; open the Jev for Splunk Setup page")
    return data["key"]


def read_key(searchinfo, realm, username, app=OWN_APP, service=None):
    """(key, how): storage/passwords with the user's own session, else this app's use_jev endpoint.

    Admins read the key directly. Users with the jev_user role lack list_storage_passwords, which
    would open every stored secret to them, and get only this key from the endpoint.
    """
    service = service or service_for(searchinfo, app)
    try:
        return key_from_splunk(searchinfo, realm, username, app=app, service=service), "storage/passwords"
    except JevError as direct:
        try:
            return key_from_endpoint(searchinfo, app=app, service=service), "the use_jev endpoint"
        except JevError as brokered:
            if brokered.code != "endpoint_unavailable":
                raise brokered
            if direct.code == "auth_failed":
                # splunkd registers a new endpoint only at startup
                raise JevError("auth_failed", "%s (with jev_user, restart Splunk once after installing or upgrading "
                                              "the app: %s)" % (NO_ACCESS, brokered.message))
            raise direct


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

    def purge(self, before):
        """Delete answers created before the epoch `before` (on the accelerated `created` field).

        The KV store does not report how many it deleted, so this returns None.
        """
        from splunklib.binding import HTTPError

        try:
            self.service.delete(self.path.rstrip("/"), owner="nobody", app=self.app,
                                query=json.dumps({"created": {"$lt": int(before)}})).body.read()
        except HTTPError as exc:
            if exc.status in (401, 403):
                raise CacheReadOnly("HTTP %d deleting from collections/%s" % (exc.status, self.collection))
            raise CacheUnavailable("HTTP %d on collections/%s" % (exc.status, self.collection))
        except (socket.error, OSError) as exc:
            raise CacheUnavailable("%s: %s" % (type(exc).__name__, exc))
        return None
