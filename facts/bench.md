# Replay bench

Tools in `tools/bench/` that score live step logs and replay their demand through any regulator version against
a fitted inverter model, so versions and settings can be compared on the same weeks.

Last verified: branch fix/bench on d80bfb1, 2026-10-22. `python3 -m pytest tools/bench/tests -q`: 57
passed; `python3 -m pytest "Home Assistant/tests" -q`: 306 passed. Not rerun on private logs since a362446.

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
| `tests/` | Synthetic logs only; `synth.py` builds step records and sample rows. `test_bench_end_to_end.py` simulates a live run of the shipped `regulator.py` against a known plant, writes it as a step log, and runs fit, replay and compare on it through their `main`; replaying the logged settings scores within 5 % of the logged run |

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

Shared options: `--solar-entity SUFFIX` (default none), `--loss-share` (default 0.15) and `--loss-w` (default
10). Replay and compare also take `--max-charge-w` and `--max-discharge-w` (default: the setting `default_max_w`
in force), `--discharge-soc` (default 10), `--ack-s`, `--fail-every`, `--timeout-every`, `--tick-s`, and `--trim FILE` or `--trim-from-log`.

## Loading (`logs.py`)

- Daily `.gz` files load before the plain live file; then every record sorts by `t`. Pass each day once: a
  concatenated copy plus the originals loads twice.
- A bad JSON line, or one without `k` or a numeric `t`, is counted and skipped. A truncated gzip keeps what was
  read.
- Kept: `start`, `cfg`, `e`, `trim_load` and `trim` records whatever the window (so the cfg and learned table
  in force and the last solar meter reading are known), others within 60 s of the window. Only `e` records of the `--solar-entity` are parsed; none
  without one.
- `samples` keeps `step` records with `status == "regulating"` and finite `grid_w`, `battery`, `error_w`,
  `request_w`, `output_w`. A row has `brk` True when its `seq` is not the last row's + 1 or more than 5 s
  passed. `seq` restarts at a firmware reboot, so nothing keys on `seq` across rows that are not consecutive.
- Row fields: `target = grid_w - error_w` (it holds the policy offset), `cfg_target` (logged `target_w` in
  force), `deadband_w` (the same, None without one), `cmds` (every dispatch request on the bus after this
  step record and before the next step record, signed W or None for normal), `wait_s` (`wait_ms / 1000`, 0
  unlogged), `demand = grid_w + battery - loss(battery)`, `sent` (signed W, or None), `solar`, `load_w`.
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
- SOC and holds, set by hand, not fitted: `capacity_wh` above 0 integrates the SOC from the battery power
  (0 leaves it to the caller). A discharge while the SOC is at or under `refuse_soc`, or a charge at or over
  `full_soc`, delivers 0 W; None is off, and so is an unknown SOC. Replay and compare take `--capacity-wh`,
  `--refuse-soc` and `--full-soc` over the plant.
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
- Per sample: advance the model to `t - wait_s` and read the battery there (the controller reads a sample,
  then waits up to `match_wait_s` for its correction, and logs `wait_ms`; the step record's `t` is after the
  wait); deliver the responses due by `t`; grid = `demand + loss(battery) - battery`; `update_grid`; then
  `step` with `Policy(REGULATE, max_charge_w, max_discharge_w, charge_soc=100, discharge_soc,
  target_offset_w = target - cfg_target)`. The replayed target is the variant's `target_w` plus that offset.
  Without a logged cfg, `cfg_target` is the variant's `target_w`.
- Responses, as `controller._on_response` and `_check_ack` handle them. Each published command has an
  outcome, `ok` unless `--fail-every N` or `--timeout-every N` picks it (counted from 1; refused wins a tie).
  `ok`: the model gets the command, and a response at send time + `--ack-s` (0.1 s) calls `ack(that time,
  power_write = cmd.power_only)`. `fail`: the model never gets it; the response calls `fail()`. `timeout`: the
  model never gets it, no response comes, and the first check over `ack_timeout_s` after sending calls `fail()`
  once. A response counts only for the newest command while the regulator's `sent` is still it. Responses carry
  their own time but are delivered at the next sample or tick. The regulator reads ack and fail state only
  inside `step`, so this matches live: no step falls between a response and its delivery.
