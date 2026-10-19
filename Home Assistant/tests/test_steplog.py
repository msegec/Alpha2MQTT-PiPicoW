"""Tests for the step log. Loads steplog.py by path so Home Assistant is not needed."""

import datetime
import gzip
import importlib.util
import json
import os
import time
from pathlib import Path

_PATH = Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller" / "steplog.py"
_spec = importlib.util.spec_from_file_location("steplog", _PATH)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def days_ago(n):
    return datetime.date.today() - datetime.timedelta(days=n)


def written_days_ago(path, n, record):
    """A log file last written n days ago, as after Home Assistant was stopped."""
    path.write_text(json.dumps(record) + "\n")
    t = time.mktime((days_ago(n)).timetuple()) + 12 * 3600
    os.utime(path, (t, t))


def test_writes_one_json_line_per_record(tmp_path):
    path = tmp_path / "logs" / "alpha2mqtt.jsonl"
    log = S.StepLog(str(path))
    log.write({"k": "step", "t": 1, "grid_w": 20.5, "sent": None})
    log.write({"k": "e", "t": 2, "a": {"since": datetime.datetime(2026, 1, 1)}})  # attributes may hold datetimes
    log.close()
    assert lines(path) == [{"k": "step", "t": 1, "grid_w": 20.5, "sent": None},
                           {"k": "e", "t": 2, "a": {"since": "2026-01-01 00:00:00"}}]


def test_a_restart_appends(tmp_path):
    path = tmp_path / "alpha2mqtt.jsonl"
    for t in (1, 2):
        log = S.StepLog(str(path))
        log.write({"t": t})
        log.close()
    assert lines(path) == [{"t": 1}, {"t": 2}]


def test_a_closed_day_is_gzipped_under_its_date_and_today_stays_plain(tmp_path):
    path = tmp_path / "alpha2mqtt.jsonl"
    written_days_ago(path, 3, {"t": "then"})
    log = S.StepLog(str(path))
    log.write({"t": "now"})
    log.close()
    closed = tmp_path / f"alpha2mqtt.jsonl.{days_ago(3).isoformat()}.gz"
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name, closed.name]
    assert json.loads(gzip.decompress(closed.read_bytes())) == {"t": "then"}
    assert lines(path) == [{"t": "now"}]


def test_it_keeps_the_newest_days_and_leaves_other_logs_alone(tmp_path):
    for n in (5, 6, 7):
        (tmp_path / f"alpha2mqtt.jsonl.{days_ago(n).isoformat()}.gz").write_bytes(gzip.compress(b"{}\n"))
    shadow = tmp_path / f"alpha2mqtt_shadow.jsonl.{days_ago(9).isoformat()}.gz"
    shadow.write_bytes(gzip.compress(b"{}\n"))
    path = tmp_path / "alpha2mqtt.jsonl"
    written_days_ago(path, 1, {"t": 1})
    log = S.StepLog(str(path), days=2)
    log.write({"t": 2})
    log.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([
        path.name, shadow.name,
        f"alpha2mqtt.jsonl.{days_ago(1).isoformat()}.gz", f"alpha2mqtt.jsonl.{days_ago(5).isoformat()}.gz"])


def test_file_name_comes_from_the_base_topic():
    assert S.file_name("Alpha2MQTT") == "alpha2mqtt.jsonl"
    assert S.file_name("Alpha2MQTT/shadow") == "alpha2mqtt_shadow.jsonl"
