import pytest

import compare
import plant as P
from synth import rows

FAST = P.Plant(dead_s=0.0, tau_discharge_s=0.01, tau_charge_s=0.01)


def test_a_variant_names_its_regulator_and_its_settings():
    assert compare.parse_variant("now:ref=HEAD") == {"name": "now", "ref": "HEAD", "path": None, "set": {}}
    assert compare.parse_variant("wip:path=/x/regulator.py, ki=0.03 ,kp=0.5") == {
        "name": "wip", "ref": None, "path": "/x/regulator.py", "set": {"ki": "0.03", "kp": "0.5"}}
    assert compare.parse_variant("tree") == {"name": "tree", "ref": None, "path": None, "set": {}}
    with pytest.raises(SystemExit):
        compare.parse_variant("bad:ki")


def test_run_scores_live_and_each_variant_per_window_and_over_all():
    a, b = rows(20, demand=500.0), rows(20, demand=900.0)
    for r in b:
        r["t"] += 100
    variants = [compare.parse_variant("now"), compare.parse_variant("slow:kp=0.2")]
    res = compare.run({"a": a, "b": b}, variants, FAST, {"min_interval_s": "0"}, None, {})
    assert set(res) == {"a", "b", "all windows"}
    assert set(res["a"]) == {"live", "now", "slow"}
    assert res["all windows"]["now"]["all"]["samples"] == 40
    assert res["all windows"]["live"]["all"]["samples"] == 40
    assert res["a"]["slow"]["all"]["cost_w"] > res["a"]["now"]["all"]["cost_w"]
    one = compare.run({"a": a}, variants[:1], FAST, {}, None, {})
    assert set(one) == {"a"}  # no "all windows" for one window


def test_overlapping_windows_count_twice_over_all():
    a = rows(10)
    res = compare.run({"a": a, "again": a}, [compare.parse_variant("now")], FAST, {}, None, {})
    assert res["all windows"]["now"]["all"]["samples"] == 20
