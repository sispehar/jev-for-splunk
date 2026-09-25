"""Shared fixtures: a fake urlopen, a client factory and a battery directory on disk."""
from __future__ import annotations

import json

import pytest

from jev_core.client import JevClient
from jevkit import TEST_BATTERY, FakeHTTP


@pytest.fixture
def fake_http():
    return FakeHTTP()


@pytest.fixture
def make_client(fake_http):
    def _make(**kwargs):
        params = dict(api_key="test-key", endpoint="https://api.example/v1", model="jev-test", timeout=5,
                      max_retries=3, requests_per_second=0, urlopen=fake_http.urlopen, sleep=fake_http.sleep)
        params.update(kwargs)
        return JevClient(**params)
    return _make


@pytest.fixture
def app_root(tmp_path):
    default = tmp_path / "default" / "batteries"
    default.mkdir(parents=True)
    (default / "test_battery.json").write_text(json.dumps(TEST_BATTERY))
    (tmp_path / "default" / "jev.conf").write_text("[api]\nmodel = jev-conf\ntimeout = 12\n\n[defaults]\nthreads = 3\nprobs = true\n")
    return str(tmp_path)
