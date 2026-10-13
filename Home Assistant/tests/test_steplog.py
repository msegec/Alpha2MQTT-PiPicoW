"""Tests for the step log. Loads steplog.py by path so Home Assistant is not needed."""

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller" / "steplog.py"
_spec = importlib.util.spec_from_file_location("steplog", _PATH)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def test_writes_one_json_line_per_step(tmp_path):
    path = tmp_path / "logs" / "alpha2mqtt.jsonl"
    log = S.StepLog(str(path))
    log.write({"t": 1, "grid_w": 20.5, "sent": None})
    log.write({"t": 2, "grid_w": -3, "sent": {"kind": "charge", "watts": 500}})
    log.close()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{"t": 1, "grid_w": 20.5, "sent": None}, {"t": 2, "grid_w": -3, "sent": {"kind": "charge", "watts": 500}}]


def test_rotation_caps_disk_use(tmp_path):
    path = tmp_path / "steps.jsonl"
    log = S.StepLog(str(path), max_bytes=1000, backups=2)
    for t in range(500):
        log.write({"t": t, "pad": "x" * 40})
    log.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["steps.jsonl", "steps.jsonl.1", "steps.jsonl.2"]
    assert all(p.stat().st_size <= 1000 for p in tmp_path.iterdir())
    assert json.loads(path.read_text().splitlines()[-1])["t"] == 499


def test_file_name_comes_from_the_base_topic():
    assert S.file_name("Alpha2MQTT") == "alpha2mqtt.jsonl"
    assert S.file_name("Alpha2MQTT/shadow") == "alpha2mqtt_shadow.jsonl"
