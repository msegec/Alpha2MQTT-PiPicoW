import subprocess

import pytest

import logs
import plant as P
import replay
from synth import rows

TRIVIAL = '''
from dataclasses import dataclass

REGULATE = "regulate"
CALLS = []


@dataclass(frozen=True)
class Policy:
    mode: str = "release"
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    charge_soc: int = 100
    discharge_soc: int = 10
    target_offset_w: float = 0.0


@dataclass
class Settings:
    target_w: float = 20.0
    duration_s: int = 120


@dataclass(frozen=True)
class Command:
    kind: str
    watts: int = 0
    power_only: bool = False

    @property
    def signed_w(self):
        return self.watts if self.kind == "discharge" else -self.watts


class Regulator:
    """Asks the battery for exactly the grid error, every sample."""

    def __init__(self, settings):
        self.settings = settings
        self.request_w = self.output_w = 0.0

    def update_grid(self, t, grid_w, battery_w, fresh=True, soc=None{solar_arg}):
        CALLS.append(("grid", t, grid_w, battery_w, soc{solar_val}))
        self.grid_w, self.battery_w = grid_w, battery_w
{load_method}
    def step(self, t, policy):
        CALLS.append(("step", t, policy.target_offset_w, policy.max_charge_w))
        w = self.battery_w + self.grid_w - self.settings.target_w - policy.target_offset_w
        self.request_w = self.output_w = w
        return Command("discharge" if w > 0 else "charge", int(round(abs(w))), power_only=True)

    def ack(self, t, power_write=False):
        CALLS.append(("ack", t, power_write))

    def fail(self):
        CALLS.append(("fail", None))
'''
OLD = TRIVIAL.format(solar_arg="", solar_val="", load_method='''
    def update_load(self, t, load_w):
        CALLS.append(("load", t, load_w))
''')
NEW = TRIVIAL.format(solar_arg=", solar_w=None", solar_val=", solar_w", load_method="")
FAST = P.Plant(dead_s=0.0, tau_discharge_s=0.01, tau_charge_s=0.01)


def module(tmp_path, src, name="regulator.py"):
    f = tmp_path / name
    f.write_text(src)
    return replay.load_regulator(str(f))


def test_a_trivial_regulator_settles_the_grid_on_target(tmp_path):
    mod = module(tmp_path, NEW)
    out, warnings = replay.replay(mod, rows(), FAST, {"target_w": "0"})
    assert warnings == []
    # logged target -50 under a cfg target of -20 is an offset of -30, replayed over the new target_w of 0
    assert {r["target"] for r in out} == {-30.0}
    assert {r["deadband_w"] for r in out} == {None}  # the stand in has no deadband
    assert abs(out[-1]["error_w"]) < 1.0
    assert out[-1]["battery"] == pytest.approx((500 + 30 + 10) / 0.85, abs=1)  # demand plus 15 % and 10 W loss
    out, _ = replay.replay(mod, rows(), FAST, {"target_w": "0"}, loss=logs.Loss(0, 0))
    assert out[-1]["battery"] == pytest.approx(500 + 30, abs=1)
    steps = [c for c in mod.CALLS if c[0] == "step"]
    assert steps[0][2] == -30.0 and steps[0][3] == 4000.0  # the stand in has no default_max_w


def test_each_command_is_acked_a_tenth_of_a_second_later(tmp_path):
    mod = module(tmp_path, NEW)
    replay.replay(mod, rows(5), FAST, tick_s=0)
    acks = [c for c in mod.CALLS if c[0] == "ack"]
    assert [a[1] for a in acks] == pytest.approx([0.1, 1.1, 2.1, 3.1])
    assert all(a[2] is True for a in acks)


def test_a_refused_command_is_not_written_and_calls_fail(tmp_path):
    mod = module(tmp_path, NEW)
    r = rows(5)
    for x in r[1:]:
        x["demand"] = 1000.0
    out, _ = replay.replay(mod, r, FAST, outcome=replay.every(fail_every=2), tick_s=0)
    seen = [(c[0], c[1]) for c in mod.CALLS if c[0] in ("ack", "fail")]
    assert seen == [("ack", 0.1), ("fail", None), ("ack", 2.1), ("fail", None)]
    assert out[1]["battery"] == pytest.approx(out[2]["battery"], abs=1)  # the step up at t 1 was refused
    assert out[3]["battery"] > out[2]["battery"] + 400  # the one at t 2 was written


def test_an_unanswered_command_fails_once_after_the_ack_timeout(tmp_path):
    mod = module(tmp_path, NEW)
    first = mod.Regulator.step

    def once(self, t, policy):  # one command, then hold it
        return first(self, t, policy) if t == 0 else None
    mod.Regulator.step = once
    out, _ = replay.replay(mod, rows(15), FAST, outcome=replay.every(timeout_every=1))
    assert [c for c in mod.CALLS if c[0] in ("ack", "fail")] == [("fail", None)]
    fail_at = [i for i, c in enumerate(mod.CALLS) if c[0] == "fail"][0]
    assert [c[1] for c in mod.CALLS[:fail_at] if c[0] == "grid"][-1] == 10.0  # fails at the 11 s sample
    assert {r["battery"] for r in out} == {0.0}  # never written


