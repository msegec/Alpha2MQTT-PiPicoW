import argparse
import gzip
import json

import logs
from synth import step, write_jsonl

CFG = {"target_w": -20.0, "kp": 1.0, "ki": 0.01}


def test_loads_gz_and_plain_in_time_order_across_a_seq_reset(tmp_path):
    day1 = [{"k": "start", "t": 100.0}, {"k": "cfg", "t": 100.0, **CFG}]
    day1 += [step(101.0 + i, 5000 + i, grid_w=-20, error_w=0) for i in range(3)]
    with gzip.open(tmp_path / "alpha2mqtt.jsonl.2026-01-01.gz", "wt") as f:
        for r in day1:
            f.write(json.dumps(r) + "\n")
    # a firmware reboot: seq starts again, the same second as the last sample would be + 1
    day2 = [step(104.0, 1), step(105.0, 2), step(106.0, 3)]
    write_jsonl(tmp_path / "alpha2mqtt.jsonl", day2, bad_lines=["{not json", '{"k":"step"}', ""])
    recs, bad = logs.load([str(tmp_path / "alpha2mqtt.jsonl*")])
    assert bad == 3
    assert [r["t"] for r in recs] == sorted(r["t"] for r in recs)
    rows = logs.samples(recs)
    assert [r["seq"] for r in rows] == [5000, 5001, 5002, 1, 2, 3]
    assert [r["brk"] for r in rows] == [True, False, False, True, False, False]


def test_a_skipped_or_late_sample_breaks_the_run():
    recs = [step(0.0, 1), step(1.0, 2), step(2.0, 3, status="hold"), step(3.0, 4), step(10.0, 5)]
    rows = logs.samples(recs)
    assert [r["seq"] for r in rows] == [1, 2, 4, 5]
    assert [r["brk"] for r in rows] == [True, False, True, True]


def test_sample_fields_target_demand_and_window():
    recs = [{"k": "cfg", "t": 0.0, **CFG},
            step(1.0, 1, grid_w=100, battery=1000, error_w=150, sent={"kind": "charge", "watts": 300, "soc": 100,
                                                                       "power_only": True}),
            step(2.0, 2), step(3.0, 3)]
    rows = logs.samples(recs, t0=0.5, t1=2.5)
    assert len(rows) == 2
    r = rows[0]
    assert r["target"] == -50  # grid_w - error_w: the policy offset of -30 is in it
    assert r["cfg_target"] == -20
    assert r["deadband_w"] is None  # not in this cfg
    assert r["wait_s"] == 0.0  # synth logs wait_ms 0
    assert r["demand"] == 100 + 1000 - (0.15 * 1000 + 10)  # the default loss: 15 % plus 10 W
    assert r["sent"] == -300 and r["power_only"]
    assert logs.samples(recs, exclude=[(0.5, 2.5)])[0]["t"] == 3.0


def test_solar_falls_back_to_west_meter_plus_a_named_second_meter():
    recs = [{"k": "e", "t": 0.5, "e": "sensor.east_meter", "s": "400", "a": {}},
            step(1.0, 1, pv=-30), step(2.0, 2, pv=-5),
            {"k": "e", "t": 2.5, "e": "sensor.east_meter", "s": "unavailable", "a": {}},
            step(3.0, 3, pv=-20), step(4.0, 4, solar_w=1234.0)]
    assert [r["solar"] for r in logs.samples(recs, solar_suffix="east_meter")] == [700.0, 400.0, 200.0, 1234.0]
    # no second meter by default: west only
    assert [r["solar"] for r in logs.samples(recs)] == [300.0, 0.0, 200.0, 1234.0]


def test_entity_records_load_only_for_a_named_solar_meter(tmp_path):
    write_jsonl(tmp_path / "a.jsonl", [{"k": "e", "t": 0.5, "e": "sensor.east_meter", "s": "400", "a": {}},
                                       step(1.0, 1)])
    assert [r["k"] for r in logs.load([str(tmp_path / "a.jsonl")])[0]] == ["step"]
    assert [r["k"] for r in logs.load([str(tmp_path / "a.jsonl")], solar_suffix="east_meter")[0]] == ["e", "step"]


