from __future__ import annotations

import urllib.error

import pytest

from jevkit import FakeResponse, http_error, noul_answer, ok_response
from jev_core.client import JevError, parse_retry_after


def test_judge_sends_bearer_and_parses(make_client, fake_http):
    fake_http.script.append(ok_response({"q": noul_answer(0.7)}, input_tokens=123))
    client = make_client()
    result = client.judge({"text": "hi"}, {"q": {"type": "noul", "instructions": "x"}})
    assert result.answers["q"]["noul"] == 0.7 and result.input_tokens == 123 and result.attempts == 1
    sent = fake_http.requests[0]
    assert sent["url"].endswith("/v1/systemone") and sent["method"] == "POST"
    assert sent["headers"]["Authorization"] == "Bearer test-key"
    assert sent["body"] == {"state": {"text": "hi"}, "model": "jev-test", "questions": {"q": {"type": "noul", "instructions": "x"}}}


def test_429_then_success_honours_retry_after(make_client, fake_http):
    fake_http.script.extend([http_error(429, {"error": "slow down"}, {"Retry-After": "2"}), ok_response({"q": noul_answer(0.1)})])
    client = make_client()
    result = client.judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert result.attempts == 2 and client.retries == 1
    assert fake_http.sleeps and fake_http.sleeps[0] >= 2.0


def test_401_marks_client_dead(make_client, fake_http):
    fake_http.script.append(http_error(401, {"error": "bad key"}))
    client = make_client()
    with pytest.raises(JevError) as info:
        client.judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert info.value.code == "auth_failed" and client.auth_dead
    with pytest.raises(JevError) as again:
        client.judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert again.value.code == "auth_failed" and len(fake_http.requests) == 1


def test_422_names_the_field(make_client, fake_http):
    fake_http.script.append(http_error(422, {"detail": [{"loc": ["body", "questions", "risk", "criteria"], "msg": "too few"}]}))
    with pytest.raises(JevError) as info:
        make_client().judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert info.value.code == "validation:questions.risk.criteria" and not info.value.retryable


def test_network_errors_retry_then_give_up(make_client, fake_http):
    fake_http.script.extend([urllib.error.URLError("timed out"), urllib.error.URLError("timed out"), ok_response({"q": noul_answer(0.5)})])
    result = make_client().judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert result.attempts == 3
    fake_http.script.extend([urllib.error.URLError("connection refused")] * 4)
    with pytest.raises(JevError) as info:
        make_client(max_retries=3).judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert info.value.code == "network:refused" and info.value.retryable


def test_5xx_retries_and_reports(make_client, fake_http):
    fake_http.script.extend([http_error(529, None)] * 2 + [ok_response({"q": noul_answer(0.5)})])
    assert make_client().judge("s", {"q": {"type": "noul", "instructions": "x"}}).attempts == 3
    fake_http.script.extend([http_error(500, None)] * 3)
    with pytest.raises(JevError) as info:
        make_client(max_retries=2).judge("s", {"q": {"type": "noul", "instructions": "x"}})
    assert info.value.code == "http_500"


def test_list_models(make_client, fake_http):
    fake_http.script.append(FakeResponse({"models": [{"name": "jev-latest", "release_date": "2026-09-01", "description": "d"}]}))
    models = make_client().list_models()
    assert models[0]["name"] == "jev-latest" and fake_http.requests[0]["method"] == "GET"


def test_missing_key_rejected(make_client):
    with pytest.raises(JevError):
        make_client(api_key="")


def test_parse_retry_after():
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("999") == 120.0
    assert parse_retry_after(None) is None and parse_retry_after("soon") is None
