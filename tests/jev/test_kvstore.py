"""The KV store adapter and the storage/passwords reader against a fake splunklib Service."""
from __future__ import annotations

import io
import json

import pytest

from jev_core.cache import CacheReadOnly, CacheUnavailable
from jev_core.client import JevError
from jev_splunk import KEY_ENDPOINT, KVStoreCache, key_from_splunk, read_key
from splunklib.binding import HTTPError

KEY_PATH = "storage/passwords/jev_for_splunk%3Atypesafe_api_key%3A"


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
        for target, status in self.fail.items():   # "get" fails every GET, a path prefix only its own
            if target == "get" or path.startswith(target):
                raise _http_error(status)
        return _Response(self.store.get(path, {"entry": []}))

    def delete(self, path, owner=None, app=None, **kwargs):
        self.calls.append(("DELETE", path, owner, app, kwargs))
        if "delete" in self.fail:
            raise _http_error(self.fail["delete"])
        return _Response(b"")


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


def test_purge_deletes_on_the_created_field():
    service = FakeService()
    assert KVStoreCache(service).purge(1700000000.7) is None     # the KV store does not count
    method, path, owner, app, kwargs = service.calls[0]
    assert (method, path, owner, app) == ("DELETE", "storage/collections/data/jev_cache", "nobody", "jev_for_splunk")
    assert json.loads(kwargs["query"]) == {"created": {"$lt": 1700000000}}
    with pytest.raises(CacheReadOnly):
        KVStoreCache(FakeService(fail={"delete": 403})).purge(1)
    with pytest.raises(CacheUnavailable):
        KVStoreCache(FakeService(fail={"delete": 503})).purge(1)


def test_key_from_splunk_reads_one_entry():
    path = KEY_PATH
    service = FakeService(store={path: {"entry": [{"content": {"clear_password": "sk-test"}}]}})
    assert key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=service) == "sk-test"
    assert service.calls[0][1] == path and service.calls[0][3] == "jev_for_splunk"
    with pytest.raises(JevError) as info:
        key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=FakeService())
    assert info.value.code == "api_key_missing" and "Setup page" in info.value.message
    with pytest.raises(JevError) as info:
        key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=FakeService(fail={"get": 403}))
    assert "list_storage_passwords" in info.value.message and "jev_user" in info.value.message
    with pytest.raises(JevError) as info:
        key_from_splunk(None, "jev_for_splunk", "typesafe_api_key", service=FakeService(fail={"get": 500}))
    assert info.value.code == "storage_error"


def test_read_key_prefers_storage_passwords_then_the_use_jev_endpoint():
    admin = FakeService(store={KEY_PATH: {"entry": [{"content": {"clear_password": "sk-direct"}}]}})
    assert read_key(None, "jev_for_splunk", "typesafe_api_key", service=admin) == ("sk-direct", "storage/passwords")
    assert len(admin.calls) == 1                                  # an admin never asks the endpoint
    # a jev_user: no list_storage_passwords, so the endpoint (splunkd checks use_jev) serves the key
    user = FakeService(store={KEY_ENDPOINT: {"key": "sk-endpoint"}}, fail={"storage/passwords": 403})
    assert read_key(None, "jev_for_splunk", "typesafe_api_key", service=user) == ("sk-endpoint", "the use_jev endpoint")
    assert user.calls[-1][1:4] == (KEY_ENDPOINT, "nobody", "jev_for_splunk")


@pytest.mark.parametrize("fail,store,code,words", [
    ({"storage/passwords": 403, KEY_ENDPOINT: 403}, {}, "auth_failed", "jev_user"),       # neither capability
    ({"storage/passwords": 403}, {KEY_ENDPOINT: {"key": ""}}, "api_key_missing", "Setup page"),  # nothing stored
    ({"storage/passwords": 403, KEY_ENDPOINT: 404}, {}, "auth_failed", "restart Splunk"),  # endpoint not loaded yet
    ({KEY_ENDPOINT: 404}, {}, "api_key_missing", "Setup page"),  # an admin with no key: the direct answer stands
])
def test_read_key_errors_say_what_to_do(fail, store, code, words):
    with pytest.raises(JevError) as info:
        read_key(None, "jev_for_splunk", "typesafe_api_key", service=FakeService(store=store, fail=fail))
    assert info.value.code == code and words in info.value.message, info.value.message


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
