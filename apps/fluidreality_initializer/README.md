# Fluid Reality Actuator Initialization

Runs the long actuator initialization sequence (the same sweep as
`infinidaq/amplifier_sequence_run.py`) on Rockford boards, with up to five
actuators per board and any number of boards. Each actuator runs its own,
fully independent initialization, shows its readings live, and gets its own
report.

## Install and run

From the SDK repository root (Python 3.10 or newer):

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install -r apps\fluidreality_initializer\requirements.txt
python apps\fluidreality_initializer\app.py
```

Try it without hardware (two simulated Rockford boards, time sped up 20×):

```powershell
python apps\fluidreality_initializer\app.py --demo
python apps\fluidreality_initializer\app.py --demo --demo-speed 60
```

## Workflow

1. **+ Add board**: pick the Rockford's USB serial port. Repeat for every board.
2. Turn **Power** on for the board, then **Detect actuators** (firmware
   `DT0`/`DT1` on channels 0-4).
3. Double-click a channel tile (or select it and press **Configure…**), enter
   the **Actuator ID** and choose the sequence: a preset, an infinidaq
   `amplifier_sequence_config*.json` (**Load…**), or your own values.
4. **Save & start**, or configure several channels and use **Start all
   configured**. Every actuator can be paused, resumed or stopped on its own.
5. When a channel finishes (or is stopped/fails), its report is written to the
   report folder shown in the header. **Open report** shows the HTML report.

## What one initialization does

For every voltage × high time × repeat (infinidaq order: voltage, then high
time, then repeat):

```
[pause @ 0 V  | pre-hold @ −V for one high time]
→ num_cycles × ([+V for high time] [low for high time])     low = −V or 0 V
→ [pause @ 0 V]
```

Optional firmware checks (`DT0`/`DT1`, the same Ready/Error classification the
dashboard uses) run before and after the sweep and go into the report.

## How the board is driven

The app uses the same firmware path as the Dashboard's Initialize, Fast Init
and Square Wave tools:

* Firmware safety is switched **off** while any initialization runs on the
  board and back **on** as soon as the board is idle.
* `OUT ch pos neg` sets an output; `OUC ch top bottom ms` sets an output and
  measures current. `value = round(255 × |V| / supply)`; positive volts are
  `TOP=value, BOTTOM=0`, negative volts are `BOTTOM=1, TOP=255−value`.
* The supply is measured continuously; the DAC scale always uses the higher of
  the smoothed and latest readings, so a measurement error can only lower the
  drive voltage. Outputs are re-scaled if the supply drifts.
* Manual output bypasses the firmware's volt-time budget. The default sequences
  are balanced (+V and −V phases of equal length); every report records forward
  and reverse exposure in V·s.

### Current measurement and "control blocking"

Rockford has one current sensor for the whole board, so a reading is only
meaningful while a single actuator is driven. Current is therefore read only at
phase boundaries, and while one actuator is being read every other actuator on
that board is held at 0 V with its phase clock frozen:

1. other actuators → 0 V (clocks frozen)
2. `OUC` at the end of the old phase (counts as the old phase's drive time)
3. `OUC` all-off baseline (default 250 ms)
4. `OUC` at the start of the new phase (counts as the new phase's drive time)
5. other actuators restored

The reported delta is `current − baseline`. One thread per board issues every
command to that board, so readings never overlap. Phases of each actuator still
get exactly their configured drive time; the time spent holding for a
neighbour is shown as **Held for neighbours** and recorded in the report.

### Safety behaviour

* A 1 s supply poll keeps the firmware's 10 s control watchdog satisfied.
* A failure that belongs to one actuator (for example a firmware error on its
  output) stops only that actuator.
* Lost connection, supply loss, another client taking control, or any channel
  that cannot be confirmed at 0 V is a **board fault**: every channel is set to
  0 V (the supply is powered off if that cannot be confirmed), safety is turned
  back on, and every process on that board is stopped with a partial report.
  This deliberately puts safety ahead of isolation.
* Closing the app sets every output to 0 V, restores safety and writes partial
  reports for anything still running.

## Reports

Each actuator run gets a folder `YYYY-MM-DD/<actuator>_<board>_ch<n>_<time>/`:

| File | Contents |
|---|---|
| `report.json` | Machine-readable report (autosaved every 2 minutes while running) |
| `report.html` | Self-contained visual report with charts |
| `measurements.csv` | Every start/end reading, appended live |
| `phases.csv` | Every phase with drive time, held time and deltas, appended live |
| `config.json` | The exact sequence used (loadable back into the app) |

`report.json` (`schema: fluid-reality/actuator-initialization-report`,
`schema_version: 1`) has a flat `summary` object meant for mapping directly to
production-tracking fields (e.g. Fibery):

| Field | Meaning |
|---|---|
| `actuator_id`, `channel`, `board_label`, `board_id` | Identity (board ID is the Rockford's Bluetooth name) |
| `status` | `completed`, `stopped` or `failed` |
| `verdict` | `pass`/`fail` when a pass threshold is configured, else `null` |
| `sequence_name`, `operator`, `notes` | Context |
| `started_at`, `finished_at`, `duration_h` | Timing (local time with UTC offset) |
| `runs_completed`, `runs_planned` | Progress |
| `pre_check_state`, `pre_check_delta_ma`, `post_check_state`, `post_check_delta_ma` | Firmware checks |
| `max_voltage_v`, `max_voltage_first_delta_ma`, `max_voltage_last_delta_ma`, `max_voltage_change_pct` | Conditioning at the highest voltage |
| `final_high_end_delta_ma` | Last end-of-high-phase current delta |
| `forward_exposure_vs`, `reverse_exposure_vs`, `measurement_count` | Exposure and data volume |

The rest of the file holds the full config, per-voltage and per-run summaries,
check results, timing and the event log.

## Files

| File | Role |
|---|---|
| `app.py` | Main window and entry point |
| `engine.py` | Board thread, actuator state machines, measurement protocol, safety |
| `sequence.py` | Sequence model, presets, infinidaq config import |
| `reporting.py` | Report folder, JSON/CSV/HTML |
| `plots.py`, `widgets.py`, `dialogs.py`, `ui_style.py` | UI |
| `demo.py` | In-process simulated boards (uses `apps/fluidreality_simulator`) |
| `presets/` | Standard, pre-hold, long-hold and quick-check sequences |

Tests: `python -m pytest tests/test_fluidreality_initializer.py`.
