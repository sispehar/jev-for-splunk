from __future__ import annotations

import json
import os
import tempfile
import time
from collections import OrderedDict

import pytest

from jevkit import http_error, noul_answer, ok_response
from jev_core.battery import Battery, battery_from_dict, validate_battery
from jev_core.cache import (CacheReadOnly, CacheUnavailable, MemoryCache, SqliteCache, cache_key, make_doc,
                            question_hash)
from jev_core.options import FileLookupResolver
from jev_core.primitive import primitive_question
from jev_core.runner import Evaluator

OVERCHARGED = "The customer says they were charged more than they should have been"


def _msg_battery():
    return validate_battery(battery_from_dict({
        "id": "msgs", "state": {"message": "message"}, "required_state": ["message"],
        "questions": OrderedDict([
            ("overcharged", {"type": "noul", "text": OVERCHARGED}),
            ("late", {"type": "noul", "text": "The parcel is late"}),
        ])}), FileLookupResolver(None, scan=False))


def _primitive(alias, text):
    battery = Battery(id="primitive", questions={alias: primitive_question("noul", ["message"], text)},
                      state="message", required_state=["message"])
    battery.text_form.add(alias)
    return battery


def _answers(body):
    return ok_response({qid: noul_answer(0.9) for qid in body["questions"]}, input_tokens=100 * len(body["questions"]))


def test_cache_key_ignores_ids_and_dict_order_but_not_model_or_wording():
    wire = primitive_question("noul", ["message"], OVERCHARGED)
    a = cache_key("jev-1.13.0", OrderedDict([("message", "hi"), ("x", 1)]), wire)
    b = cache_key("jev-1.13.0", OrderedDict([("x", 1), ("message", "hi")]), wire)
    assert a == b and len(a) == 64
    assert cache_key("jev-latest", {"message": "hi", "x": 1}, wire) != a
    other = primitive_question("noul", ["message"], OVERCHARGED + ".")
    assert cache_key("jev-1.13.0", {"message": "hi", "x": 1}, other) != a
    # choice option order is part of what the model sees, so part of the key
    c1 = primitive_question("choice", ["message"], "Which?", options=OrderedDict([("a", None), ("b", None)]))
    c2 = primitive_question("choice", ["message"], "Which?", options=OrderedDict([("b", None), ("a", None)]))
    assert cache_key("m", {"message": "hi"}, c1) != cache_key("m", {"message": "hi"}, c2)
    assert question_hash(c1) != question_hash(c2)


def test_battery_text_form_key_equals_primitive_key():
    battery = _msg_battery()
    wire_battery = battery.questions["overcharged"]
    wire_primitive = primitive_question("noul", ["message"], OVERCHARGED)
    assert cache_key("m", {"message": "hi"}, dict(wire_battery)) == cache_key("m", {"message": "hi"}, wire_primitive)


def test_second_run_makes_zero_requests(make_client, fake_http):
    backend = MemoryCache()
    records = [{"message": "I paid full price"}, {"message": "Where is my parcel?"}, {"message": "I paid full price"}]
    fake_http.script.extend([_answers, _answers])
    first = Evaluator(make_client(), _msg_battery(), cache_mode="kv", backend=backend, threads=1)
    out = first.process([dict(r) for r in records])
    assert len(fake_http.requests) == 2 and first.stats.fresh == 4 and first.stats.kv_saved == 4
    assert [r["jev_cached"] for r in out] == ["0", "0", "1"]
    second = Evaluator(make_client(), _msg_battery(), cache_mode="kv", backend=backend, threads=1)
    out2 = second.process([dict(r) for r in records])
    assert len(fake_http.requests) == 2                      # nothing new was sent
    assert second.stats.kv_hits == 4 and second.stats.fresh == 0 and second.stats.input_tokens == 0
    assert [r["jev_cached"] for r in out2] == ["1", "1", "1"]
    assert out2[0]["jev_overcharged"] == out[0]["jev_overcharged"] and out2[0]["jev_model"] == "jev-1.13.0"


