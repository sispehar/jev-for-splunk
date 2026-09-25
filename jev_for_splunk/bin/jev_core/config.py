"""Configuration layering: built-in defaults <- default/jev.conf <- local/jev.conf."""
from __future__ import annotations

import configparser
import os

DEFAULTS = {
    "api": {
        "endpoint": "https://api.typesafe.ai/v1",
        "model": "jev-latest",
        "timeout": 30.0,
        "max_retries": 5,
        "requests_per_second": 15.0,
        "proxy_url": "",
        "ca_bundle": "",
    },
    "defaults": {
        "threads": 8,
        "prefix": "jev_",
        "maxevents": 5000,
        "maxstate": 16000,
        "probs": False,
    },
    "cache": {
        "mode": "kv",
        "collection": "jev_cache",
        "ttl_days": 90,
        "batch_size": 1000,
    },
    "storage": {
        "realm": "jev_for_splunk",
        "username": "typesafe_api_key",
    },
}

CACHE_MODES = ("kv", "memo", "refresh", "off")

_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off", ""}


def parse_bool(value, default=False):
    """Splunk-style boolean parsing; unknown strings fall back to `default`."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _coerce(default, raw):
    if isinstance(default, bool):
        return parse_bool(raw, default)
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return str(raw).strip()


class Config(object):
    """Flat attribute access over the layered sections.

    Attributes: endpoint, model, timeout, max_retries, requests_per_second,
    proxy_url, ca_bundle, threads, prefix, maxevents, maxstate, probs, mode,
    collection, ttl_days, batch_size, realm, username. The [cache] keys are also
    available as cache_mode, cache_collection, cache_ttl_days and
    cache_batch_size. `sources` lists the files that contributed.
    """

    def __init__(self, data, sources):
        self._data = data
        self.sources = list(sources)
        for section in data.values():
            for key, value in section.items():
                setattr(self, key, value)
        for key, value in data.get("cache", {}).items():
            setattr(self, "cache_" + key, value)

    def as_dict(self):
        return {section: dict(values) for section, values in self._data.items()}

    @property
    def systemone_url(self):
        return self.endpoint.rstrip("/") + "/systemone"

    @property
    def models_url(self):
        return self.endpoint.rstrip("/") + "/models"


def conf_paths(app_root):
    return [
        os.path.join(app_root, "default", "jev.conf"),
        os.path.join(app_root, "local", "jev.conf"),
    ]


def load_config(app_root=None, overrides=None, extra_roots=()):
    """Read jev.conf layers under `app_root` (then `extra_roots`); `overrides` is {section: {key: value}}.

    `extra_roots` lets the app that owns a search (for example a demo app that
    pins a model version) layer its own default/local jev.conf on top.
    """
    data = {section: dict(values) for section, values in DEFAULTS.items()}
    sources = []
    roots = [r for r in [app_root] + list(extra_roots) if r]
    for root in roots:
        for path in conf_paths(root):
            if not os.path.isfile(path):
                continue
            parser = configparser.RawConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            with open(path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
            for section in parser.sections():
                if section not in data:
                    continue
                for key, raw in parser.items(section):
                    if key in data[section]:
                        data[section][key] = _coerce(DEFAULTS[section][key], raw)
            sources.append(path)
    for section, values in (overrides or {}).items():
        for key, raw in values.items():
            if raw is None or section not in data or key not in data[section]:
                continue
            data[section][key] = _coerce(DEFAULTS[section][key], raw)
    if data["cache"]["mode"] not in CACHE_MODES:
        data["cache"]["mode"] = "kv"
    return Config(data, sources)
