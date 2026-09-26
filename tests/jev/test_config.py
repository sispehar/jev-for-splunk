from __future__ import annotations

import pytest

from jev_core.config import ConfigError, load_config, parse_bool


def test_defaults_without_files():
    cfg = load_config(None)
    assert cfg.endpoint == "https://api.typesafe.ai/v1" and cfg.model == "jev-latest" and cfg.threads == 8
    assert cfg.systemone_url.endswith("/v1/systemone") and cfg.sources == []


def test_layering_and_coercion(app_root):
    cfg = load_config(app_root)
    assert cfg.model == "jev-conf" and cfg.timeout == 12.0 and cfg.threads == 3 and cfg.probs is True
    assert cfg.maxevents == 5000 and len(cfg.sources) == 1
    cfg2 = load_config(app_root, overrides={"api": {"model": "jev-1.13.0"}, "defaults": {"threads": "5", "probs": "0"}})
    assert cfg2.model == "jev-1.13.0" and cfg2.threads == 5 and cfg2.probs is False


def test_bad_value_names_the_file_and_setting(tmp_path):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "jev.conf").write_text("[api]\ntimeout = thirty\n")
    with pytest.raises(ConfigError) as info:
        load_config(str(tmp_path))
    assert "local" in str(info.value) and "[api] timeout = 'thirty' is not a number" in str(info.value)


def test_parse_bool():
    assert parse_bool("TRUE") and parse_bool("t") and not parse_bool("no") and parse_bool("maybe", default=None) is None