def test_partial_hits_send_only_missing_questions_in_one_request(make_client, fake_http):
    backend = MemoryCache()
    # warm only the 'overcharged' question via the primitive form
    fake_http.script.append(_answers)
    Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="kv", backend=backend,
              primitive_alias="oc").process([{"message": "I paid full price"}])
    # the battery then needs only 'late' for that message: one request with one question
    fake_http.script.append(_answers)
    evaluator = Evaluator(make_client(), _msg_battery(), cache_mode="kv", backend=backend)
    out = evaluator.process([{"message": "I paid full price"}])
    assert len(fake_http.requests) == 2
    assert list(fake_http.requests[1]["body"]["questions"]) == ["late"]
    assert out[0]["jev_overcharged"] == "0.9000" and out[0]["jev_late"] == "0.9000" and out[0]["jev_cached"] == "0"
    assert evaluator.stats.kv_hits == 1 and evaluator.stats.fresh == 1


def test_errors_are_never_cached(make_client, fake_http):
    backend = MemoryCache()
    fake_http.script.append(http_error(422, {"detail": "bad"}))
    evaluator = Evaluator(make_client(), _msg_battery(), cache_mode="kv", backend=backend)
    out = evaluator.process([{"message": "x"}])
    assert out[0]["jev_error"].startswith("validation") and backend.docs == {}


def test_unavailable_backend_falls_back_to_memo_with_one_warning(make_client, fake_http):
    backend = MemoryCache()
    backend.fail_get = CacheUnavailable("HTTP 503")
    fake_http.script.extend([_answers])
    evaluator = Evaluator(make_client(), _msg_battery(), cache_mode="kv", backend=backend)
    out = evaluator.process([{"message": "x"}, {"message": "x"}])
    assert out[0]["jev_error"] == "" and out[1]["jev_cached"] == "1"
    assert evaluator.kv_state == "unavailable" and len([w for w in evaluator.warnings if "unavailable" in w]) == 1
    assert backend.puts == 0                                   # no writes to a backend that is down


def test_readonly_backend_keeps_reading(make_client, fake_http):
    backend = MemoryCache()
    backend.fail_put = CacheReadOnly("HTTP 403 writing collections/jev_cache")
    fake_http.script.extend([_answers])
    evaluator = Evaluator(make_client(), _msg_battery(), cache_mode="kv", backend=backend)
    evaluator.process([{"message": "x"}])
    assert evaluator.kv_state == "readonly" and any("cannot write" in w for w in evaluator.warnings)


def test_modes_off_refresh_memo(make_client, fake_http):
    backend = MemoryCache()
    fake_http.script.extend([_answers] * 3)
    off = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="off", backend=backend, primitive_alias="oc")
    off.process([{"message": "x"}, {"message": "x"}, {"message": "x"}])
    assert len(fake_http.requests) == 3 and backend.docs == {}   # off: no reuse, no writes
    fake_http.script.extend([_answers])
    refresh = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="refresh", backend=backend,
                        primitive_alias="oc")
    refresh.process([{"message": "x"}, {"message": "x"}])
    assert len(fake_http.requests) == 4 and len(backend.docs) == 1 and backend.gets == 0
    memo = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="memo", backend=backend, primitive_alias="oc")
    assert memo.backend is None


def test_dryrun_estimates_and_serves_cached(make_client, fake_http):
    backend = MemoryCache()
    fake_http.script.append(_answers)
    Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="kv", backend=backend,
              primitive_alias="oc").process([{"message": "old"}])
    dry = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="kv", backend=backend,
                    primitive_alias="oc", dryrun=True)
    out = dry.process([{"message": "old"}, {"message": "new message"}])
    assert len(fake_http.requests) == 1
    assert out[0]["oc_error"] == "" and out[0]["oc_cached"] == "1"
    assert out[1]["oc_error"] == "dryrun" and float(out[1]["oc_tokens"]) > 0 and dry.stats.est_tokens > 0


def test_ttl_expires_entries(make_client, fake_http):
    backend = MemoryCache()
    wire = primitive_question("noul", ["message"], OVERCHARGED)
    key = cache_key("jev-test", {"message": "x"}, wire)
    backend.put_many([make_doc(key, "jev-test", wire, {"type": "noul", "noul": 0.5}, "jev-1.13.0", 10, 10, 1, 5,
                               created=time.time() - 40 * 86400)])
    fake_http.script.append(_answers)
    evaluator = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="kv", backend=backend,
                          primitive_alias="oc", ttl_days=30)
    out = evaluator.process([{"message": "x"}])
    assert len(fake_http.requests) == 1 and out[0]["oc"] == "0.9000"   # the stale entry was ignored and replaced