def test_a_row_carries_every_dispatch_request_until_the_next_step():
    recs = [step(1.0, 1, sent={"kind": "discharge", "watts": 500, "soc": 10, "power_only": False}),
            {"k": "cmd", "t": 1.05, "topic": "A/request/set/discharge", "p": '{"watts":500}'},
            {"k": "cmd", "t": 1.6, "topic": "A/request/set/charge", "p": '{"watts":100}'},  # a tick command
            {"k": "cmd", "t": 1.7, "topic": "A/request/read/register/raw", "p": "{}"},
            step(2.0, 2), step(3.0, 3, status="hold"),
            {"k": "cmd", "t": 3.1, "topic": "A/request/set/normal", "p": "{}"},  # after a step not kept
            step(4.0, 4)]
    assert [r["cmds"] for r in logs.samples(recs)] == [[500.0, -100.0], [], []]


def test_reads_and_raw_writes_are_not_commands():
    recs = [{"k": "cmd", "t": 1.0, "topic": "A/request/read/register/raw", "p": "{}"},
            {"k": "cmd", "t": 2.0, "topic": "A/request/set/discharge", "p": '{"watts":500,"seq":1}'},
            {"k": "cmd", "t": 3.0, "topic": "A/request/set/charge", "p": '{"watts":200,"powerOnly":1}'},
            {"k": "cmd", "t": 4.0, "topic": "A/request/set/normal", "p": "{}"},
            {"k": "ack", "t": 4.1, "topic": "A/response/set/normal", "p": "{}"},
            {"k": "cmd", "t": 5.0, "topic": "A/request/set/charge", "p": "garbage"}]
    assert logs.commands(recs) == [(2.0, 500.0, False), (3.0, -200.0, True), (4.0, None, False)]


def test_the_learned_table_in_force_and_from_a_store_file(tmp_path):
    a, b = [[0.0, 0.0], [500.0, 12.0]], [[0.0, 0.0], [500.0, 15.0]]
    write_jsonl(tmp_path / "x.jsonl", [{"k": "trim_load", "t": 1.0, "loaded": True, "points": a},
                                       {"k": "trim", "t": 500.0, "points": b}, step(1000.0, 1)])
    recs, _ = logs.load([str(tmp_path / "x.jsonl")], 900.0, 1100.0)  # both kept though before the window
    assert logs.trim_at(recs, 0.5) is None
    assert logs.trim_at(recs, 100.0) == a
    assert logs.trim_at(recs, 1000.0) == b
    (tmp_path / "store").write_text(json.dumps({"version": 1, "key": "k", "data": {"points": a}}))
    (tmp_path / "plain").write_text(json.dumps({"points": b}))
    assert logs.read_trim(str(tmp_path / "store")) == a
    assert logs.read_trim(str(tmp_path / "plain")) == b


def test_iterations_split_on_code_and_cfg_changes():
    recs = [{"k": "start", "t": 0.0, "code": "aaa"}, {"k": "cfg", "t": 0.0, **CFG},
            {"k": "start", "t": 10.0, "code": "aaa"}, {"k": "cfg", "t": 10.0, **CFG},  # restart, same code
            {"k": "cfg", "t": 20.0, **{**CFG, "ki": 0.03}},
            {"k": "start", "t": 30.0, "code": "bbb"}, {"k": "cfg", "t": 30.0, **{**CFG, "ki": 0.03}},
            {"k": "start", "t": 40.0}, {"k": "cfg", "t": 40.0, **{**CFG, "ki": 0.03}}]  # no code: always splits
    its = logs.iterations(recs)
    assert [(i["t0"], i["why"], i["code"], i["cfg"]["ki"]) for i in its] == [
        (0.0, "start", "aaa", 0.01), (20.0, "cfg", "aaa", 0.03), (30.0, "start", "bbb", 0.03),
        (40.0, "start", None, 0.03)]
    assert its[0]["t1"] == 10.0


def test_the_loss_is_a_share_plus_a_fixed_term_at_every_reading():
    loss = logs.Loss()
    assert (loss.share, loss.fixed_w) == (0.15, 10.0)
    assert loss(-1000) == loss(1000) == 160.0
    assert loss(0) == 10.0  # the correction template adds the fixed term whatever the battery reads
    recs = [step(1.0, 1, grid_w=100, battery=1000), step(2.0, 2, grid_w=100, battery=0)]
    assert [r["demand"] for r in logs.samples(recs, loss=logs.Loss(0, 0))] == [1100, 100]
    assert [r["demand"] for r in logs.samples(recs)] == [940, 90]


def test_the_loss_is_a_cli_parameter():
    p = argparse.ArgumentParser()
    logs.add_window_args(p)
    assert logs.loss_from_args(p.parse_args(["x"])) == logs.Loss()
    assert logs.loss_from_args(p.parse_args(["x", "--loss-share", "0", "--loss-w", "0"])) == logs.Loss(0, 0)
