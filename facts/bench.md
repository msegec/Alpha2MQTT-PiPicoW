# Replay bench

Tools in `tools/bench/` that score live step logs and replay their demand through any regulator version against
a fitted inverter model, so versions and settings can be compared on the same weeks.

Last verified: working tree on a362446, 2026-10-22. `python3 -m pytest tools/bench -q`: 28 passed. Every tool
ran with `python3 -I` on private step logs (two daily `.gz` files and the live file), and
`replay.py` replayed through every committed `regulator.py` revision without error.

Plain Python 3.12+, stdlib only. Each script puts its own directory on `sys.path`, so `python3 -I` works. The
log record contract is in `steplog-and-response.md`; the regulator in `regulator.md`.

## Files

| File | Role |
| --- | --- |
| `logs.py` | Load files or globs (`.gz` or plain), sort by `t`, skip bad lines; `samples`, `commands`, `iterations`, `CfgTimeline` |
| `plant.py` | `Plant` (the model), `Sim` (runs it), `fit`, `model_error` |
| `replay.py` | `load_regulator` (path or git ref), `replay`, `coerce` |
| `score.py` | `score`, `causes`, `regimes`, the scorecard table |
| `compare.py` | Variants by windows in one table |
| `tests/` | Synthetic logs only; `synth.py` builds step records |

## Commands

Times are local, `'YYYY-MM-DD HH:MM'`, `'YYYY-MM-DD'` or Unix seconds. Spans are `'A..B'`. Quote globs.

```
python3 -I tools/bench/logs.py 'LOGDIR/alpha2mqtt.jsonl*'                       # iterations: code and cfg spans
python3 -I tools/bench/plant.py 'LOGDIR/alpha2mqtt.jsonl*' --exclude 'A..B' --save plant.json [--plant old.json]
python3 -I tools/bench/score.py 'LOGDIR/alpha2mqtt.jsonl*' --from A --to B [--exclude 'A..B'] [--json]
python3 -I tools/bench/replay.py 'LOGDIR/alpha2mqtt.jsonl*' --from A --to B --ref HEAD --set ki=0.03 [--settings-from-log] [--plant plant.json] [--json]
python3 -I tools/bench/compare.py 'LOGDIR/alpha2mqtt.jsonl*' --window 'A..B' --window 'C..D' --exclude 'E..F' \
    --set target_w=-20 --set deadband_w=5 ... --plant plant.json \
    --variant 'now:ref=HEAD' --variant 'ki3:ref=HEAD,ki=0.03' --variant 'wip:path=/some/regulator.py'
```

Shared options: `--solar-entity SUFFIX` (default none), `--loss-share` (default 0.15) and
`--loss-w` (default 10), `--max-charge-w` and `--max-discharge-w` (default 4000), `--discharge-soc` (default 10).

## Loading (`logs.py`)

- Daily `.gz` files load before the plain live file; then every record sorts by `t`. Pass each day once: a
  concatenated copy plus the originals loads twice.
- A bad JSON line, or one without `k` or a numeric `t`, is counted and skipped. A truncated gzip keeps what was
  read.
- Kept: `start`, `cfg` and `e` records whatever the window (so the cfg in force and the last east solar reading
  are known), others within 60 s of the window. Only `e` records of the `--solar-entity` are parsed; none
  without one.
- `samples` keeps `step` records with `status == "regulating"` and finite `grid_w`, `battery`, `error_w`,
  `request_w`, `output_w`. A row has `brk` True when its `seq` is not the last row's + 1 or more than 5 s
  passed. `seq` restarts at a firmware reboot, so nothing keys on `seq` across rows that are not consecutive.
- Row fields: `target = grid_w - error_w` (it holds the policy offset), `cfg_target` (logged `target_w` in
  force), `deadband_w` (the same, None without one), `cmds` (every dispatch request on the bus after this
  step record and before the next step record, signed W or None for normal), `demand = grid_w + battery - loss(battery)`, `sent` (signed W, or None), `solar`, `load_w`.
- Battery loss, `logs.Loss`: `share * |battery| + fixed_w`, and 0 at a battery of exactly 0. The term is not
  the controller's: `estimate_grid` adds none. It comes from the correction source. The development site's
  correction template charges 15 % of |battery| plus 10 W as inverter loss in the demand it reports, so the
  defaults are 0.15 and 10 W. A correction without a loss term wants `--loss-share 0 --loss-w 0`. That the
  10 W stops at a battery of exactly 0 is the bench's assumption; how the template gates it is not in this repo.