def test_primitive_price_tag_and_paid_flag(make_client, fake_http):
    fake_http.script.append(lambda body: ok_response({"oc": noul_answer(0.8)}, input_tokens=240))
    evaluator = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="memo", primitive_alias="oc")
    out = evaluator.process([{"message": "x"}, {"message": "x"}])
    assert out[0]["oc_tokens"] == "240.0" and out[0]["oc_cached"] == "0"
    assert out[1]["oc_tokens"] == "240.0" and out[1]["oc_cached"] == "1"     # the tag repeats; only the first paid
    assert set(evaluator.schema) == {"oc", "oc_error", "oc_cached", "oc_tokens", "oc_model"}


def test_memo_is_bounded(make_client, fake_http):
    fake_http.script.extend([_answers] * 5)
    evaluator = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="memo", primitive_alias="oc",
                          memo_limit=2, threads=1)
    out = evaluator.process([{"message": str(i)} for i in range(5)])
    assert len(evaluator.memo) == 2
    # a chunk with more answers than the limit still hands every record its answer
    assert [r["oc_error"] for r in out] == [""] * 5 and all(r["oc"] == "0.9000" for r in out)


def test_memo_hit_survives_eviction_by_its_own_chunk(make_client, fake_http):
    # A full memo, then a chunk that repeats its oldest entry and adds new ones: the repeat was
    # planned as a hit, so the new answers must not evict it before the record is filled.
    fake_http.script.extend([_answers] * 6)
    evaluator = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="memo", primitive_alias="oc",
                          memo_limit=3, threads=1)
    evaluator.process([{"message": m} for m in ("a", "b", "c")])
    out = evaluator.process([{"message": m} for m in ("a", "d", "e", "f")])
    assert [r["oc_error"] for r in out] == [""] * 4 and out[0]["oc_cached"] == "1"
    assert len(fake_http.requests) == 6 and len(evaluator.memo) == 3


def test_kv_hits_beyond_the_memo_limit_are_all_filled(make_client, fake_http):
    backend = MemoryCache()
    fake_http.script.extend([_answers] * 6)
    rows = [{"message": str(i)} for i in range(6)]
    Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="kv", backend=backend,
              primitive_alias="oc", threads=1).process([dict(r) for r in rows])
    evaluator = Evaluator(make_client(), _primitive("oc", OVERCHARGED), cache_mode="kv", backend=backend,
                          primitive_alias="oc", memo_limit=2, threads=1)
    out = evaluator.process([dict(r) for r in rows])
    assert [r["oc_error"] for r in out] == [""] * 6 and [r["oc_cached"] for r in out] == ["1"] * 6
    assert evaluator.stats.kv_hits == 6 and len(fake_http.requests) == 6


@pytest.mark.parametrize("make", [MemoryCache, lambda: SqliteCache(os.path.join(tempfile.mkdtemp(), "c.sqlite"))])
def test_purge_deletes_only_answers_older_than_the_cutoff(make):
    cache = make()
    wire = primitive_question("noul", ["message"], OVERCHARGED)
    now = time.time()
    cache.put_many([make_doc(key, "m", wire, {"type": "noul", "noul": 0.5}, "m", 1, 1, 1, 1, created=now - age * 86400)
                    for key, age in (("old", 40), ("older", 400), ("new", 1))])
    assert cache.purge(now - 30 * 86400) == 2
    assert list(cache.get_many(["old", "older", "new"])) == ["new"]


def test_sqlite_cache_roundtrip(tmp_path):
    cache = SqliteCache(str(tmp_path / "c.sqlite"))
    wire = primitive_question("noul", ["message"], OVERCHARGED)
    doc = make_doc("k1", "m", wire, {"type": "noul", "noul": 0.25, "legend": {"x": 1}}, "jev-1.13.0", 12.5, 25, 2, 300)
    cache.put_many([doc])
    found = cache.get_many(["k1", "k2"])
    assert list(found) == ["k1"] and json.loads(found["k1"]["answer"]) == {"type": "noul", "noul": 0.25}
    assert found["k1"]["request_questions"] == 2 and cache.count() == 1
    assert "message" not in found["k1"] and "hi" not in json.dumps(found["k1"])
