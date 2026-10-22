"""Tests for the Home Assistant glue. Home Assistant is stubbed, so it is not needed."""

import asyncio
import hashlib
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
              "homeassistant.helpers.event", "homeassistant.helpers.typing", "homeassistant.util", "homeassistant.const",
              "voluptuous"):
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
sys.modules["homeassistant.components.sensor"] = types.SimpleNamespace(
    SensorDeviceClass=MagicMock(), SensorEntity=object, SensorStateClass=MagicMock())
sys.modules["homeassistant.components.switch"] = types.SimpleNamespace(SwitchEntity=type("SwitchEntity", (), {}))
sys.modules["homeassistant.helpers.restore_state"] = types.SimpleNamespace(RestoreEntity=type("RestoreEntity", (), {}))
sys.modules["homeassistant.components.button"] = types.SimpleNamespace(ButtonEntity=type("ButtonEntity", (), {}))
sys.modules["homeassistant.exceptions"] = types.SimpleNamespace(ConfigEntryNotReady=type("ConfigEntryNotReady", (Exception,), {}))


class _Store:
    """Home Assistant's Store, in memory: data is what is on disk, delayed a save waiting to run."""

    def __init__(self, hass, version, key):
        self.key, self.data, self.delayed = key, None, None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data, self.delayed = data, None

    def async_delay_save(self, func, delay):
        self.delayed = (func, delay)


sys.modules["homeassistant.helpers.storage"] = types.SimpleNamespace(Store=_Store)
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
S = importlib.import_module("a2m_controller.sensor")
W = importlib.import_module("a2m_controller.switch")
B = importlib.import_module("a2m_controller.button")
I = importlib.import_module("a2m_controller.__init__")
R = sys.modules["a2m_controller.regulator"]
DISCHARGE = R.Policy(R.REGULATE, max_discharge_w=3000, discharge_soc=20)


def sent_controller():
    """A controller that has published one discharge command, its seq in last_seq."""
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
    respond(c, c.last_seq, "setDishargeSuccess")  # spelled as the firmware spells it
    c.regulator.ack.assert_called_once()


@pytest.mark.parametrize("status", ["setChargeSuccess", "setDishargeSuccess", "setNormalSuccess"])
def test_each_set_success_status_of_the_firmware_acks(status):
    c = sent_controller()
    c.regulator.fail = MagicMock()
    respond(c, c.last_seq, status)
    c.regulator.ack.assert_called_once()
    c.regulator.fail.assert_not_called()


# A failed write sends the register write's own status, and nothing stops another sender using "success" in a word.
@pytest.mark.parametrize("status", ["slaveError", "invalidMQTTPayload", "writeDataRegisterSuccess", "unsuccessful", ["setChargeSuccess"],
                                    None])
def test_any_other_status_fails_the_command(status):
    c = sent_controller()
    c.regulator.fail = MagicMock()
    respond(c, c.last_seq, status)
    c.regulator.ack.assert_not_called()
    c.regulator.fail.assert_called_once()


def test_old_seq_failure_or_no_seq_does_not_ack():
    c = sent_controller()
    respond(c, 0, "setDishargeSuccess")
    respond(c, c.last_seq, "noResponse")
    respond(c, None, "setDishargeSuccess")
    c.regulator.ack.assert_not_called()


def test_no_ack_once_a_newer_command_is_pending():
    # The regulator has chosen the next command but its publish has not finished: seq 1 is not its ack.
    c = sent_controller()
    c.regulator.update_grid(3, 1020, 500)
    assert c.regulator.step(3, DISCHARGE) is not None
    respond(c, c.last_seq, "setDishargeSuccess")
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


def test_a_charge_to_100_sends_the_full_register():
    assert published(R.Command("charge", 800, 100, 120))["socPercent"] == 102  # register 252
    assert published(R.Command("charge", 800, 105, 120))["socPercent"] == 105  # balance stays 255
    assert published(R.Command("charge", 800, 80, 120))["socPercent"] == 80
    assert published(R.Command("discharge", 800, 100, 120))["socPercent"] == 100


def seqs_published(c, n):
    mqtt = sys.modules["homeassistant.components"].mqtt
    mqtt.async_publish = AsyncMock()
    for _ in range(n):
        asyncio.run(c._publish(R.Command("discharge", 800, 20, 120)))
    return [json.loads(call.args[2])["seq"] for call in mqtt.async_publish.call_args_list]


def test_a_reload_never_reuses_a_seq_of_the_run_before(monkeypatch):
    # A response from before a reload can arrive after it. Its seq must not be one the new controller has sent.
    clock = [1_790_000_000.0]
    monkeypatch.setattr(C.time, "time", lambda: clock[0])
    old = seqs_published(C.Controller(MagicMock(), {}, "entry"), 120)  # two a second for a minute
    clock[0] += 60
    c = C.Controller(MagicMock(), {}, "entry")
    new = seqs_published(c, 3)
    assert min(new) > max(old)
    c.regulator.ack = MagicMock()
    c.regulator.sent = c.last_command
    respond(c, old[0], "setDishargeSuccess")
    c.regulator.ack.assert_not_called()