- Solar: the step's `solar_w` when logged. Older logs: west `pv * -10` when at least 95 W, else 0, plus the
  last numeric state of the entity ending in `--solar-entity` (a non-numeric state counts 0). That entity is
  installation specific, so there is no default: without one, older logs see west solar only.
- `commands`: `cmd` records on `.../request/set/{charge,discharge,normal}` only. `read/register/raw`, raw
  writes and responses are not commands.
- `iterations`: a new span at a `start` unless it carries the same `code` as the running span (a start
  without `code` always splits), and at a `cfg` whose settings differ. The `cfg` written with a `start`
  belongs to it.

## The inverter model (`plant.py`)

- A command takes effect `dead_s` after it is sent. The battery then follows a first-order lag towards the
  command plus `offset_at(command)`, with `tau_discharge_s` when the aim is above 0 (or 0 from a discharge),
  else `tau_charge_s`. `normal` is modelled as 0 W.
- `offset` is a list of `[setpoint W, battery minus setpoint W]`, linear between points, held flat outside them,
  0 at a 0 W command.
- `offset` is the same quantity as the regulator's `INVERTER_OFFSET` (battery minus setpoint, by signed
  setpoint, linear between points, flat outside). A `Plant` built from that table is the inverter the regulator
  believes in: at `offset_comp` 1 a replay lands the battery on the output. The `1.046 u - 42` inverter in
  `test_regulator.py` is a stress plant, over 100 W from the table at some setpoints, not a third measurement.
  `tests/test_bench_offsets.py` holds all three to this.
- `ramp_w_s` above 0: a discharge that takes effect while `|battery| <= 15 W` rises no faster than this
  until it reaches its aim. 0 is off.
- `Sim` steps 0.1 s at most.

Fit, from samples and bus commands:

1. Offset: samples whose command value has not changed for 8 s and whose battery moved under 25 W over the
   last 3 consecutive samples. Excluded: SOC at or over 97 %, and capped samples (`|request_w - output_w| >=
   30`). Binned by signed setpoint at edges -4000, -3000, -2500, -2000, -1500, -1000, -750, -500, -250, -100,
   -1, 1, 100, 250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000; a bin of 30 or more gives one point (median
   setpoint, median offset).
2. Steps: every command more than 300 W from the battery at the sample before it (that sample within 2 s,
   not excluded). Up to 8 following consecutive samples, stopping at a later command more than 100 W
   different. Each gives seconds after the command and the fraction of the move to command plus offset.
3. Groups: `discharge` (command over 0 W, or 0 W from a discharge), else `charge`. A discharge rise still
   under 0.5 after 6 samples is `ramp`, or `refused` if the battery rose under 30 W.
4. Per group, the median fraction per sample index, at the mean time. A grid search (dead 0 to 3 s, tau 0.1 to
   10 s, steps of 0.1) fits `1 - exp(-(t - dead) / tau)`. `dead_s` is the event-weighted mean of the two.
5. With 5 or more `ramp` events, `ramp_w_s` is their median rise rate from the second to the last sample.

`model_error` drives the model with the logged commands, restarts it from the logged battery at each `brk`, and
scores samples that are not excluded: mean |model - log|, bias, the same within 8 s of a step over 300 W, and
the mean |error| of "battery = last command" for reference. With `--plant old.json` the old fit is scored on
the new logs too: a growing error is drift.

## Replay (`replay.py`)

- Regulator: `--regulator PATH`, or `--ref GITREF`, which runs `git show REF:"Home Assistant/custom_components/
  alpha2mqtt_controller/regulator.py"` into a temp dir. Default is the working tree's file.
- Settings: module defaults, then the logged cfg in force with `--settings-from-log` (a later `cfg` is applied
  with `setattr`, as the controller does), then `--set`. Values are cast to each default's type. A field the
  module lacks is dropped; a dropped `--set` prints a warning.
- API by signature: `update_grid` gets `soc=` and `solar_w=` only if it takes them. `update_load` (older
  versions) gets the logged `load_w` when there is one. `ack` gets `power_write=` if it takes it; a regulator
  without `ack` gets none.
