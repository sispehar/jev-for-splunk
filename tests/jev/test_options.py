from __future__ import annotations

import os

import pytest

from jev_core.battery import BatteryError, load_battery
from jev_core.options import FileLookupResolver, ResolveError, split_ref

HERE = os.path.dirname(os.path.abspath(__file__))
APPS = os.path.join(HERE, "fixtures", "apps")


def _apps(tmp_path, layout):
    for rel, text in layout.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return str(tmp_path)


def test_split_ref():
    assert split_ref("shop_areas") == (None, "shop_areas")
    assert split_ref("jev_shop_demo:shop_areas") == ("jev_shop_demo", "shop_areas")
    for bad in ("../etc/passwd", "a/b", "", "app:"):
        with pytest.raises(ResolveError):
            split_ref(bad)


def test_options_from_lookup_with_bom_labels_and_blank_description():
    resolver = FileLookupResolver(APPS, search_app="jev_test_app")
    options = resolver.options("lookup:areas")
    assert list(options.items()) == [("discounts_pricing", "Prices, promo codes, discounts"),
                                     ("delivery", "Shipping and tracking"), ("other", "Anything else")]
    scopes = resolver.options("lookup:jev_test_app:scopes")
    assert scopes["unclear"] is None and list(scopes)[0] == "project"


def test_levels_from_lookup_and_inline():
    resolver = FileLookupResolver(APPS, search_app="jev_test_app")
    assert resolver.levels("lookup:moods") == ["Calm question", "Mildly annoyed", "Angry"]
    assert resolver.levels("one; two") == ["one", "two"]
    with pytest.raises(ResolveError):
        resolver.levels("only one")
    with pytest.raises(ResolveError):
        resolver.levels(";".join(str(i) for i in range(11)))


def test_resolution_order_and_unique_scan(tmp_path):
    apps = _apps(tmp_path, {
        "own/lookups/x.csv": "option\nown_a\nown_b\n",
        "search/lookups/x.csv": "option\nsearch_a\nsearch_b\n",
        "other1/lookups/y.csv": "option\ny1\ny2\n",
        "other1/lookups/z.csv": "option\nz1\nz2\n",
        "other2/lookups/z.csv": "option\nz3\nz4\n",
    })
    resolver = FileLookupResolver(apps, search_app="search", own_app="own")
    assert list(resolver.options("lookup:x")) == ["search_a", "search_b"]        # the search's app wins
    assert list(resolver.options("lookup:own:x")) == ["own_a", "own_b"]         # explicit app
    assert list(resolver.options("lookup:y")) == ["y1", "y2"]                   # unique match in another app
    with pytest.raises(ResolveError) as info:
        resolver.options("lookup:z")                                           # ambiguous
    assert "several apps" in str(info.value)
    with pytest.raises(ResolveError):
        resolver.options("lookup:nope")


def test_limits_and_duplicates(tmp_path):
    apps = _apps(tmp_path, {
        "a/lookups/dup.csv": "option,description\nx,1\nx,2\n",
        "a/lookups/one.csv": "option\nonly\n",
        "a/lookups/big.csv": "option\n" + "\n".join("o%d" % i for i in range(256)) + "\n",
    })
    resolver = FileLookupResolver(apps, search_app="a", own_app="a", scan=False)
    for name in ("dup", "one", "big"):
        with pytest.raises(ResolveError):
            resolver.options("lookup:" + name)


def test_battery_resolution_across_apps_and_local_override(tmp_path):
    battery = '{"id": "b1", "state": {"t": "_raw"}, "questions": {"q": {"type": "noul", "text": "Is it?"}}}'
    override = '{"id": "b1", "version": 7, "state": {"t": "_raw"}, "questions": {"q": {"type": "noul", "text": "Is it?"}}}'
    apps = _apps(tmp_path, {"shop/default/batteries/b1.json": battery, "shop/local/batteries/b1.json": override})
    loaded = load_battery(FileLookupResolver(apps, search_app="search", own_app="own"), "b1")
    assert loaded.version == 7 and loaded.questions["q"]["instructions"] == "Regarding `t`: Is it?"
    assert load_battery(FileLookupResolver(apps, search_app="search"), "shop:b1").version == 7
    with pytest.raises(BatteryError) as info:
        load_battery(FileLookupResolver(apps, search_app="search"), "missing")
    assert "unknown battery" in str(info.value) and "b1" in str(info.value)


def test_text_form_about_must_exist_in_state():
    from jev_core.battery import battery_from_dict, validate_battery
    battery = battery_from_dict({"id": "b", "state": {"message": "message"},
                                 "questions": {"q": {"type": "noul", "text": "x", "about": ["subject"]}}})
    with pytest.raises(BatteryError):
        validate_battery(battery)
    bad_key = battery_from_dict({"id": "b", "state": {"message": "message"},
                                 "questions": {"q": {"type": "noul", "text": "x", "colour": "red"}}})
    with pytest.raises(BatteryError):
        validate_battery(bad_key)
