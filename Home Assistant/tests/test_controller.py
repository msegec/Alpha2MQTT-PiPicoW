"""Tests for the Home Assistant glue. Home Assistant is stubbed, so it is not needed."""

import asyncio
import importlib
import json
import sys
import types
from dataclasses import fields
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

for _name in ("homeassistant", "homeassistant.components", "homeassistant.config_entries", "homeassistant.core",
              "homeassistant.helpers", "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_platform",
              "homeassistant.helpers.event", "homeassistant.util", "voluptuous"):
    sys.modules.setdefault(_name, MagicMock())


class _RestoreNumber:
    async def async_added_to_hass(self):
        pass


class _Flow:
    """A config or options flow handler: the form and the result are returned as plain dicts."""

    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs

    def add_suggested_values_to_schema(self, schema, values):
        return dict(values)


sys.modules["homeassistant.components.number"] = types.SimpleNamespace(NumberMode=MagicMock(), RestoreNumber=_RestoreNumber)
sys.modules["homeassistant.config_entries"].ConfigFlow = _Flow
sys.modules["homeassistant.config_entries"].OptionsFlow = _Flow
sys.modules["homeassistant.core"].callback = lambda f: f
sys.modules["homeassistant.components"].mqtt.async_publish = AsyncMock()

_pkg = types.ModuleType("a2m_controller")
_pkg.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller")]
sys.modules["a2m_controller"] = _pkg
C = importlib.import_module("a2m_controller.controller")
F = importlib.import_module("a2m_controller.config_flow")
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
    hass = MagicMock(async_add_executor_job=AsyncMock(return_value=MagicMock()))
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


def options_flow(data):
    entry = MagicMock(entry_id="entry", data=data)
    flow = F.Alpha2MqttControllerFlow.async_get_options_flow(entry)
    flow.hass = MagicMock()
    return entry, flow


def test_the_options_form_offers_the_entry_as_it_is():
    _, flow = options_flow({"base_topic": "Alpha2MQTT", "policy_entity": "sensor.policy"})
    form = asyncio.run(flow.async_step_init())
    assert form["step_id"] == "init"
    assert form["data_schema"] == {"base_topic": "Alpha2MQTT", "policy_entity": "sensor.policy"}


def test_saved_options_replace_the_entry_data_and_reload_it():
    entry, flow = options_flow({"base_topic": "Alpha2MQTT"})
    new = {"base_topic": "Alpha2MQTT", "correction_entity": "sensor.net_demand"}
    asyncio.run(flow.async_step_init(new))
    flow.hass.config_entries.async_update_entry.assert_called_once_with(entry, data=new)
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry")


NOW = C.datetime(2026, 1, 1)