- Per sample: advance the model to `t`; ack the last command at its send time + 0.1 s with
  `power_write = cmd.power_only`; grid = `demand + loss(battery) - battery`; `update_grid`; then
  `step` with `Policy(REGULATE, max_charge_w, max_discharge_w, charge_soc=100, discharge_soc,
  target_offset_w = target - cfg_target)`. The replayed target is the variant's `target_w` plus that offset.
  Without a logged cfg, `cfg_target` is the variant's `target_w`.
- At a `brk` the model restarts from the logged battery, keeping its last aim; the regulator runs on and goes
  stale on its own after `stale_after_s`.
- SOC is the logged one, not simulated.

## Scorecard (`score.py`)

Over rows from `samples` (live) or `replay` (simulated). `e = grid_w - target`. Each row weighs
`dt = min(next t - t, 5 s)`, the last row 1 s.

| Metric | Definition |
| --- | --- |
| `import_w`, `import_wh` | time mean and sum of `max(e, 0)`: error towards import |
| `export_w`, `export_wh` | the same for `max(-e, 0)` |
| `cost_w`, `cost_wh` | `4 * import + export` |
| `abs_error_w`, `mean_error_w` | time mean of `|e|` and `e` |
| `grid_import_w`, `grid_export_w` | JSON only: the grid itself, above and below 0 |
| `commands_per_min` | commands per minute, every command in each row's `cmds`: live, every dispatch request on the bus between the row's step record and the next, so tick commands count; replay, every command the replay sent |
| `turn_rounds` | non-zero commands whose sign differs from the last non-zero one; 0 W keeps the sign |

Weights. `IMPORT_WEIGHT` 4 is the ratio in the regulator's `ki_import_x` comment, import costing about four
times what export earns; no tariff figure is in the repo. It weighs the error from target, not the grid flow, and
only changes the ranking of variants that trade import for export. Read `import_w` and `export_w` beside it.

Loss budget: each row gets one cause, first match wins.

| Cause | Rule |
| --- | --- |
| `capped` | `|request_w - output_w| >= 30 W`: a limit holds the output |
| `spike` | within 6 rows of a demand jump over 500 W between consecutive rows that comes back within 30 % of the jump in the next 3 rows |
| `step` | within 15 rows of such a jump that stays |
| `ramp` | demand moved over 30 W/s across the last 3 consecutive rows |
| `offset` | 8 s or more since the last command and `|e|` over the row's `deadband_w`, at least 5 W |
| `noise` | the rest |

`deadband_w` is the logged cfg in force for live rows and the variant's setting for replay rows; a row without
one takes 20 W, the regulator's default. A loop at rest inside its deadband is `noise`, not `offset`.

Demand for causes is the logged `demand` in both live and replay, so variants share the same tags except
`capped` and `offset`. A jump inside a spike (its way back) starts nothing; the prototype `budget.py` tagged it
a step.

Regimes: night when solar < 50 W. A day row takes its local clock hour's class: from 1-minute means `m` of day
solar in that hour, variability = mean `|m[i] - (m[i-1] + m[i+1]) / 2|` / max(hour mean, 300 W). Over 0.05
is cloudy, else clear; under 3 day minutes is clear. The threshold separated clear and broken cloud hours on the
development logs, a one off choice. It only moves rows between the clear and cloudy columns; `all` does not
depend on it.

`compare.py` scores the live rows and each variant per window, plus "all windows" when there are several.
Overlapping windows count twice there. One plant serves all variants: `--plant`, else the fit on all windows.

## Known model limits

- The model has no reading noise or quantisation, so a replay scores better than live for the same code.
  On the development logs the same regulator at live settings replayed at about two thirds of the live cost.
  Compare variants with each other; use the live row only for the trend.
- Policy limits are not in the log. A replay uses the CLI limits, so a live day capped by a policy charge
  limit replays without that cap. Exclude such spans, or set the limits.
- The slow ramp starts only from an idle battery. Logs show it lasting across later commands; whether it
  can start from a running discharge, and when the inverter enters that mode, is unknown.
- A balance hold or full battery delivers less than asked; the model does not. Exclude holds with `--exclude`.
  SOC is not simulated, so floor and full holds follow the logged SOC.
- The fit's discharge group mixes rises, cuts to 0 W and turns from charge, which lag differently; fractions
  above 1 (overshoot) are fitted as a plain lag.
- Older logs: a `pv` of -10 at night reads as 100 W west solar, so a night can show minutes of `clear`.

## Open questions

- Whether a noise model taken from `model_error` residuals would bring replay costs near live: unknown.
- The policy limits in force per sample: not logged, unknown.
