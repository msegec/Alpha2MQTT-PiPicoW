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
    assert abs(out[-1]["error_w"]) < 1.0
    assert out[-1]["battery"] == pytest.approx((500 + 30 + 10) / 0.85, abs=1)  # demand plus 15 % and 10 W loss
    out, _ = replay.replay(mod, rows(), FAST, {"target_w": "0"}, loss=logs.Loss(0, 0))
    assert out[-1]["battery"] == pytest.approx(500 + 30, abs=1)
    steps = [c for c in mod.CALLS if c[0] == "step"]
    assert steps[0][2] == -30.0 and steps[0][3] == 4000.0


def test_each_command_is_acked_a_tenth_of_a_second_later(tmp_path):
    mod = module(tmp_path, NEW)
    replay.replay(mod, rows(5), FAST)
    acks = [c for c in mod.CALLS if c[0] == "ack"]
    assert [a[1] for a in acks] == pytest.approx([0.1, 1.1, 2.1, 3.1])
    assert all(a[2] is True for a in acks)


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