def test_a_seq_stays_what_the_firmware_echoes():
    # The firmware keeps only 0-9a-fx- of a value, reads seq with strtoul into a 32-bit unsigned long and echoes it
    # as %lu. A seq is a whole number in 1..SEQ_MAX, so it comes back unchanged and 0 (no seq) is never sent.
    c = C.Controller(MagicMock(), {}, "entry")
    assert 1 <= c._cmd_seq + 1 <= C.SEQ_MAX == 2**32 - 1
    c._cmd_seq = C.SEQ_MAX - 1
    assert seqs_published(c, 2) == [C.SEQ_MAX, 1]
    c._cmd_seq = C.SEQ_MAX - 1
    mqtt = sys.modules["homeassistant.components"].mqtt
    asyncio.run(c._publish(R.Command("discharge", 20000, 105, 600, power_only=True)))
    assert len(mqtt.async_publish.call_args.args[2]) < 90  # the Pico reads at most 127 bytes


def respond_write(c, write):
    payload = {"responseStatus": "setDishargeSuccess", "seq": c.last_seq}
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
    respond(c, c.last_seq, "noResponse")
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


def user_flow():
    flow = F.Alpha2MqttControllerFlow()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


def test_the_user_step_shows_the_form_then_makes_one_entry_per_base_topic():
    flow = user_flow()
    form = asyncio.run(flow.async_step_user())
    assert form["step_id"] == "user" and form["data_schema"] is F.SCHEMA
    data = {"base_topic": "Alpha2MQTT/shadow", "state_topic": "Alpha2MQTT/state/fast"}
    assert asyncio.run(flow.async_step_user(data)) == {"title": "Battery controller", "data": data}
    flow.async_set_unique_id.assert_awaited_once_with("Alpha2MQTT/shadow")
    flow._abort_if_unique_id_configured.assert_called_once_with()


def test_an_import_from_yaml_updates_the_entry_for_its_base_topic():
    flow = user_flow()
    data = {"base_topic": "Alpha2MQTT", "grid_scale": 1.0}
    assert asyncio.run(flow.async_step_import(data)) == {"title": "Battery controller", "data": data}
    flow.async_set_unique_id.assert_awaited_once_with("Alpha2MQTT")
    flow._abort_if_unique_id_configured.assert_called_once_with(updates=data)


def test_yaml_starts_an_import_and_no_yaml_starts_nothing():
    hass = MagicMock()
    assert asyncio.run(I.async_setup(hass, {})) is True
    hass.async_create_task.assert_not_called()
    assert asyncio.run(I.async_setup(hass, {C.DOMAIN: {"base_topic": "Alpha2MQTT"}})) is True
    hass.config_entries.flow.async_init.assert_called_once_with(
        C.DOMAIN, context={"source": I.SOURCE_IMPORT}, data={"base_topic": "Alpha2MQTT"})
    hass.async_create_task.assert_called_once()


def test_setup_waits_for_mqtt_and_starts_after_the_entities(monkeypatch):
    mqtt = sys.modules["homeassistant.components"].mqtt
    mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=False)
    hass, entry = MagicMock(), MagicMock(entry_id="entry", data={"base_topic": "Alpha2MQTT"})
    with pytest.raises(sys.modules["homeassistant.exceptions"].ConfigEntryNotReady):
        asyncio.run(I.async_setup_entry(hass, entry))
    order = []
    mqtt.async_wait_for_mqtt_client = AsyncMock(return_value=True)
    hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=lambda e, p: order.append(("forward", p)))
    monkeypatch.setattr(C.Controller, "async_start", AsyncMock(side_effect=lambda: order.append("start")))
    assert asyncio.run(I.async_setup_entry(hass, entry)) is True
    assert isinstance(entry.runtime_data, C.Controller) and entry.runtime_data.config == entry.data
    assert entry.runtime_data.config is not entry.data  # a copy: the options flow replaces the entry's data
    assert order == [("forward", I.PLATFORMS), "start"]


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
    assert steps[0]["limit"] == "none" and steps[0]["solar_w"] is None
    assert {"error_w", "integral_w", "ff_w", "request_w", "output_w", "offset_w", "solar_w", "wait_ms"} <= steps[0].keys()
    sent = [step["sent"] for step in steps]
    assert sent == ([{"kind": "discharge", "watts": 960, "soc": 20, "power_only": False}, None, None] if enabled
                    else [None, None, None])


def test_solar_comes_from_the_net_demand_attributes_and_feeds_forward_its_change(monkeypatch):
    house = logged_house(monkeypatch)
    house.c.set_setting("kff", 1.0)
    house.set("sensor.net_demand", "1000", west=300, east=200.5)
    house.second()
    house.set("sensor.net_demand", "700", west="600", east=200.5)  # 300 W more solar
    house.second()
    for west, east in ((None, 200), ("unknown", 200), (300, None)):
        house.set("sensor.net_demand", "700", **{k: v for k, v in (("west", west), ("east", east)) if v is not None})
        house.second()
    steps = house.log.kind("step")
    assert [(step["solar_w"], step["ff_w"]) for step in steps] == [
        (500.5, 0), (800.5, -300), (None, 0), (None, 0), (None, 0)]


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


