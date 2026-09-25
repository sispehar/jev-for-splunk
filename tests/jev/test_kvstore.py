"""The KV store adapter and the storage/passwords reader against a fake splunklib Service."""
from __future__ import annotations

import io
import json

import pytest

from jev_core.cache import CacheReadOnly, CacheUnavailable
from jev_core.client import JevError
from jev_splunk import KVStoreCache, key_from_splunk
from splunklib.binding import HTTPError


class _Body(object):
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload

    def read(self):
        return self._data


class _Response(object):
    def __init__(self, payload):
        self.body = _Body(payload)


def _http_error(status):
    response = type("R", (), {})()
    response.status = status
    response.reason = "error"
    response.headers = []
    response.body = io.BytesIO(b'{"messages":[{"type":"ERROR","text":"nope"}]}')
    return HTTPError(response)


class FakeService(object):
    def __init__(self, store=None, fail=None):
        self.store = store if store is not None else {}
        self.fail = fail or {}
        self.calls = []

    def post(self, path, owner=None, app=None, headers=None, body=None, **kwargs):
        self.calls.append(("POST", path, owner, app, json.loads(body)))
        op = path.rsplit("/", 1)[-1]
        if op in self.fail:
            raise _http_error(self.fail[op])
        payload = json.loads(body)
        if op == "batch_find":
            results = []
            for query in payload:
                keys = [clause["_key"] for clause in query["query"]["$or"]]
                results.append([self.store[k] for k in keys if k in self.store])
            return _Response(results)
        if op == "batch_save":
            for doc in payload:
                self.store[doc["_key"]] = doc
            return _Response([d["_key"] for d in payload])
        raise AssertionError(path)

    def get(self, path, owner=None, app=None, **kwargs):
        self.calls.append(("GET", path, owner, app, kwargs))
        if "get" in self.fail:
            raise _http_error(self.fail["get"])
        return _Response(self.store.get(path, {"entry": []}))


def _doc(key, created=2000000000):
    return {"_key": key, "answer": '{"type":"noul","noul":0.5}', "created": created}


def test_batches_reads_and_writes():
    service = FakeService()
    cache = KVStoreCache(service, collection="jev_cache", app="jev_for_splunk")
    cache.put_many([_doc("k%04d" % i) for i in range(2500)])
    saves = [c for c in service.calls if c[1].endswith("batch_save")]
    assert [len(c[4]) for c in saves] == [1000, 1000, 500]
    assert all(c[1] == "storage/collections/data/jev_cache/batch_save" and c[2] == "nobody" and c[3] == "jev_for_splunk"
               for c in saves)
    found = cache.get_many(["k%04d" % i for i in range(1200)] + ["missing"])
    finds = [c for c in service.calls if c[1].endswith("batch_find")]
    assert [len(c[4][0]["query"]["$or"]) for c in finds] == [500, 500, 201]
    assert len(found) == 1200 and "missing" not in found


def test_ttl_filter_and_status_mapping():
    service = FakeService(store={"old": _doc("old", created=1), "new": _doc("new")})
    cache = KVStoreCache(service)
    assert list(cache.get_many(["old", "new"], ttl_days=30)) == ["new"]
    with pytest.raises(CacheUnavailable):
        KVStoreCache(FakeService(fail={"batch_find": 503})).get_many(["a"])
    with pytest.raises(CacheUnavailable):
        KVStoreCache(FakeService(fail={"batch_find": 404})).get_many(["a"])
    with pytest.raises(CacheReadOnly):
        KVStoreCache(FakeService(fail={"batch_save": 403})).put_many([_doc("a")])
    with pytest.raises(CacheUnavailable):
        KVStoreCache(FakeService(fail={"batch_save": 500})).put_many([_doc("a")])


def test_key_from_splunk_reads_one_entry():
    path = "storage/passwords/jev_for_splunk%3Atypesafe_api_key%3A"
    service = FakeService(store={path: {"entry": [{"content": {"clear_password": "sk-test"}}]}})
    assert key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=service) == "sk-test"
    assert service.calls[0][1] == path and service.calls[0][3] == "jev_for_splunk"
    with pytest.raises(JevError) as info:
        key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=FakeService())
    assert info.value.code == "api_key_missing" and "Setup page" in info.value.message
    with pytest.raises(JevError) as info:
        key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=FakeService(fail={"get": 403}))
    assert "list_storage_passwords" in info.value.message


def test_key_path_is_encoded_once_by_splunklib():
    # The fake above never runs splunklib's own quoting; this does. A plain str path was
    # quoted twice (jev_for_splunk%253A...), which splunkd answered with 404.
    from splunklib.binding import Context

    service = FakeService()
    with pytest.raises(JevError):
        key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=service)
    path = service.calls[0][1]
    context = Context(scheme="https", host="127.0.0.1", port=8089, token="x", owner="nobody", app="jev_for_splunk")
    assert context._abspath(path, owner="nobody", app="jev_for_splunk") == \
        "/servicesNS/nobody/jev_for_splunk/storage/passwords/jev_for_splunk%3Atypesafe_api_key%3A"
