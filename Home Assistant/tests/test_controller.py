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


NOW = C.datetime(2026, 1, 1)


class House:
    """A controller on the net demand estimate, with entity states and a clock the test sets."""

    def __init__(self, monkeypatch):
        self.t = 0.0
        self.seq = 0
        monkeypatch.setattr(C.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(C, "dt_util", types.SimpleNamespace(utcnow=lambda: NOW))
        self.mqtt = sys.modules["homeassistant.components"].mqtt
        self.mqtt.async_publish = AsyncMock()
        self.timers = []
        monkeypatch.setattr(C, "async_call_later", self._call_later)
        self.states = {}
        hass = MagicMock(async_create_task=asyncio.run)
        hass.states.get = self.states.get
        self.c = C.Controller(hass, {"grid_source": "estimate", "correction_entity": "sensor.net_demand"}, "entry")
        self.c.enabled = True
        self.set("sensor.battery_auto_action", "discharge", soc_target=20)
        self.set("sensor.net_demand", "1000")

    def set(self, entity_id, state, **attributes):
        self.states[entity_id] = types.SimpleNamespace(state=state, attributes=attributes, last_reported=NOW)

    def _call_later(self, hass, delay, action):
        timer = types.SimpleNamespace(delay=delay, action=action, cancelled=False)
        self.timers.append(timer)
        return lambda: setattr(timer, "cancelled", True)

    def update(self, entity_id, state, **attributes):
        """An entity changes, as a template rendering a new value."""
        self.set(entity_id, state, **attributes)
        self.c._on_entity(types.SimpleNamespace(data={"entity_id": entity_id}))

    def sample(self, **fields):
        """A fresh sample with the battery at 500 W."""
        self.c._on_state(types.SimpleNamespace(payload=json.dumps(
            {C.GRID_KEY: 0, C.BATTERY_KEY: 500, "seq": self.seq, **fields})))

    def second(self, **fields):
        """One second on: a fresh sample, then the tick."""
        self.t += 1
        self.seq += 1
        self.sample(**fields)
        self.c._on_tick(None)

    def sent(self):
        return [json.loads(call.args[2]) | {"kind": call.args[1].rsplit("/", 1)[1]}
                for call in self.mqtt.async_publish.call_args_list]

    def released(self):
        return any(call.args[1].endswith("/normal") for call in self.mqtt.async_publish.call_args_list)


@pytest.mark.parametrize("entity_id", ["sensor.net_demand", "sensor.battery_auto_action"])
def test_a_gap_in_an_input_holds_the_last_command_until_stale_after(monkeypatch, entity_id):
    house = House(monkeypatch)
    house.second()
    assert house.c.status == "regulating" and house.mqtt.async_publish.called
    house.set(entity_id, "unavailable")
    for _ in range(29):
        house.second()
        assert not house.released()
    for _ in range(3):
        house.second()
    assert house.released()


def test_an_input_back_within_stale_after_keeps_regulating(monkeypatch):
    house = House(monkeypatch)
    house.second()
    house.set("sensor.net_demand", "unknown")
    house.set("sensor.battery_auto_action", "unavailable")
    for _ in range(10):
        house.second()
    house.set("sensor.net_demand", "1000")
    house.set("sensor.battery_auto_action", "discharge", soc_target=20)
    for _ in range(60):
        house.second()
    assert house.c.status == "regulating" and not house.released()


def test_a_policy_of_normal_releases_at_once(monkeypatch):
    house = House(monkeypatch)
    house.second()
    house.set("sensor.battery_auto_action", "normal")
    house.second()
    assert house.released() and house.c.status == "released"


def test_at_the_policy_soc_the_battery_is_held_at_zero_not_asked_to_discharge(monkeypatch):
    house = House(monkeypatch)
    for _ in range(10):
        house.second(REG_BATTERY_HOME_R_SOC=20.0)
    assert [(cmd["kind"], cmd["watts"]) for cmd in house.sent()] == [("charge", 0)]


# Net demand is recomputed from the same fast message as the controller's sample. Stepping on last
# second's net demand with this second's battery is out by however much the battery moved.
NEW_NET_W = 2000  # this second's: 2000 - 500 battery = 1500 W import, a 1960 W discharge
OLD_NET_W = 1000  # last second's: 500 W import, 960 W


def stepped_watts(house):
    return [cmd["watts"] for cmd in house.sent()]


def test_net_demand_already_rendered_for_the_sample_steps_at_once(monkeypatch):
    house = House(monkeypatch)
    house.seq = 7
    house.update("sensor.net_demand", str(NEW_NET_W), seq=7)
    house.sample()
    assert stepped_watts(house) == [1960] and not house.timers


def test_a_sample_waits_for_net_demand_rendered_from_it(monkeypatch):
    house = House(monkeypatch)
    house.set("sensor.net_demand", str(OLD_NET_W), seq=6)
    house.seq = 7
    house.sample()
    assert stepped_watts(house) == []
    house.update("sensor.net_demand", str(NEW_NET_W), seq=7)
    assert stepped_watts(house) == [1960]
    assert [timer.cancelled for timer in house.timers] == [True]


def test_without_matching_net_demand_the_sample_steps_on_the_last_value_soon_after(monkeypatch):
    house = House(monkeypatch)
    house.set("sensor.net_demand", str(OLD_NET_W), seq=6)
    house.seq = 7
    house.sample()
    (timer,) = house.timers
    assert timer.delay <= 0.5
    timer.action(None)
    assert stepped_watts(house) == [960]


def test_a_new_sample_steps_on_one_still_waiting_first(monkeypatch):
    house = House(monkeypatch)
    house.set("sensor.net_demand", str(OLD_NET_W), seq=6)
    house.seq = 7
    house.sample()
    house.t, house.seq = 4, 8
    house.sample()
    assert stepped_watts(house) == [960]


@pytest.mark.parametrize("net_seq, sample_seq", [(None, 7), (6, None)])
def test_without_a_seq_on_either_side_the_sample_steps_at_once(monkeypatch, net_seq, sample_seq):
    house = House(monkeypatch)
    house.set("sensor.net_demand", str(OLD_NET_W), **({} if net_seq is None else {"seq": net_seq}))
    house.seq = sample_seq
    house.sample()
    assert stepped_watts(house) == [960] and not house.timers