def test_a_whole_number_setting_stays_a_whole_number(monkeypatch):
    # Home Assistant restores a number as a float, and the firmware strips a "." from a value: 120.0 would be 1200.
    house = House(monkeypatch)
    house.c.set_setting("duration_s", 130.0)
    house.c.set_setting("full_register", 251.0)
    assert type(house.c.regulator.settings.duration_s) is int and house.c.regulator.settings.full_register == 251
    house.second()
    assert house.sent()[-1]["duration"] == 130 and '"duration":130,' in house.mqtt.async_publish.call_args.args[2]


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
    _, _, lo, hi, step, unit, _ = next(n for n in N.NUMBERS if n[0] == "warm_w")
    assert lo == 0 and step <= 5 and unit == "W" and (10 - lo) % step == 0


def test_every_number_names_its_section_as_an_attribute():
    entry = MagicMock(entry_id="entry", runtime_data=C.Controller(MagicMock(), {}, "entry"))
    for row in N.NUMBERS:
        assert row[6] in (N.BASIC, N.SAFETY, N.ADVANCED), row
        assert N.TuningNumber(entry, *row)._attr_extra_state_attributes == {"group": row[6]}
    assert {n[0]: n[6] for n in N.NUMBERS}["target_w"] == "Basic"


def test_every_setting_has_one_entity_whose_range_holds_its_default():
    keys = [n[0] for n in N.NUMBERS] + [w[0] for w in W.SWITCHES]
    assert sorted(keys) == sorted(f.name for f in fields(R.Settings))
    for key, _name, lo, hi, _step, _unit, _group in N.NUMBERS:
        assert lo <= getattr(R.Settings(), key) <= hi, key


def setting_switch(c, key, last_state=None):
    entry = MagicMock(entry_id="entry", runtime_data=c)
    switch = W.SettingSwitch(entry, *next(w for w in W.SWITCHES if w[0] == key))
    switch.async_get_last_state = AsyncMock(return_value=None if last_state is None else MagicMock(state=last_state))
    switch.async_write_ha_state = MagicMock()
    asyncio.run(switch.async_added_to_hass())
    return switch


@pytest.mark.parametrize("last_state, on", [(None, False), ("on", True), ("off", False), ("unavailable", False)])
def test_a_setting_switch_restores_its_last_state_or_the_default(last_state, on):
    c = C.Controller(MagicMock(), {}, "entry")
    switch = setting_switch(c, "ff_guard", last_state)
    assert c.regulator.settings.ff_guard is on and switch.is_on is on


def test_a_setting_switch_writes_the_setting_logs_it_and_names_its_section(monkeypatch):
    house = logged_house(monkeypatch)
    switch = setting_switch(house.c, "ff_guard")
    asyncio.run(switch.async_turn_on())
    assert house.c.regulator.settings.ff_guard is True and house.log.kind("cfg")[-1]["ff_guard"] is True
    asyncio.run(switch.async_turn_off())
    assert house.c.regulator.settings.ff_guard is False
    for row in W.SWITCHES:
        assert row[2] in (N.BASIC, N.SAFETY, N.ADVANCED)
        assert isinstance(getattr(R.Settings(), row[0]), bool), row
    assert switch._attr_extra_state_attributes == {"group": "Basic"}


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


def test_a_refusal_near_the_floor_and_its_rearm_are_logged(monkeypatch):
    house = logged_house(monkeypatch)
    for _ in range(25):
        house.second(**{C.SOC_KEY: 22.0, C.BATTERY_KEY: 0})
    assert house.log.kind("refused") == [{"soc": 22.0, "floor": 20}]
    house.second(**{C.SOC_KEY: 23.0, C.BATTERY_KEY: 0})
    assert house.log.kind("rearmed") == [{"soc": 23.0}]


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


def test_the_start_record_and_the_loop_version_sensor_name_the_loop_code(tmp_path):
    started(tmp_path, {})
    (path,) = tmp_path.rglob("*.jsonl")
    start = json.loads(path.read_text().splitlines()[0])
    folder = Path(C.__file__).parent
    code = hashlib.sha256((folder / "regulator.py").read_bytes() + (folder / "controller.py").read_bytes())
    assert start == {"k": "start", "t": start["t"], "code": code.hexdigest()[:12]}
    c = C.Controller(MagicMock(), {}, "entry")
    c.code = "abc"
    key, *rest = next(s for s in S.SENSORS if s[0] == "code")
    sensor = S.ControllerSensor(MagicMock(runtime_data=c), key, *rest)
    assert (sensor._attr_name, sensor._value(c)) == ("Loop version", "abc")
    assert sensor._attr_entity_category == S.EntityCategory.DIAGNOSTIC


def learned(c, w=500.0, y=-30.0):
    """A table with y W learned at the w W node, as the regulator's points."""
    points = c.regulator.trim_points()
    points[R.TRIM_NODES.index(w)][1] = y
    return points


