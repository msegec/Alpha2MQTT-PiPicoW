# Design history of the fork's control system

Why the dispatch path, the regulator, the idle hold and the step timing look the way they do, from commit messages and diffs.

Last verified: 75e08a8, 2026-10-21; rows for the solar feed forward and offset correction added 2026-10-22.

Scope: fork commits from ec388dc onward. Upstream (dxoverdy, up to febfd78, 2024-05-16) is out of scope.
Dates are author dates. Several commits authored 2026-10-13 and 10-14 sit after 2026-10-17 commits in history, so the branch was rebased. Commit 3a6e951 cites `5e56998`, which is not an ancestor of HEAD: it is a pre-rebase copy of 66e7f49.

## Timeline

| Phase | Dates | Key commits | What changed and why |
|---|---|---|---|
| 1. Pico W port | 10-12 | ec388dc, d03fdb1, 2fba12f, 99a1bdf | Pico W target, credentials to gitignored `Secrets.h`, HA package reads state JSON directly (no Node-RED), energy totals every minute, PV1 to PV6 reads dropped (AC coupled SMILE-B3 has none). |
| 2. Bus speed and fast state | 10-13, 10-14 | 9102d0c, 4fb5457, 1d2e5fc, c86f28c, 3637105 | RS485 polled every 1 ms instead of 50 ms sleeps. MQTT and OTA serviced between schedule reads. `state/fast` every second for a closed loop. `seq` and `ms` echoed on set responses. Payload bound and SOC rounding in `Dispatch.h`. |
| 3. Regulator introduced | 10-13, 10-14 | 3ba305d, c0d7bcb, 50850bf, 2076fcb, 24f5a85, 8feffdc | Pure Python `Regulator` replaces a 30 s template automation. HA integration steps it per sample. Grid estimate mode, fixed policy, step log, policy `grid_offset`. |
| 4. Inverter findings, v1.28 | 10-17 | f5de99e, 53b10d3, 05cd6b2, bc74137 | Five byte exception parsed as slave error. SMILE-B3 settings block gated by period flag 0x084F. Fork release 1.28. |
| 5. Fail safe and fast turns | 10-17 | 4aa846d, e0e291d, 754c81d, ceeaf2f, 7b759af | `both` policy, flip dwell 60 s to 5 s. Stale input hands back at once. Acked import cuts go early. Min interval floor 2 s. Default topic `state/fast`. |
| 6. Power-only writes, input gaps | 10-18 | 672066a, 1a09f99, a3a20a7, 70fe0e2, 06a81f5, 9630537 | Lone PV meter 0 held back (`PvMeter.h`). `powerOnly` rewrites only 0x0881 (`dispatchPowerOnly`). Controller uses it via Power write gap. Short gaps hold the last command. Import gain tried and removed. |
| 7. Floor and sample alignment | 10-19 | 7603b02, 8aa10b6, 19c820f | 0 W hold at the discharge SOC. Step waits for the correction rendered from the same `seq`. Step log records every fast message and every request. |
| 8. Slow discharge start | 10-20 | b47c99a, 9490f53, f333100, 15aaddd, 9452d4a | Start hold tried and reverted. Keep warm added. 0 W keeps the block kind. Floor margin 1.5 %. Refusal detector. Arrival gate on the integral. One frame block write. |
| 9. Trim gate, diagnosis, cleanup | 10-21 | 42e3727, cff2ebf, 42a9c10, 0af19ef, 8259b36, dd0ea1e | Arrival gate one sided. Full battery holds 0 W. Limit, Battery, Sent, Integral sensors. 10 s ack timeout. Refactors proven by a 3000 seed differential fuzz. Options flow. |
| 10. Firmware hardening | 10-21 | 95f3733, 9268ba4, 1ff3b82, 80ef485, 73d2899, f25975b, c90b7f3, 2f5e1c7, d49930f, 9c3a983 | Buffer bounds and reply matching (`ModbusFrame.h`), `timerDue` wrap safe (`Timer.h`), range check, cache cleared when dispatch stops, refused frame latch, `writeFullDispatchBlock`. |
| 11. Network and watchdog | 10-21 | 66e7f49, 458e9a4, 3a6e951, 86412e8, 65f3526 | Reconnects return instead of blocking. 8 s hardware watchdog. WiFi join bounded to 3 s. Rejoin on lost link via `wifiJoined`. |
| 12. Idle hold, v1.29 | 10-21 | 5bd64cc, 3ed37e9, 7766762, 4bc74a9 | Pico holds the battery idle while HA is gone (`IdleHold.h`), 180 s after the will. Release 1.29. |
| 13. Ready floor and step timing | 10-21 | 9ef2224, 7a07eab, 9684b66, 93576af, 75e08a8 | Keep warm becomes a ready floor. Large steps timed (`response.py`). Refusal near the floor latches until SOC rises. Small trims wait 6 s. |