- Tick, as `controller._on_tick`: every `--tick-s` (1 s, 0 off), half a tick out of phase with the first
  sample, the ack timeout check runs, then `step` with the last sample's policy and no new sample. Ticks run
  only between consecutive samples, not across a `brk`. A tick's command counts in the `cmds` of the sample
  before it, as on the bus. With ticks off the timeout check runs at each sample.
- Learned table: none by default, so the regulator starts from zero. `--trim FILE` loads the controller's
  store file (`{"data": {"points": ...}}`) or `{"points": ...}`; `--trim-from-log` loads the points of the last
  `trim_load` or `trim` record at or before the window, as `controller._load_trim` does at start. Either goes
  through `load_trim`; a table that does not fit the regulator warns and starts from zero. It loads once; after
  that the variant learns only if its `trim_learn` is on.
- `fresh`: every replayed `update_grid` is `fresh=True`. That matches the log: `step` records come only from
  `controller._take`, which passes `fresh=True`. The `fresh=False` updates when only the correction changes
  write no record, so they cannot be replayed.
- Still not as live: the `_on_entity` steps on policy and correction changes (not logged as steps), the
  policy caps and floor (see Known model limits), and a correction that misses the match wait.
- At a `brk` the model restarts from the logged battery, keeping its last aim; the regulator runs on and goes
  stale on its own after `stale_after_s`.
- SOC: the logged one, unless the plant has `capacity_wh`; then it starts from the logged one at the first
  sample and each `brk` and follows the simulated battery. Either one feeds the regulator and the model's
  refusal and full hold.

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
  Compare variants with each other; use the live row only for the trend. Noise is left out on purpose: the
  `model_error` residuals are not known to be white, a guessed noise model would rank variants by the guess,
  and a seeded one makes each run depend on its seed. A fitted residual model is the open question below.
- Policy caps and floor are not in the log. A replay uses the CLI caps, else `default_max_w` (what the
  controller uses for a cap it cannot read), and the CLI floor. A step's `limit` says when a cap held but not
  its value: `output_w` is after the slew. So a live day capped by a policy limit replays without that cap.
  Exclude such spans, or set the caps. A `refused` record's `floor` is the policy floor at that moment only.
- The slow ramp starts only from an idle battery. Logs show it lasting across later commands; whether it
  can start from a running discharge, and when the inverter enters that mode, is unknown.
- A balance hold delivers less than asked; the model does not. Exclude holds with `--exclude`. Refusal and
  full holds are a hard 0 W at a hand set SOC, off by default; the real refusal SOC is not fitted. Without
  `capacity_wh` they follow the logged SOC, so a variant that drains faster than live does not reach the
  floor sooner.
- `normal` is modelled as 0 W. The inverter's own mode is not 0 W, but replay only sends `normal` when the
  regulator hands back (stale or released), and what the inverter then does is not in the log.
- The fit's discharge group mixes rises, cuts to 0 W and turns from charge, which lag differently; fractions
  above 1 (overshoot) are fitted as a plain lag.
- Older logs: a `pv` of -10 at night reads as 100 W west solar, so a night can show minutes of `clear`.

## Open questions

- Whether a noise model taken from `model_error` residuals would bring replay costs near live: unknown.
- The policy caps and floor in force per sample: not logged, unknown.
- How the correction source gates its fixed 10 W loss at a near idle battery: unknown; the bench takes it as
  any non-zero reading.
- What starts the inverter's slow ramp, and its refusal SOC: unknown, so `ramp_w_s` starts only from idle
  and `refuse_soc` is set by hand.