class House:
    """A controller on the net demand estimate, with entity states and a clock the test sets."""

    def __init__(self, monkeypatch):
        self.t = 0.0
        self.seq = 0
        monkeypatch.setattr(C.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(C, "dt_util", types.SimpleNamespace(utcnow=lambda: NOW, now=lambda: NOW))
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


def test_an_unknown_policy_state_releases_and_the_status_says_why(monkeypatch):
    house = House(monkeypatch)
    house.second()
    house.set("sensor.battery_auto_action", "boost")
    house.second()
    assert house.released() and house.c.status == "released: unknown policy state 'boost'"


def test_an_unknown_policy_state_is_logged_once_per_value(monkeypatch, caplog):
    house = logged_house(monkeypatch)
    house.second()
    house.set("sensor.battery_auto_action", "boost")
    for _ in range(3):
        house.second()
    house.set("sensor.battery_auto_action", "zap")
    for _ in range(2):
        house.second()
    house.set("sensor.battery_auto_action", "discharge", soc_target=20)
    house.second()
    assert house.log.kind("gap") == [
        {"what": "policy state", "missing": True, "reason": "unknown policy state 'boost'"},
        {"what": "policy state", "missing": True, "reason": "unknown policy state 'zap'"},
        {"what": "policy state", "missing": False, "reason": None}]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2 and "'boost'" in warnings[0] and "'zap'" in warnings[1]


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


class Records:
    """Stands in for the step log file."""

    def __init__(self):
        self.rows = []

    def write(self, record):
        self.rows.append(json.loads(json.dumps(record, default=str)))

    def kind(self, k):
        return [{key: v for key, v in row.items() if key not in ("k", "t")} for row in self.rows if row["k"] == k]


def logged_house(monkeypatch, enabled=True):
    house = House(monkeypatch)
    house.c.enabled = enabled
    house.log = house.c._steplog = Records()
    return house


FAST = {"REG_PV_METER_R_TOTAL_ACTIVE_POWER_1": -120, "REG_BATTERY_HOME_R_SOC": 55.0,
        "REG_DISPATCH_RW_DISPATCH_START": "Start", "ms": 1000500, "read_ms": 150}


@pytest.mark.parametrize("enabled", [True, False])
def test_every_fast_message_logs_a_step_enabled_or_not(monkeypatch, enabled):
    house = logged_house(monkeypatch, enabled)
    for _ in range(3):
        house.second(**FAST)
    steps = house.log.kind("step")
    assert [step["seq"] for step in steps] == [1, 2, 3]
    assert {k: steps[0][k] for k in ("grid", "pv", "battery", "soc", "dispatch", "ms", "read_ms")} == {
        "grid": 0, "pv": -120, "battery": 500, "soc": 55.0, "dispatch": "Start", "ms": 1000500, "read_ms": 150}
    assert steps[0]["grid_w"] == 500 and steps[0]["status"] == ("regulating" if enabled else "disabled")
    assert {"error_w", "integral_w", "ff_w", "request_w", "output_w", "load_w", "wait_ms"} <= steps[0].keys()
    sent = [step["sent"] for step in steps]
    assert sent == ([{"kind": "discharge", "watts": 960, "soc": 20, "power_only": False}, None, None] if enabled
                    else [None, None, None])


def test_a_step_that_waited_for_its_net_demand_says_how_long(monkeypatch):
    house = logged_house(monkeypatch)
    house.set("sensor.net_demand", "1000", seq=0)
    house.seq = 1
    house.sample()
    assert house.log.kind("step") == []
    house.t = 0.25
    house.update("sensor.net_demand", "2000", seq=1)
    assert house.log.kind("step")[0]["wait_ms"] == 250


def test_requests_and_responses_are_logged_raw(monkeypatch):
    house = logged_house(monkeypatch)
    house.c._on_request(types.SimpleNamespace(topic="Alpha2MQTT/request/set/discharge", payload='{"watts":300}'))
    respond(house.c, 9, "setDishargeSuccess")
    assert house.log.kind("cmd") == [{"topic": "Alpha2MQTT/request/set/discharge", "p": '{"watts":300}'}]
    (ack,) = house.log.kind("ack")
    assert ack["topic"] == "Alpha2MQTT/response/set/discharge" and json.loads(ack["p"])["seq"] == 9


def test_gaps_are_logged_where_they_start_and_end(monkeypatch):
    house = logged_house(monkeypatch)
    house.second()
    house.set("sensor.net_demand", "unavailable")
    house.second()
    house.second()
    house.set("sensor.net_demand", "1000")
    house.second()
    assert house.log.kind("gap") == [{"what": "grid estimate", "missing": True},
                                     {"what": "grid estimate", "missing": False}]


def test_a_tuning_change_logs_every_setting(monkeypatch):
    house = logged_house(monkeypatch)
    entry = MagicMock(entry_id="entry", runtime_data=house.c)
    number = N.TuningNumber(entry, *next(n for n in N.NUMBERS if n[0] == "kp"))
    number.async_write_ha_state = MagicMock()
    asyncio.run(number.async_set_native_value(0.8))
    (cfg,) = house.log.kind("cfg")
    assert cfg["kp"] == 0.8 and cfg["target_w"] == 20.0 and len(cfg) == len(fields(R.Settings))
    assert house.c.regulator.settings.kp == 0.8


def test_the_ready_floor_can_be_set_down_to_10_w():
    _, _, lo, hi, step, unit = next(n for n in N.NUMBERS if n[0] == "warm_w")
    assert lo == 0 and step <= 5 and unit == "W" and (10 - lo) % step == 0


@pytest.mark.parametrize("warm_w, logged", [(20, [{"from_w": 960, "to_w": -1000}]), (0, [])])
def test_a_turn_through_0_w_with_the_ready_floor_on_is_logged(monkeypatch, warm_w, logged):
    house = logged_house(monkeypatch)
    house.c.regulator.settings.warm_w = warm_w
    house.set("sensor.battery_auto_action", "discharge", soc_target=20, both="true", other_soc=100)
    house.second()
    house.set("sensor.net_demand", "-1000")  # a surplus the battery can store
    for _ in range(4):
        house.second()
    assert house.sent()[-1]["kind"] == "charge"
    assert house.log.kind("cross") == logged


def test_log_entities_are_logged_on_change(monkeypatch):
    house = logged_house(monkeypatch)
    state = types.SimpleNamespace(state="95.0", attributes={"unit_of_measurement": "W"})
    house.c._on_log_entity(types.SimpleNamespace(data={"entity_id": "sensor.plug_power", "new_state": state}))
    house.c._on_log_entity(types.SimpleNamespace(data={"entity_id": "sensor.plug_power", "new_state": None}))
    assert house.log.kind("e") == [{"e": "sensor.plug_power", "s": "95.0", "a": {"unit_of_measurement": "W"}},
                                   {"e": "sensor.plug_power", "s": None, "a": {}}]


def started(tmp_path, config):
    """Starts and stops a controller with a real step log, and returns its subscriptions and records."""
    mqtt = sys.modules["homeassistant.components"].mqtt
    mqtt.async_subscribe = AsyncMock(return_value=MagicMock())
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *args: f(*args))
    hass.config.path = lambda *parts: str(tmp_path.joinpath("config", *parts))
    c = C.Controller(hass, config, "entry")

    async def run():
        await c.async_start()
        await c.async_stop()

    asyncio.run(run())
    topics = [call.args[1] for call in mqtt.async_subscribe.call_args_list]
    paths = list(tmp_path.rglob("*.jsonl"))
    return topics, paths, [json.loads(line)["k"] for line in paths[0].read_text().splitlines()]