## Problems hit and how they were solved

| Problem | Symptom as recorded | Root cause as recorded | Fix |
|---|---|---|---|
| Slow RS485 | A set/charge took 1918 ms. | `checkForData` slept in 50 ms steps, plus 1600 ms of fixed gaps between five writes. | 9102d0c (1 ms poll), 9452d4a (one nine register frame, about 50 ms) |
| Loop blocked by schedules | Commands and OTA waited about 2 s for the 10 s schedule. | About 30 reads ran back to back. | 4fb5457 (`serviceBetweenReads`) |
| Missed loads | A 2 kW load cycling every minute was mostly missed. | Template automation sent a setpoint every 30 s. | 3ba305d, c0d7bcb |
| Clamp not a grid reading | `scale x clamp` was wrong on the first install. | Clamp saw only the battery inverter, with an idle offset and gain error. | 50850bf (`grid_source` estimate: correction minus battery, `estimate_grid`) |
| Force modes regulated | Force charge and discharge were regulated, not obeyed. | Policy reached the controller as plain charge or discharge. | 50850bf (policy attribute `fixed: true`, FIXED mode) |
| Load step split | One load step went out as two commands a Min gap apart. | Slew was limited per step, and steps come several a second. | 2076fcb (slew counted from the last command sent) |
| Slow turn round | A load edge waited up to 30 s for the policy and 60 s for the dwell. | Policy allowed one direction; `flip_dwell_s` was 60 s. | 4aa846d (`both`, `other_soc`, dwell 5 s) |
| Held stale setpoint | Stale input held the last watts until the 120 s lease ran out. | 3ba305d sent nothing when stale. | e0e291d (`_hand_back` sends normal on the first stale step) |
| Import on cloud dips | Import cuts waited the full minimum interval. | Interval sized for five-write blocks. | 754c81d (`_may_send_early` after ack and a fresh sample) |
| Command queuing | Below about 2 s a command queued behind the last one's writes. | Five writes took about 1.8 s. | ceeaf2f (2 s floor, `MIN_SEND_GAP_S`) |
| Lone PV 0 | PV meter read exactly 0 for one read while generating, valid CRC. | Inverter behaviour; cause unknown. | 672066a (`pvMeterZeroUnconfirmed`, `unconfirmedZero`) |
| Hand back on a blip | Template reload or missing key handed the inverter back; house bought from grid. | Any missing input released at once. | 70fe0e2 (hold last command until `stale_after_s`, integration capped by `MAX_INTEGRATE_S`) |
| Commands at the floor | A few commands a minute while the battery sat at 0 W at its discharge SOC. | Integral and error grew a request the inverter would not serve. | 7603b02 (hi = 0 at the SOC limit), 15aaddd (`FLOOR_MARGIN` 1.5 %) |
| Grid estimate skew | Step paired this second's battery with last second's net demand. | Correction template had not rendered yet. | 8aa10b6 (correction `seq` attribute, `MATCH_WAIT_S` 0.5 s) |
| Slow discharge start | A 2 kW load came from the grid for 3 minutes. Discharge from 0 W ramped about 10 W/s; a step from a running discharge took 3 s. | Not the power write: block-only starts crept the same (9490f53). Starting from idle is slow. | f333100 (Keep warm), 9684b66 (ready floor) |
| Block write per idle crossing | Every crossing between idle and a small discharge was a full block write. | 0 W went out as kind charge. | 15aaddd (0 W keeps the running block's kind, `_command`) |
| Refused discharge above the floor | House on the grid for the last percent. Inverter gave nothing against a request. | Inverter cuts discharge about 1 % above its floor. | 15aaddd (`_refused`, `REFUSED_*`), 75e08a8 (latch near the floor) |
| Integral wind after a start | A charge wound up after each start. | Integral trimmed inverter lag as error. | 15aaddd (`LAG_W`, `ARRIVE_S` gate) |
| Integral never ran | About 190 W exported in simulation at 3 kW. | Gate was two sided: a battery reading over its setpoint (DC over AC) never arrived. | 42e3727 (gate only for a battery short of setpoint) |
| Full battery winds integral | Export it could not store came back as import at the next load. | Full battery still asked to charge. | 42e3727 (lo = 0 at `charge_soc`, limit `full`) |
| Lost commands | No fail, so no full resend, during a Pico reconnect. | No response timeout. | cff2ebf (`ACK_TIMEOUT_S` 10 s once firmware echoes seq) |
| Late publish after off | A command could publish after the release and leave a lease nobody ends. | Publish raced the switch. | cff2ebf (superseded publish dropped) |
| Silent release | Unknown policy state released with status "released" only. | No reason carried. | 8259b36 (`Policy.reason`) |
| Stale reply accepted | A grid reply after the next flush passed as the battery reading. | No reply-to-request check. | 9268ba4 (`replyMatchesRequest`, quiet flush) |
| Ignored power write | Power-only write after the lease ended was ignored. | `_activeDispatch` cache trusted after dispatch stopped. | c90b7f3 (cleared when fast state reads not start) |
| Five-write latch too eager | One bad command cost about 1.6 s per later block write until reboot. | Any exception to the frame latched the fallback. | 2f5e1c7 added latch; d49930f latches only when the five writes then land (`_blockFrameRefused`) |
| HA down, inverter on its own | Battery ran self-consumption when HA stopped. | Controller handed back on stop. | 5bd64cc (idle hold) |
| Idle hold on planned restart | Every HA restart put the house on the grid. | Hold started at the will; restart returns in about 2 min. | 3ed37e9 (`HA_GONE_AFTER_MS` 180 s) |
| Boot loop with AP down | Pico rebooted every 8 s until the AP returned. | `WiFi.begin` blocks up to 15 s in `CYW43::begin`; nothing feeds the watchdog. | 3a6e951 (`WIFI_JOIN_TIMEOUT` 3000 ms) |
| Dead after AP reboot | Broker attempts failed for good; idle hold wrote 0 W; OTA unreachable. | lwIP keeps the address after link loss, so `WiFi.status()` stays connected. | 86412e8 (`wifiJoined` reads `cyw43_wifi_link_status`) |
| Retry at cutoff | An inverter at its cutoff was asked again each minute for about 2 h. | `REFUSED_HOLD_S` retry. | 75e08a8 (`REFUSED_NEAR_SOC` 3, `REARM_SOC` 1) |
| Integral holds the inverter offset | About -155 W of integral after a 2 kW load, then about +175 W of import decaying over 3 min once it stopped. | The inverter lands off its setpoint by a level dependent offset (-25 W to +220 W); one integral holds one level. | 2026-10-22 (`INVERTER_OFFSET`, `offset_comp`) |
| Trim noise on the bus | Three commands in four moved under 50 W. | Noise and integral creep. | 75e08a8 (`SMALL_TRIM_W` 50, `TRIM_GAP_S` 6 s; replay 810 to 531 commands an hour for 0.6 W more mean import) |

## Tried and reverted or replaced

Do not retry these without new evidence.

| Approach | Added | Removed or replaced | Why it went |
|---|---|---|---|
| Template automation, setpoint every 30 s | before fork control work | 3ba305d, c0d7bcb | Missed minute-scale loads. |
| Stale input sends nothing, lease runs out | 3ba305d | e0e291d | Up to 2 min at a setpoint for a load that had gone. |
| Release on the first missing input | e0e291d | 70fe0e2 | A one second gap put the house on the grid. |
| Per-step slew limit | 3ba305d | 2076fcb | Split a load step into two commands. |
| 60 s flip dwell | 3ba305d | 4aa846d | Turn round too slow; now 5 s behind the 150 W margin. |
| `unsend()` (forget a failed command) | e0e291d | a3a20a7 (`fail()`) | A release in the meantime must still send normal. |
| Import gain (kp multiplier on import) | 06a81f5 | 9630537 | Live grid swung more. kp 1 already corrects the whole error per step. The favouring replay used a log that omitted small moves. |
| Block writes until a discharge has started | b47c99a | 9490f53 | Block-only starts crept at the same 10 W/s. It only cost 1.8 s bus per block. |
| Power-only window of a third of the lease | 1a09f99 | 9452d4a (half) | With 120 s lease and 40 s keepalive, a third refused a third of power changes. |
| Warm band equal to the 150 W flip margin | f333100 | 15aaddd (`WARM_BAND_W` 50) | Threw away surpluses up to 150 W the battery could store. |
| No warm on top of an unstorable surplus | 42e3727, 9ef2224 | 9684b66 | Ready floor now holds through it: "a slow start costs more than the floor's watts". 9ef2224 had measured the extra export at crossovers in replay. |
| Two sided arrival gate on the integral | 15aaddd | 42e3727 | Never ran for a battery reading past its setpoint. |
| Refused discharge retried every 60 s near the floor | 15aaddd | 75e08a8 | Asked into a refusal for hours. |
| Refusal threshold of requests over 200 W | 15aaddd | 75e08a8 (`REFUSED_ASK_W` 100) | Smaller requests also refused. |
| Five single writes as the only dispatch path | upstream | 9452d4a | 1.8 s per block with the fast state paused. Kept as fallback. |
| Latch fallback on any frame exception | 2f5e1c7 | d49930f | Value errors and busy replies latched it. |
| `checkTimer` restarting from the late time | upstream | 9452d4a, 73d2899 (`timerDue`) | Phase drift and early runs after the millis() wrap. |
| Blocking WiFi and MQTT reconnect loops | upstream | 66e7f49 | Boot stopped with AP down; broker outage stopped dispatch. |
| HA gone 60 s after disconnect, hold at the will | 5bd64cc | 3ed37e9 | Planned restarts triggered it. |
| Display refresh calling `_mqtt.loop()` | upstream | d45d5c0 | Could run `mqttCallback` inside a half built payload. |
| Smaller flip margin than 150 W | replay only | 9ef2224 | Did not help in replay and added flips. |
| Feed forward on a `load_entity` change since the last sample | 3ba305d | solar feed forward, 2026-10-22 | Never ran: no load entity was set, `load_w` null in every step. With 1 s samples that already carry the load, it would count the change twice. |

## Rules that look arbitrary

| Rule (symbol) | Lesson | Commit |
|---|---|---|
| Five-write fallback order START, ACTIVE_POWER, TIME, SOC, MODE, 400 ms apart (`writeFullDispatchBlock`) | No other order is proven on hardware. | 9c3a983 comment |
| Requests over 127 bytes rejected, never truncated (`MQTT_REQUEST_PAYLOAD_MAX`) | A cut payload can parse as a different command. Same for keys or values over 31 chars. | 3637105, 1ff3b82 |
| SOC rounded up at 0.4 % per bit (`dispatchSocFromPercent`) | Truncation sent 24.8 % for 25 %. | 3637105 |
| `kp` 1.0, no D term | Plant is dead time with quantised readings. kp above 1 overshoots on real noise. | 3ba305d, 9630537 |
| Request starts from battery now (`request = battery_w + kp * e + ...`) | Lands on house demand whatever command is in flight. | 3ba305d |
| `MIN_SEND_GAP_S` 2.0 even for acked commands | Closer, a command queues behind five-write blocks. | ceeaf2f |
| `FLOOR_MARGIN` 1.5 | Inverter cuts discharge about 1 % above its floor. | 15aaddd |
| `REFUSED_AFTER_S` 20 | Every start logged since 10-17 left 10 W within 5.1 s, so 20 s is not a slow start. | 15aaddd, 75e08a8 comment |
| `REFUSED_ASK_W` 100 | A battery reads about 35 W under a 100 to 400 W command. | 75e08a8 |
| `LAG_W` 100, `ARRIVE_S` 15 | Gap after a command is lag, not error. Timeout covers an inverter that clamps below request. | 15aaddd, 42e3727 |
| `MAX_INTEGRATE_S` 10 | Integral must not wind on an error nobody measured. | 70fe0e2 |
| 0 W in the running block's kind (`_command`) | Idle must not cost a block write. | 15aaddd |
| Rise to the ready floor sent however small (`to_floor` in `_maybe_send`) | Otherwise the battery idles until the keepalive. | 9684b66 |
| Rounding never takes a discharge under the floor (`_command`) | 10 W rounding could drop under `warm_w`. | 9684b66 |
| Power-only writes stop at half the lease (`dispatchPowerOnly`) | Inverter returns to normal within about 1.5 durations of the last full write even if a power write restarts its timer. | 9452d4a, README |
| Release before the first command sends normal (`_hand_back`, `unknown`) | A crash may have left a lease running. | 42e3727, 42a9c10 |
| Idle hold is charge 0 W to 100 % for 120 s, rewritten every 60 s | One late rewrite must not let it lapse. 0 W charge stores nothing, so surplus exports. | 5bd64cc |
| Any set request counts as HA up (`idleHoldCommand`) | HA birth is non-retained by default, so the Pico can miss it. Retained birth and will recommended. | 5bd64cc, 7766762 |
| Watchdog fed once per RS485 exchange, not per byte | A line that never stops sending must still reboot. | 458e9a4 |
| Prefer a dotted broker address | Hostname lookup adds 2.5 s, about 7 s of the 8 s watchdog. | 65f3526 |
| Do not reissue `WiFi.begin` while DHCP is pending | `_beginInternal` calls `end()` and leaves the AP. | 3a6e951, 86412e8 |
| Lone PV 0 needs a second read (`PV_METER_ZERO_CONFIRM_FROM_W` 10) | A real reading reaches 0 through small values. | 672066a |
| Correction older than 90 s is missing (`CORRECTION_MAX_AGE_S`), readings over 20 kW ignored (`MAX_PLAUSIBLE_W`) | Frozen or implausible inputs must not drive the battery. | e0e291d |
| Tuning numbers are entities, not in the options form | A second copy would be a second owner of each value. | dd0ea1e |
| Step log records every fast message | A replay on a log that skipped small moves picked the wrong gain. | 19c820f, 9630537 |

## Merges from agent worktrees

- 93576af merges `worktree-agent-ae5f3517bbc9b1628` (7a07eab) into `worktree-agent-a3a2ea11da8edba5e` (9684b66). Both branch from 9ef2224.
- 7a07eab side: `response.py` (step timer: steps over `STEP_W` 500 W timed to `REACH` 80 %, `SLOW_S` 4 s counts as slow), a slow steps sensor, `resp` step log record, `test_response.py`. No command change.
- 9684b66 side: ready floor in `Regulator.step`, `_command` and `_maybe_send`; Keep warm step 10 W to 5 W in `number.py`.
- `git show --cc 93576af` shows no hand resolution: the merge was clean.
- No other merge commits in the fork range.

## Doc drift seen at 75e08a8

- README controller bullet still says a refusal needs a discharge over 200 W. Code says `REFUSED_ASK_W` 100 (75e08a8).
- README Keep warm text describes the pre-9684b66 behaviour (surplus not added to). 9684b66 changed no README.
- `MIN_SEND_GAP_S` comment and `min_interval_s` default 3.0 still cite five-write timing, after 9452d4a made a block about 50 ms.

## Open questions

- Whether the inverter acts on a power-only write mid dispatch: 1a09f99 says untested on hardware. 9630537 mentions "power writes landing every second" live, so it seems to work. Guess, not verified.
- Whether `MIN_SEND_GAP_S` 2 s and the 3 s default can drop now that one frame writes land. Unknown.
- Cause of the lone PV meter 0 reads. Unknown.
- Why discharge from idle ramps slowly. 9684b66 says the discharge ceiling restarts low and climbs a few W/s. Mechanism inside the inverter unknown.
- Whether 9684b66 re-ran the replay that justified 9ef2224's no-warm-on-surplus rule. Unknown.
- Live values of `power_write_gap_s` and `warm_w` (both default 0). Unknown from the repo.
- Whether the five-write fallback has ever run on the deployed unit. Unknown.