def test_a_saved_table_of_learned_offsets_loads_at_start_and_is_logged(monkeypatch):
    house = logged_house(monkeypatch)
    house.c._store.data = {"points": learned(house.c)}
    asyncio.run(house.c._load_trim())
    assert house.c.regulator.trim[R.TRIM_NODES.index(500.0)] == -30.0
    (record,) = house.log.kind("trim_load")
    assert record == {"loaded": True, "points": learned(house.c)}


def test_a_bad_saved_table_loads_nothing_and_none_saved_logs_nothing(monkeypatch):
    house = logged_house(monkeypatch)
    asyncio.run(house.c._load_trim())
    assert house.log.kind("trim_load") == []
    house.c._store.data = {"points": learned(house.c, w=0.0, y=30.0)}  # a 0 W node that is not 0
    asyncio.run(house.c._load_trim())
    assert not any(house.c.regulator.trim)
    assert house.log.kind("trim_load")[0]["loaded"] is False


def test_an_unreadable_store_starts_from_an_empty_table(monkeypatch):
    house = logged_house(monkeypatch)

    async def broken():
        raise OSError("disk")
    house.c._store.async_load = broken
    asyncio.run(house.c._load_trim())
    assert not any(house.c.regulator.trim)
    assert house.log.kind("trim_load")[0]["loaded"] is False


def test_lowering_the_learned_offset_limit_clips_the_table_at_once(monkeypatch):
    house = logged_house(monkeypatch)
    house.c.regulator.load_trim(learned(house.c, y=-90.0))
    house.c.set_setting("trim_limit_w", 50.0)
    assert house.c.regulator.trim[R.TRIM_NODES.index(500.0)] == -50.0


def test_a_moved_table_is_saved_later_and_logged_at_most_once_a_minute(monkeypatch):
    house = logged_house(monkeypatch)
    house.second()
    assert house.c._store.delayed is None and house.log.kind("trim") == []
    house.c.regulator.trim = [y for _, y in learned(house.c)]
    house.c.regulator.trim_moves += 1
    house.second()
    func, delay = house.c._store.delayed
    assert delay == C.TRIM_SAVE_S and func() == {"points": learned(house.c)}
    for _ in range(30):
        house.c.regulator.trim_moves += 1
        house.second()
    assert len(house.log.kind("trim")) == 1
    house.t += 60
    house.c.regulator.trim_moves += 1
    house.second()
    assert len(house.log.kind("trim")) == 2 and house.log.kind("trim")[-1]["points"] == learned(house.c)


def test_a_stop_saves_a_table_still_waiting_to_be_saved(monkeypatch):
    house = logged_house(monkeypatch)
    house.c.regulator.trim = [y for _, y in learned(house.c)]
    house.c.regulator.trim_moves += 1
    house.second()
    house.c._steplog = None
    asyncio.run(house.c.async_stop())
    assert house.c._store.data == {"points": learned(house.c)}


def test_the_forget_button_zeroes_the_table_saves_and_logs_it(monkeypatch):
    house = logged_house(monkeypatch)
    house.c.regulator.load_trim(learned(house.c))
    button = B.ForgetTrimButton(MagicMock(entry_id="entry", runtime_data=house.c))
    assert button._attr_name == "Forget learned offsets" and button._attr_extra_state_attributes == {"group": "Basic"}
    asyncio.run(button.async_press())
    assert not any(house.c.regulator.trim)
    assert house.c._store.data == {"points": house.c.regulator.trim_points()}
    assert house.log.kind("trim")[-1] == {"points": house.c.regulator.trim_points(), "forgot": True}


def test_the_integral_sensor_carries_the_learned_offsets_for_a_chart(monkeypatch):
    house = House(monkeypatch)
    house.c.regulator.load_trim(learned(house.c))
    assert attributes(house.c, "integral") == {"learned_offsets": learned(house.c)}


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
    respond(c, c.last_seq, "setDishargeSuccess")
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


def test_a_step_with_no_samples_after_it_ends_cancelled_by_the_stale_release(monkeypatch):
    # The timeout is checked on a sample. With none, the regulator goes stale and its release cancels the step, so
    # missing data is not counted as a slow battery.
    house = stepped_house(monkeypatch, lambda b, t: b, seconds=1)
    for _ in range(int(house.c.regulator.settings.stale_after_s) + 2):
        house.t += 1
        house.c._on_tick(None)
    assert house.released()
    assert [r["result"] for r in house.log.kind("resp")] == ["cancelled"] and house.c.slow_responses_today == 0


def test_a_9_w_per_s_step_response_is_counted_slow(monkeypatch):
    house = stepped_house(monkeypatch, lambda b, t: min(t, b + 9))
    (resp,) = house.log.kind("resp")
    assert resp["result"] == "timeout" and resp["slow"]
    assert house.c.slow_responses_today == 1


# Diagnosis attributes. They change only at edges or ride on a power sensor, so they add no state writes of their own.

def attributes(c, key):
    return next(s for s in S.SENSORS if s[0] == key)[4](c)


