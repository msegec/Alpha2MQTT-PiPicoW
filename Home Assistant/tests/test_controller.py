"""Tests for the Home Assistant glue. Home Assistant is stubbed, so it is not needed."""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

for _name in ("homeassistant", "homeassistant.components", "homeassistant.config_entries", "homeassistant.core",
              "homeassistant.helpers", "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform",
              "homeassistant.helpers.event", "homeassistant.util"):
    sys.modules.setdefault(_name, MagicMock())


class _RestoreNumber:
    async def async_added_to_hass(self):
        pass


sys.modules["homeassistant.components.number"] = types.SimpleNamespace(NumberMode=MagicMock(), RestoreNumber=_RestoreNumber)
sys.modules["homeassistant.core"].callback = lambda f: f
sys.modules["homeassistant.components"].mqtt.async_publish = AsyncMock()

_pkg = types.ModuleType("a2m_controller")
_pkg.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller")]
sys.modules["a2m_controller"] = _pkg
C = importlib.import_module("a2m_controller.controller")
N = importlib.import_module("a2m_controller.number")
R = sys.modules["a2m_controller.regulator"]
DISCHARGE = R.Policy(R.REGULATE, max_discharge_w=3000, discharge_soc=20)


def sent_controller():
    """A controller that has published one discharge command, seq 1."""
    c = C.Controller(MagicMock(), {}, "entry")
    c.regulator.update_grid(0, 20, 500)
    cmd = c.regulator.step(0, DISCHARGE)
    asyncio.run(c._publish(cmd))
    c.regulator.ack = MagicMock()
    return c


def respond(c, seq, status):
    c._on_response(types.SimpleNamespace(topic="Alpha2MQTT/response/set/discharge",
                                         payload=json.dumps({"responseStatus": status, "seq": seq})))


def test_success_for_the_last_seq_acks():
    c = sent_controller()
    respond(c, 1, "setDishargeSuccess")  # spelled as the firmware spells it
    c.regulator.ack.assert_called_once()


def test_old_seq_failure_or_no_seq_does_not_ack():
    c = sent_controller()
    respond(c, 0, "setDishargeSuccess")
    respond(c, 1, "noResponse")
    respond(c, None, "setDishargeSuccess")
    c.regulator.ack.assert_not_called()


def test_no_ack_once_a_newer_command_is_pending():
    # The regulator has chosen the next command but its publish has not finished: seq 1 is not its ack.
    c = sent_controller()
    c.regulator.update_grid(3, 1020, 500)
    assert c.regulator.step(3, DISCHARGE) is not None
    respond(c, 1, "setDishargeSuccess")
    c.regulator.ack.assert_not_called()


def restored_min_interval(value):
    entry = MagicMock(entry_id="entry", runtime_data=C.Controller(MagicMock(), {}, "entry"))
    number = N.TuningNumber(entry, *next(n for n in N.NUMBERS if n[0] == "min_interval_s"))
    number.async_get_last_number_data = AsyncMock(return_value=MagicMock(native_value=value))
    asyncio.run(number.async_added_to_hass())
    return entry.runtime_data.regulator.settings.min_interval_s


def test_min_interval_restores_in_half_seconds_from_2_s():
    assert restored_min_interval(2.5) == 2.5
    assert restored_min_interval(1.0) == 2  # saved under the old 1 s minimum


def subscribed_state_topic(config):
    mqtt = sys.modules["homeassistant.components"].mqtt
    mqtt.async_subscribe = AsyncMock()
    hass = MagicMock(async_add_executor_job=AsyncMock())
    asyncio.run(C.Controller(hass, config, "entry").async_start())
    return mqtt.async_subscribe.call_args_list[0].args[1]


def published(cmd):
    mqtt = sys.modules["homeassistant.components"].mqtt
    mqtt.async_publish = AsyncMock()
    asyncio.run(C.Controller(MagicMock(), {}, "entry")._publish(cmd))
    return json.loads(mqtt.async_publish.call_args.args[2])


def test_power_only_commands_carry_the_flag_and_others_do_not():
    assert published(R.Command("discharge", 800, 20, 120, power_only=True))["powerOnly"] == 1
    assert "powerOnly" not in published(R.Command("discharge", 800, 20, 120))


def respond_write(c, write):
    payload = {"responseStatus": "setDishargeSuccess", "seq": 1}
    if write is not None:
        payload["write"] = write
    c._on_response(types.SimpleNamespace(topic="Alpha2MQTT/response/set/discharge", payload=json.dumps(payload)))


@pytest.mark.parametrize("write, power_write", [("power", True), ("block", False), (None, False)])
def test_the_ack_says_whether_only_the_power_was_written(write, power_write):
    c = sent_controller()
    respond_write(c, write)
    assert c.regulator.ack.call_args.kwargs == {"power_write": power_write}
    assert c.ack["ack_write"] == write


def test_a_failed_write_of_the_newest_command_is_retried():
    c = sent_controller()
    c.regulator.fail = MagicMock()
    respond(c, 0, "noResponse")  # an older command
    c.regulator.fail.assert_not_called()
    respond(c, 1, "noResponse")
    c.regulator.fail.assert_called_once()


def test_state_topic_defaults_to_the_fast_state_and_keeps_an_explicit_one():
    assert subscribed_state_topic({}) == "Alpha2MQTT/state/fast"
    assert subscribed_state_topic({"state_topic": "Alpha2MQTT/state/second/ten"}) == "Alpha2MQTT/state/second/ten"