def test_the_tick_steps_between_samples_but_not_across_a_break(tmp_path):
    mod = module(tmp_path, NEW)
    r = rows(6)
    r[3]["brk"] = True
    out, _ = replay.replay(mod, r, FAST)
    steps = [c[1] for c in mod.CALLS if c[0] == "step"]
    assert steps == [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5, 5.0]
    assert [len(x["cmds"]) for x in out] == [2, 2, 1, 2, 2, 1]  # a tick's command counts on the row before it
    grids = [c[1] for c in mod.CALLS if c[0] == "grid"]
    assert grids == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]  # a tick has no new sample
    mod.CALLS.clear()
    replay.replay(mod, rows(3), FAST, tick_s=0)
    assert [c[1] for c in mod.CALLS if c[0] == "step"] == [0.0, 1.0, 2.0]


def test_caps_default_to_the_default_max_w_in_force(tmp_path):
    src = NEW.replace("    duration_s: int = 120", "    duration_s: int = 120\n    default_max_w: float = 3000.0")
    mod = module(tmp_path, src)
    cfgs = logs.CfgTimeline([{"k": "cfg", "t": 1.5, "default_max_w": 2500.0}])
    replay.replay(mod, rows(3), FAST, cfgs=cfgs, tick_s=0)
    assert [c[3] for c in mod.CALLS if c[0] == "step"] == [3000.0, 3000.0, 2500.0]
    mod.CALLS.clear()
    replay.replay(mod, rows(2), FAST, max_charge_w=1200.0, tick_s=0)
    assert [c[3] for c in mod.CALLS if c[0] == "step"] == [1200.0, 1200.0]


def test_the_battery_is_read_before_the_match_wait(tmp_path):
    mod = module(tmp_path, NEW)
    slow = P.Plant(dead_s=0.0, tau_discharge_s=1.0, tau_charge_s=1.0)
    r = rows(3)
    out0, _ = replay.replay(mod, r, slow, tick_s=0)
    for x in r:
        x["wait_s"] = 0.5
    out, _ = replay.replay(mod, r, slow, tick_s=0)
    assert out[1]["t"] == 1.0  # the step stays at the logged time
    assert 0 < out[1]["battery"] < out0[1]["battery"]  # read half a second earlier, on the rise


def test_a_replay_can_simulate_the_soc_and_the_floor_refusal(tmp_path):
    mod = module(tmp_path, NEW)
    plant = P.Plant(dead_s=0.0, tau_discharge_s=0.01, tau_charge_s=0.01, capacity_wh=10.0, refuse_soc=50.0)
    out, _ = replay.replay(mod, rows(40, soc=60.0), plant, tick_s=0, loss=logs.Loss(0, 0))
    socs = [x["soc"] for x in out]
    assert socs[0] == 60.0 and socs == sorted(socs, reverse=True)  # from the logged SOC, then down
    assert [c[4] for c in mod.CALLS if c[0] == "grid"] == socs  # the regulator sees the simulated SOC
    assert 49.0 < socs[-1] <= 50.0  # the refusal stops the discharge at 50 %
    assert out[-1]["battery"] == pytest.approx(0, abs=1)


def test_outcomes_count_commands_from_one():
    o = replay.every(fail_every=3, timeout_every=2)
    assert [o(n) for n in range(6)] == ["ok", "timeout", "fail", "timeout", "ok", "fail"]


def test_the_api_is_detected_by_signature(tmp_path):
    new = module(tmp_path, NEW, "new.py")
    replay.replay(new, rows(3, load_w=900.0), FAST)
    assert [c[5] for c in new.CALLS if c[0] == "grid"] == [321.0] * 3
    assert not [c for c in new.CALLS if c[0] == "load"]
    old = module(tmp_path, OLD, "old.py")
    replay.replay(old, rows(3, load_w=900.0), FAST)
    assert len([c for c in old.CALLS if c[0] == "grid"][0]) == 5  # no solar
    assert [c[2] for c in old.CALLS if c[0] == "load"] == [900.0] * 3


def test_settings_are_cast_and_unknown_ones_named(tmp_path):
    mod = module(tmp_path, NEW)
    vals, unknown = replay.coerce(mod, {"target_w": "-20", "duration_s": "60.0", "warm_w": "30"})
    assert vals == {"target_w": -20.0, "duration_s": 60} and isinstance(vals["duration_s"], int)
    assert unknown == ["warm_w"]
    _, warnings = replay.replay(mod, rows(2), FAST, {"warm_w": "30"})
    assert warnings == ["setting warm_w unknown to this regulator, ignored"]


def test_settings_from_log_follow_cfg_changes(tmp_path):
    mod = module(tmp_path, NEW)
    cfgs = logs.CfgTimeline([{"k": "cfg", "t": -1.0, "target_w": -20.0, "kp": 1.0},
                             {"k": "cfg", "t": 10.0, "target_w": 40.0}])
    r = rows(20)
    for x in r[10:]:
        x["cfg_target"] = 40.0
        x["target"] = 10.0  # offset -30 still
    out, _ = replay.replay(mod, r, FAST, cfgs=cfgs)
    assert out[5]["target"] == -50.0 and out[15]["target"] == 10.0


def test_a_regulator_from_a_git_ref(tmp_path):
    repo = tmp_path / "repo"
    f = repo / replay.REGULATOR
    f.parent.mkdir(parents=True)
    f.write_text(NEW)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "one"], check=True)
    f.write_text(OLD)  # the working tree differs; the ref must win
    mod = replay.load_regulator(ref="HEAD", repo=repo)
    assert not hasattr(mod.Regulator, "update_load")
