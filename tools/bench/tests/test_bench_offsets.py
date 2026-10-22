"""Three models of the inverter's steady offset, battery reading minus setpoint, and how they relate:

- regulator.INVERTER_OFFSET: the table the regulator corrects by, offset_comp x it, hand set from one unit.
- plant.Plant.offset: the bench's model, fitted from logs. Same quantity, same signs, same interpolation, so a
  Plant built from INVERTER_OFFSET is the inverter the regulator believes in.
- 1.046 u - 42 in test_regulator.py: a stress plant on purpose, not a measurement. It is far from the table, and
  the regulator test shows the integral closes the gap anyway.
"""

import pytest

import logs
import plant as P
import replay
from synth import rows

REG = replay.load_regulator()
TABLE = P.Plant(dead_s=0.0, tau_discharge_s=0.01, tau_charge_s=0.01, offset=[list(p) for p in REG.INVERTER_OFFSET])


def stress(u):
    """The closed loop test's inverter: 1.046 u - 42 for a discharge, nothing otherwise."""
    return 1.046 * u - 42 if u > 0 else 0.0


def test_a_plant_built_from_the_regulator_table_has_the_same_offset():
    for w in range(-4500, 4501, 25):
        assert TABLE.offset_at(w) == pytest.approx(REG.inverter_offset(w), abs=1e-9)


def test_the_stress_plant_as_a_plant_offset_and_how_far_it_is_from_the_table():
    plant = P.Plant(offset=[[1.0, stress(1.0) - 1.0], [4000.0, stress(4000.0) - 4000.0]])
    for u in range(1, 4001, 50):
        assert u + plant.offset_at(u) == pytest.approx(stress(u))
    gap = {u: plant.offset_at(u) - REG.inverter_offset(u) for u in (100, 1000, 1500, 2750, 3000)}
    assert gap[1000] == pytest.approx(4.0 - -25.0)
    assert gap[2750] == pytest.approx(84.5 - 198.0)
    assert max(abs(g) for g in gap.values()) > 100  # far from the table: a stress case, not the same inverter


def test_a_learned_table_loaded_at_start_corrects_the_plant_it_was_learned_on():
    # The plant is 25 W short at every setpoint; the table says so, so the battery lands on the output.
    short = P.Plant(dead_s=0.0, tau_discharge_s=0.01, tau_charge_s=0.01, offset=[[-4000.0, -25.0], [4000.0, -25.0]])
    table = [[x, 0.0 if x == 0 else -25.0] for x in REG.TRIM_NODES]
    gaps = []
    for trim in (None, table):
        out, warnings = replay.replay(REG, rows(240, demand=1000.0, solar=0.0), short, {"min_interval_s": 0},
                                      loss=logs.Loss(0, 0), trim=trim)
        assert warnings == []
        sent = [r for r in out if r["sent"] is not None][-1]
        gaps.append(out[-1]["battery"] - sent["output_w"])
    assert gaps[0] == pytest.approx(-25, abs=REG.Settings().command_step_w / 2 + 1)
    assert gaps[1] == pytest.approx(0, abs=REG.Settings().command_step_w / 2 + 1)


def test_a_table_that_does_not_fit_warns_and_starts_from_zero():
    _, warnings = replay.replay(REG, rows(3), TABLE, trim=[[0.0, 0.0]])
    assert warnings == ["the learned table given does not fit this regulator, starting from zero"]


@pytest.mark.parametrize("comp", [0.0, 1.0])
def test_offset_comp_1_lands_the_battery_on_the_output_of_a_table_plant(comp):
    # Demand 1000 W at a target of -50: the output settles near 1050 W, where the table says -25 W.
    out, warnings = replay.replay(REG, rows(240, demand=1000.0, solar=0.0), TABLE,
                                  {"offset_comp": comp, "min_interval_s": 0}, loss=logs.Loss(0, 0))
    assert warnings == []
    sent = [r for r in out if r["sent"] is not None][-1]  # the last command, and the output it was sent for
    assert out[-1]["t"] - sent["t"] >= 5  # settled since
    battery = out[-1]["battery"]
    if comp:  # on the output, but for the command's rounding to command_step_w
        assert battery == pytest.approx(sent["output_w"], abs=REG.Settings().command_step_w / 2 + 1)
    else:  # the command plus the table's offset at it
        assert battery - sent["sent"] == pytest.approx(REG.inverter_offset(sent["sent"]), abs=0.5)
        assert REG.inverter_offset(sent["sent"]) == -25.0
