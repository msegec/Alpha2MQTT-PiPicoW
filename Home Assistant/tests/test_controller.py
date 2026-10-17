"""Tests for the controller's response handling. Home Assistant is stubbed, so it is not needed."""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

for _name in ("homeassistant", "homeassistant.components", "homeassistant.core", "homeassistant.helpers",
              "homeassistant.helpers.device_registry", "homeassistant.helpers.event", "homeassistant.util"):
    sys.modules.setdefault(_name, MagicMock())
sys.modules["homeassistant.core"].callback = lambda f: f
sys.modules["homeassistant.components"].mqtt.async_publish = AsyncMock()

_pkg = types.ModuleType("a2m_controller")
_pkg.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller")]
sys.modules["a2m_controller"] = _pkg
C = importlib.import_module("a2m_controller.controller")
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