def ago(seconds):
    return (NOW - C.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_the_status_says_how_long_the_policy_has_been_unreadable(monkeypatch):
    house = House(monkeypatch)
    house.second()
    assert attributes(house.c, "status") == {"policy_gap_since": None, "sample_gap_since": None}
    house.set("sensor.battery_auto_action", "unavailable")
    for _ in range(5):
        house.second()
    assert attributes(house.c, "status")["policy_gap_since"] == ago(5)  # the last good read, 5 s ago
    house.second()
    assert attributes(house.c, "status")["policy_gap_since"] == ago(5)  # worked out once: the text does not move
    house.set("sensor.battery_auto_action", "discharge", soc_target=20)
    house.second()
    assert attributes(house.c, "status")["policy_gap_since"] is None


def test_the_status_says_when_the_last_sample_came_once_it_is_late(monkeypatch):
    house = House(monkeypatch)
    house.second()
    house.t += 2.5
    house.c._on_tick(None)
    assert attributes(house.c, "status")["sample_gap_since"] is None  # within SAMPLE_LATE_S
    house.t += 1.5
    house.c._on_tick(None)
    assert attributes(house.c, "status")["sample_gap_since"] == ago(4)
    house.second()
    assert attributes(house.c, "status")["sample_gap_since"] is None


def test_the_output_shows_the_proportional_term_and_the_output_before_the_slew(monkeypatch):
    house = House(monkeypatch)
    house.second()  # grid 1000 - 500 = 500 W, 460 W off target after the deadband, from 500 W
    assert attributes(house.c, "output") == {"request": 960, "integral": 0, "feed_forward": 0, "offset": 0, "p": 460,
                                             "u": 960, "slewed": False}


def test_the_limit_shows_the_caps_the_direction_and_the_refusal_clock(monkeypatch):
    house = House(monkeypatch)
    house.second(**{C.SOC_KEY: 50.0})
    assert attributes(house.c, "limit") == {"lo": 0, "hi": 3000, "direction": 1, "direction_since": None,
                                            "refusal_since": None, "refused_soc": None}
    for _ in range(3):
        house.second(**{C.SOC_KEY: 22.0, C.BATTERY_KEY: 0})  # asked for 960 W, giving nothing
    assert attributes(house.c, "limit")["refusal_since"] == ago(2)
    for _ in range(22):
        house.second(**{C.SOC_KEY: 22.0, C.BATTERY_KEY: 0})
    limit = attributes(house.c, "limit")
    assert (limit["refused_soc"], limit["refusal_since"], limit["hi"]) == (22.0, None, 0)


def gated_sensor(monkeypatch, power, attributes=None):
    """A sensor on a value the test sets, its writes counted, on a clock the test sets."""
    clock, value = [0.0], [None]
    monkeypatch.setattr(S.time, "monotonic", lambda: clock[0])
    sensor = S.ControllerSensor(MagicMock(runtime_data=C.Controller(MagicMock(), {}, "entry")), "x", "X", power,
                                lambda c: value[0], attributes)
    sensor.async_write_ha_state = MagicMock()

    def write(v, at):
        clock[0], value[0] = at, v
        sensor._update()
        return sensor.async_write_ha_state.call_count
    return write


def test_a_power_sensor_writes_on_a_25_w_move_or_after_10_s(monkeypatch):
    write = gated_sensor(monkeypatch, power=True)
    assert write(100, 0) == 1
    assert write(100 + S.MOVE_W - 1, 1) == 1  # a small move soon after: no write
    assert write(100 + S.MOVE_W, 2) == 2
    assert write(126, 2 + S.EVERY_S - 0.1) == 2
    assert write(126, 2 + S.EVERY_S) == 3  # the same value once EVERY_S is up
    assert write(None, 13) == 4  # unknown always writes


def test_a_sensor_without_attributes_writes_on_change_and_one_with_them_on_every_update(monkeypatch):
    write = gated_sensor(monkeypatch, power=False)
    assert [write("regulating", t) for t in (0, 20, 40)] == [1, 1, 1]
    assert write("stale", 41) == 2
    write = gated_sensor(monkeypatch, power=False, attributes=lambda c: {"a": 1})
    assert [write("regulating", t) for t in (0, 1)] == [1, 2]  # Home Assistant drops an unchanged write


def test_commands_in_the_last_hour_counts_publishes_in_a_sliding_hour(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: t[0])
    c = C.Controller(MagicMock(), {}, "entry")
    for t[0] in (0.0, 1000.0, 3000.0):
        seqs_published(c, 1)
    assert c.commands_last_hour == 3
    t[0] = 3600.5
    assert c.commands_last_hour == 2
    t[0] = 6600.5
    assert c.commands_last_hour == 0


def test_the_ack_says_how_long_the_firmware_took_to_answer(monkeypatch):
    t = [10.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: t[0])
    c = sent_controller()
    t[0] = 10.25
    respond(c, c.last_seq, "setDishargeSuccess")
    assert c.ack["ack_after_ms"] == 250 and c.ack["ack_seq"] == c.last_seq
    respond(c, 0, "setDishargeSuccess")  # a seq this controller did not just send
    assert c.ack["ack_after_ms"] is None


# Edge cases.

def caps_house(monkeypatch):
    house = House(monkeypatch)
    house.c.config["max_discharge_entity"] = "input_number.discharge_max_rate"
    house.set("input_number.discharge_max_rate", "500")
    return house


def test_a_negative_cap_reads_as_0_w(monkeypatch):
    house = caps_house(monkeypatch)
    house.set("input_number.discharge_max_rate", "-500")
    assert house.c._read_policy().max_discharge_w == 0


def test_an_unreadable_cap_keeps_its_last_value_not_3000_w(monkeypatch):
    house = caps_house(monkeypatch)
    house.c.config["max_charge_entity"] = "input_number.charge_max_rate"
    assert house.c._read_policy().max_discharge_w == 500
    house.set("input_number.discharge_max_rate", "unavailable")
    assert house.c._read_policy().max_discharge_w == 500
    house.set("input_number.discharge_max_rate", "800")
    assert house.c._read_policy().max_discharge_w == 800
    house.set("sensor.battery_auto_action", "charge", soc_target=100)
    assert house.c._read_policy().max_charge_w == R.Settings().default_max_w  # never read yet


@pytest.mark.parametrize("state, soc, net", [("charge", 110, "-1000"), ("discharge", -5, "1000")])
def test_a_policy_soc_the_firmware_would_refuse_is_unreadable(monkeypatch, state, soc, net):
    # The firmware refuses socPercent over 105, and a negative one wraps through strtoul to the same refusal.
    house = House(monkeypatch)
    house.set("sensor.battery_auto_action", state, soc_target=soc)
    house.set("sensor.net_demand", net)
    house.second()
    socs = [cmd.get("socPercent") for cmd in house.sent() if cmd["kind"] != "normal"]
    assert all(0 <= s <= 105 for s in socs), socs
    assert house.c._read_policy() is None
    house.set("sensor.battery_auto_action", state, soc_target=soc, fixed="true", watts=500)
    assert house.c._read_policy() is None


@pytest.mark.parametrize("state, kind", [("discharge", "discharge"), ("charge", "charge")])
def test_a_fixed_policy_sends_its_watts_whatever_the_grid(monkeypatch, state, kind):
    house = House(monkeypatch)
    house.set("sensor.battery_auto_action", state, soc_target=50, fixed="true", watts="700")
    house.second()
    assert house.c.status == "fixed"
    assert (house.sent()[-1]["kind"], house.sent()[-1]["watts"], house.sent()[-1]["socPercent"]) == (kind, 700, 50)


@pytest.mark.parametrize("watts", [None, "lots", 25000])
def test_a_fixed_policy_without_plausible_watts_is_unreadable(monkeypatch, watts):
    house = House(monkeypatch)
    house.set("sensor.battery_auto_action", "discharge", soc_target=50, fixed="true",
              **({} if watts is None else {"watts": watts}))
    assert house.c._read_policy() is None


def test_a_hold_policy_holds_the_battery_at_0_w(monkeypatch):
    house = House(monkeypatch)
    house.second()
    assert house.sent()[-1]["watts"] > 0
    house.set("sensor.battery_auto_action", "hold")
    for _ in range(4):
        house.second()
    assert house.c.status == "hold" and house.sent()[-1] | {"seq": 0} == {
        "watts": 0, "socPercent": 102, "duration": 120, "seq": 0, "kind": "charge"}


def charge_error(monkeypatch, **attributes):
    house = House(monkeypatch)
    house.set("sensor.battery_auto_action", "charge", soc_target=90, **attributes)
    house.set("sensor.net_demand", "-1000")
    house.second()
    return house.c.regulator.error_w


def test_a_grid_offset_moves_the_target_while_charging(monkeypatch):
    plain = charge_error(monkeypatch)
    assert charge_error(monkeypatch, grid_offset=300) == plain - 300  # + tops up from the grid
    for bad in ("lots", 25000):
        assert charge_error(monkeypatch, grid_offset=bad) == plain


def test_an_other_soc_the_firmware_would_refuse_closes_the_other_direction(monkeypatch):
    house = House(monkeypatch)
    house.set("sensor.battery_auto_action", "discharge", soc_target=20, both="true", other_soc=110)
    assert house.c._read_policy().max_charge_w == 0
    house.set("sensor.battery_auto_action", "discharge", soc_target=20, both="true", other_soc=105)
    assert house.c._read_policy().max_charge_w == R.Settings().default_max_w


def test_a_correction_with_a_frozen_input_is_a_gap(monkeypatch):
    # A template re-rendered every second stays fresh by last_reported though an input under it froze. Its optional
    # age_s attribute, the age of its oldest input, over CORRECTION_MAX_AGE_S counts as missing: hold, then release.
    house = House(monkeypatch)
    house.second()
    house.set("sensor.net_demand", "1000", age_s=60)
    house.second()
    assert house.c.grid_w is not None
    house.set("sensor.net_demand", "1000", age_s=600)
    house.second()
    assert house.c.grid_w is None
    for _ in range(31):
        house.second()
    assert house.released()


def test_a_frozen_correction_is_a_gap_on_the_clamp_too(monkeypatch):
    house = House(monkeypatch)
    house.c.config = {"correction_entity": "sensor.net_demand"}
    house.set("sensor.net_demand", "100", age_s="unknown")  # not a number: ignored
    house.second()
    assert house.c.grid_w == 100
    house.set("sensor.net_demand", "100", age_s=91)
    house.second()
    assert house.c.grid_w is None


def clamp_house(monkeypatch, **config):
    """A controller on the grid clamp, with config in place of the net demand estimate."""
    house = House(monkeypatch)
    house.c.config = config
    return house


def test_the_clamp_reads_the_grid_as_it_is_by_default(monkeypatch):
    house = clamp_house(monkeypatch)
    house.second(**{C.GRID_KEY: 400})
    assert house.c.grid_w == 400 and house.sent()[-1]["kind"] == "discharge"


def test_the_clamp_scale_and_correction_make_the_grid(monkeypatch):
    house = clamp_house(monkeypatch, grid_scale=2.0, correction_entity="sensor.net_demand")
    house.set("sensor.net_demand", "-100")
    house.second(**{C.GRID_KEY: 400})
    assert house.c.grid_w == 2 * 400 - 100


def test_a_grid_scale_entity_overrides_the_scale_and_steps_on_its_change(monkeypatch):
    house = clamp_house(monkeypatch, grid_scale=2.0, grid_scale_entity="input_number.grid_scale")
    house.set("input_number.grid_scale", "-1.5")
    house.second(**{C.GRID_KEY: 400})
    assert house.c.grid_w == -600
    house.update("input_number.grid_scale", "0.5")
    assert house.c.grid_w == 200  # stepped on the change, with no new sample


@pytest.mark.parametrize("scale", ["unavailable", "nan"])
def test_an_unreadable_grid_scale_entity_is_a_gap_not_the_configured_scale(monkeypatch, scale):
    house = clamp_house(monkeypatch, grid_scale=2.0, grid_scale_entity="input_number.grid_scale")
    house.set("input_number.grid_scale", scale)
    house.second(**{C.GRID_KEY: 400})
    assert house.c.grid_w is None and "grid estimate" in house.c._gaps


def test_a_grid_scale_entity_past_the_form_range_is_a_gap(monkeypatch):
    # The scale is a factor, so the watts limit max_plausible_w is no bound for it; the form's range is.
    house = clamp_house(monkeypatch, grid_scale_entity="input_number.grid_scale")
    house.set("input_number.grid_scale", str(C.GRID_SCALE_MAX))
    house.second(**{C.GRID_KEY: 4})
    assert house.c.grid_w == 4 * C.GRID_SCALE_MAX
    house.set("input_number.grid_scale", "150")
    house.second(**{C.GRID_KEY: 4})
    assert house.c.grid_w is None


def test_a_response_that_is_json_but_not_an_object_is_ignored(monkeypatch):
    house = House(monkeypatch)
    house.second()
    ack = dict(house.c.ack)
    for payload in ("[]", "1", "null", '"x"'):
        house.c._on_response(types.SimpleNamespace(topic="Alpha2MQTT/response/set/charge", payload=payload))
    assert house.c.ack == ack


@pytest.mark.parametrize("flag", [True, "true"])
def test_west_from_the_10_s_fallback_gives_no_solar_step(monkeypatch, flag):
    # When the 1 s message leaves out a lone PV 0, the template falls back to the 10 s West value and says so.
    house = House(monkeypatch)
    house.c.set_setting("kff", 1.0)
    house.set("sensor.net_demand", "1000", west=800, east=200)
    house.second()
    house.set("sensor.net_demand", "820", west=980, east=200, west_fallback=flag)
    house.second()
    assert house.c.regulator.solar_w is None and house.c.regulator.ff_w == 0
    house.set("sensor.net_demand", "820", west=980, east=200, west_fallback=False)
    house.second()
    assert house.c.regulator.solar_w == 1180


# The controller's limits are settings too.

def test_the_controller_limits_are_settings_with_the_old_values():
    s = R.Settings()
    assert (s.default_max_w, s.max_plausible_w, s.correction_max_age_s, s.ack_timeout_s, s.match_wait_s) == (
        3000, 20000, 90, 10, 0.5)


def test_the_default_cap_is_a_setting(monkeypatch):
    house = caps_house(monkeypatch)
    house.c.set_setting("default_max_w", 1000)
    house.set("sensor.battery_auto_action", "discharge", soc_target=20, both="true", other_soc=100)
    assert house.c._read_policy().max_charge_w == 1000  # no charge cap entity


def test_the_plausible_limit_is_a_setting(monkeypatch):
    house = House(monkeypatch)
    house.c.set_setting("max_plausible_w", 1000)
    house.set("sensor.net_demand", "1500")
    house.second()
    assert house.c.grid_w is None
    house.c.set_setting("max_plausible_w", 2000)
    house.second()
    assert house.c.grid_w == 1000
    house.second(**{C.BATTERY_KEY: 2500})  # an implausible sample is dropped
    assert house.c.regulator.battery_w == 500


def test_the_correction_max_age_is_a_setting(monkeypatch):
    house = House(monkeypatch)
    house.set("sensor.net_demand", "1000", age_s=60)
    house.second()
    assert house.c.grid_w is not None
    house.c.set_setting("correction_max_age_s", 30)
    house.second()
    assert house.c.grid_w is None


def test_the_response_timeout_is_a_setting(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: t[0])
    c = sent_controller()
    c.set_setting("ack_timeout_s", 30)
    c.regulator.fail = MagicMock()
    respond(c, 0, "setDishargeSuccess")  # the firmware echoes seq
    t[0] = 20
    c._check_ack()
    c.regulator.fail.assert_not_called()
    t[0] = 31
    c._check_ack()
    c.regulator.fail.assert_called_once()


def test_the_correction_match_wait_is_a_setting(monkeypatch):
    house = House(monkeypatch)
    house.c.set_setting("match_wait_s", 1.5)
    house.set("sensor.net_demand", "1000", seq=0)
    house.seq = 5
    house.sample()
    assert house.timers[-1].delay == 1.5


# The safety paths: a publish that fails, a step that raises, and the Enabled switch handing the inverter back.

def errors(caplog, text):
    return [r for r in caplog.records if r.levelname == "ERROR" and text in r.getMessage()]


def test_a_failed_publish_is_sent_again_in_full_and_logged_once(monkeypatch, caplog):
    caplog.set_level("INFO")
    house = House(monkeypatch)
    house.mqtt.async_publish = AsyncMock(side_effect=OSError("broker down"))
    for _ in range(10):
        house.second()
    assert house.mqtt.async_publish.call_count > 1 and house.c.last_command is None
    assert len(errors(caplog, "could not publish")) == 1
    house.mqtt.async_publish = AsyncMock()
    for _ in range(4):
        house.second()
    assert house.sent()[0]["kind"] == "discharge" and house.c.last_command is not None
    assert "Battery controller publish works again" in caplog.text


def test_a_step_that_raises_hands_the_inverter_back_and_is_logged_once(monkeypatch, caplog):
    caplog.set_level("INFO")
    house = House(monkeypatch)
    house.second()
    house.c._read_policy = MagicMock(side_effect=ValueError("bad template"))
    for _ in range(5):
        house.second()
    assert house.released() and house.c.status == "released"
    assert len(errors(caplog, "step failed")) == 1
    del house.c._read_policy
    house.second()
    assert house.c.status == "regulating" and "Battery controller step works again" in caplog.text


def test_a_step_and_a_publish_failing_together_are_each_logged_once(monkeypatch, caplog):
    house = House(monkeypatch)
    house.second()
    house.mqtt.async_publish = AsyncMock(side_effect=OSError("broker down"))
    house.c._read_policy = MagicMock(side_effect=ValueError("bad template"))
    for _ in range(20):
        house.second()
    assert house.mqtt.async_publish.call_count > 2
    assert len(errors(caplog, "step failed")) == 1 and len(errors(caplog, "could not publish")) == 1


def enable_switch(c, last_state=None):
    switch = W.EnableSwitch(MagicMock(entry_id="entry", runtime_data=c))
    switch.async_get_last_state = AsyncMock(return_value=None if last_state is None else MagicMock(state=last_state))
    switch.async_write_ha_state = MagicMock()
    asyncio.run(switch.async_added_to_hass())
    return switch


@pytest.mark.parametrize("last_state, on", [(None, False), ("on", True), ("off", False), ("unavailable", False)])
def test_the_enabled_switch_restores_on_only_from_on(last_state, on):
    c = C.Controller(MagicMock(), {}, "entry")
    assert enable_switch(c, last_state).is_on is on and c.enabled is on


def test_turning_the_controller_off_hands_the_inverter_back_at_once(monkeypatch):
    house = House(monkeypatch)
    house.c.enabled = False
    switch = enable_switch(house.c)
    asyncio.run(switch.async_turn_on())
    house.second()
    assert house.sent()[-1]["kind"] == "discharge"
    asyncio.run(switch.async_turn_off())
    assert switch.is_on is False and house.sent()[-1]["kind"] == "normal"
    switch.async_write_ha_state.assert_called()
    count = len(house.sent())
    house.second()
    assert len(house.sent()) == count and house.c.status == "disabled"


def test_the_controller_turns_off_even_when_the_release_cannot_be_published(monkeypatch):
    # The lease then hands the inverter back within duration_s.
    house = House(monkeypatch)
    house.second()
    house.mqtt.async_publish = AsyncMock(side_effect=OSError("broker down"))
    switch = enable_switch(house.c)
    asyncio.run(switch.async_turn_off())
    assert switch.is_on is False and house.mqtt.async_publish.call_args.args[1].endswith("/normal")
    count = house.mqtt.async_publish.call_count
    for _ in range(5):
        house.second()
    assert house.mqtt.async_publish.call_count == count