def test_start_and_stop_are_logged_with_the_settings_in_the_default_directory(tmp_path):
    topics, paths, kinds = started(tmp_path, {})
    assert "Alpha2MQTT/request/#" in topics
    assert paths == [tmp_path / "config" / "alpha2mqtt_controller" / "alpha2mqtt.jsonl"]
    assert kinds == ["start", "cfg", "stop"]


def test_the_log_directory_can_be_set(tmp_path):
    _, paths, _ = started(tmp_path, {"log_dir": str(tmp_path / "logs")})
    assert paths == [tmp_path / "logs" / "alpha2mqtt.jsonl"]


def test_a_command_with_no_response_is_sent_again_in_full(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: t[0])
    c = sent_controller()
    c.regulator.fail = MagicMock()
    c._check_ack()
    t[0] = 20
    c._check_ack()
    c.regulator.fail.assert_not_called()  # this firmware has never echoed a seq: it cannot be timed out
    respond(c, 0, "setDishargeSuccess")  # an older command's response: the firmware echoes seq
    c._check_ack()
    c.regulator.fail.assert_called_once()
    c._check_ack()
    c.regulator.fail.assert_called_once()  # given up on once, not every tick


def test_a_command_with_a_response_is_not_timed_out(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: t[0])
    c = sent_controller()
    c.regulator.fail = MagicMock()
    respond(c, 1, "setDishargeSuccess")
    t[0] = 20
    c._check_ack()
    c.regulator.fail.assert_not_called()


def test_a_command_superseded_before_its_publish_is_dropped():
    mqtt = sys.modules["homeassistant.components"].mqtt
    mqtt.async_publish = AsyncMock()
    c = C.Controller(MagicMock(), {}, "entry")
    c.regulator.update_grid(0, 520, 0)
    charge = c.regulator.step(0, DISCHARGE)
    normal = c.regulator.step(1, R.Policy(R.RELEASE))  # the switch went off before the task ran
    asyncio.run(c._publish(normal))
    asyncio.run(c._publish(charge))
    assert [call.args[1].rsplit("/", 1)[1] for call in mqtt.async_publish.call_args_list] == ["normal"]


def stepped_house(monkeypatch, plant, seconds=40):
    """2 kW of house load with the battery at 100 W: the controller steps the discharge up by about 1.9 kW."""
    house = logged_house(monkeypatch)
    house.set("sensor.net_demand", "2000")
    battery = 100.0
    for _ in range(seconds):
        house.second(REG_BATTERY_HOME_R_BATTERY_POWER=battery)
        battery = plant(battery, house.c.last_command.signed_w)
    return house


def test_a_fast_step_response_is_logged_and_not_counted_slow(monkeypatch):
    house = stepped_house(monkeypatch, lambda b, t: b + max(-650, min(650, t - b)))
    (resp,) = house.log.kind("resp")
    assert resp["result"] == "ok" and resp["t80_s"] <= 3
    assert house.c.last_response["t80_s"] == resp["t80_s"] and house.c.slow_responses_today == 0


def test_a_9_w_per_s_step_response_is_counted_slow(monkeypatch):
    house = stepped_house(monkeypatch, lambda b, t: min(t, b + 9))
    (resp,) = house.log.kind("resp")
    assert resp["result"] == "timeout" and resp["slow"]
    assert house.c.slow_responses_today == 1
