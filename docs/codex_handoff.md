# Codex Handoff: Fluid Reality Lansing Firmware and SDK

This note exists so a future Codex session can continue the Lansing firmware and Python SDK work without needing the original chat history.

## Repositories

There are two related repositories:

- Electronics / firmware repo: contains the Lansing Teensy firmware.
- SDK repo: contains the public Python package `fluid-reality`, import package `fluid_reality`.

Folder paths may differ between machines. Do not rely on the original local paths. Search for:

- firmware sketch: `lansing_firmware_0_1/lansing_firmware_0_1.ino`
- firmware README: `lansing_firmware_0_1/README.md`
- SDK package: `src/fluid_reality`
- SDK Lansing wrapper: `src/fluid_reality/boards/lansing.py`

## Latest Known Commits From Original Machine

These were the commits created before handoff:

- Firmware/electronics repo: `ad25995 Add Lansing actuator firmware`
- SDK repo: `4691969 Create Fluid Reality SDK with Lansing wrapper`

If these hashes are not present on the work machine, pull or push the repos first.

## 2026-06-26 Update

This section records the follow-up decisions and implementation work from the later Lansing dashboard and firmware-support session. Keep this section current because it captures behavior that came from bench testing and UX iteration, not just the initial protocol design.

Latest known repo commits after this session:

- Electronics repo: `d46e9b1 Return all Lansing config values`
- SDK repo: `452ed10 Improve Lansing dashboard Mac styling`

Important SDK commit sequence:

- `59cbbf8 Add Lansing dashboard app`
- `b15859c Harden Lansing dashboard serial status handling`
- `452ed10 Improve Lansing dashboard Mac styling`

Important electronics commit sequence:

- `70efc2e Add Lansing Codex handoff notes`
- `d46e9b1 Return all Lansing config values`

The SDK package was prepared/published as package `fluid-reality`, import package `fluid_reality`, with version `0.1.0` at the time of this update. A PyPI token was used from a local key file during the session, but do not record token contents in this handoff or in repo files.

### Firmware Change: `CFG` With No Parameters

The user requested that `CFG` with no parameters return all runtime configuration values. Current firmware behavior:

```text
CFG
OK:MAX><milliseconds>,DIS><milliseconds>,SAFE>ON,DEBUG>OFF
```

`CFG` still accepts:

- `CFG MAX`
- `CFG MAX <milliseconds>`
- `CFG DIS`
- `CFG DIS <milliseconds>`
- `CFG SAFE`
- `CFG SAFE ON|OFF|1|0`
- `CFG DEBUG`
- `CFG DEBUG ON|OFF|1|0`

`ER:CFG_PARAM_COUNT` now means more than two fields were sent. It should no longer be returned merely because `CFG` had no key.

### Lansing Dashboard App

The SDK now contains a desktop dashboard at:

```text
apps/lansing_dashboard/app.py
apps/lansing_dashboard/README.md
apps/lansing_dashboard/requirements.txt
apps/lansing_dashboard/assets/
```

Run from the SDK root:

```powershell
python -m pip install -e .
python -m pip install -r apps\lansing_dashboard\requirements.txt
python apps\lansing_dashboard\app.py
```

The dashboard adds `src` to `sys.path`, so it can also run from a local checkout before package installation.

Dashboard design decisions:

- Use the Fluid Reality logo assets in `apps/lansing_dashboard/assets`.
- Use the Fluid site-inspired palette: white surfaces, black ink, Fluid red, and blue active highlights.
- Do not use a landing page or explanation screen; first screen is the actual operational dashboard.
- The dashboard is a PySide6 app with a background worker thread for serial operations so long `INI`, `DIA`, recovery, and status polling do not block the UI.
- Controls for board telemetry, actuators, and board actions must remain disabled until a serial connection is established. Pressing Disconnect disables them again.
- Power supply on/off and output connected/disconnected use pill-style toggles in the metric cards.
- The connect bar itself remains usable while disconnected.

Mac styling decisions after user-provided screenshots:

- Do not hardcode a Windows-only font in the stylesheet. The app uses Qt's system UI font via `QFontDatabase.systemFont(QFontDatabase.GeneralFont)`.
- Use Qt `Fusion` style to make PySide widgets more predictable across Windows and macOS.
- Keep disabled text readable on macOS. Explicit disabled colors are set for labels, buttons, combo boxes, and spin boxes.
- Style `QComboBox QAbstractItemView` explicitly so macOS does not show a dark native dropdown popup against the light app.
- Give the actuator scroll viewport and grid host explicit white backgrounds so card gaps do not render as dark gutters on macOS.
- Avoid very heavy `font-weight: 800`; dashboard headline/value weights were softened to `700`.

The ugly Mac screenshot was caused by a combination of macOS Qt palette/font differences: the Segoe UI fallback rendered too heavy, disabled widget text went nearly white, the combo popup inherited a dark palette, and the actuator grid/scroll backgrounds were not explicit.

### Dashboard Actuator Grouping and Selection

Actuators are shown in three groups:

- Group 0: actuators `0..7`
- Group 1: actuators `8..15`
- Group 2: actuators `16..23`

The first group is shown by default. The user changes groups with a combo box. The dashboard should not ask for an actuator number manually. Clicking an actuator card selects it, and all board actions apply to that selected actuator.

Actuator status language is intentionally user-facing, not raw firmware-state language:

- Default before detection: `N/A`
- During detection: `Detecting`
- Detected and acceptable: `Ready`
- Missing actuator: `Not connected`
- Excessive current delta: `Error`

On disconnect, all actuator cards go back to `N/A`.

### Auto Detection Behavior

Auto detection is for the currently visible actuator group only.

Trigger detection when:

- a group is selected and the board is connected with PSU on and output connected
- the PSU becomes on and the output becomes connected

Detection procedure:

1. Confirm PSU is on and output is connected.
2. Stop square wave if it is running.
3. Command all actuators in the group off.
4. For each actuator in the group:
   - immediately set that card to `Detecting`
   - run firmware `DIA <actuator>`
   - immediately update that one card as soon as the result is available
5. Do not wait until the whole group finishes to update individual cards.

Detection thresholds:

- delta `< 0.1 mA`: `Not connected`
- delta `> 3.0 mA`: `Error`
- otherwise: `Ready`

Delta is computed from baseline and forward current. During the session the not-connected threshold moved through `.05 mA` and settled at `.1 mA`; the error threshold settled at `3.0 mA`.

The event log should be verbose during detection, including:

- group number and actuator range
- PSU/output state
- voltage/current snapshot
- thresholds
- per-actuator off command result
- per-actuator diagnostic start
- baseline, forward, discharge, delta, and classification
- final group summary

Event-log messages are HTML-escaped because strings like `<0.10 mA` otherwise disappear when appended to `QTextEdit` as rich text.

### Dashboard Action Availability

The dashboard intentionally separates selectable from operable:

- `N/A` cards may be selected, but actions stay disabled until detection classifies them.
- `Ready` actuators can be initialized, diagnosed, recovered, and square-wave driven.
- `Error` actuators can be diagnosed and recovered.
- `Not connected` actuators cannot be operated.
- Recovery should be available for working/Ready actuators as well as Error actuators.

After recovery, the user must be able to run full `Diagnose` to reclassify the actuator from Error back to Ready if the current delta is acceptable.

### Recovery Behavior

Recovery is an intentional bench/debug operation using raw manual output:

- It temporarily disables manual-output safety with `CFG SAFE OFF`.
- It alternates the selected actuator between positive manual drive and negative manual drive.
- It restores manual-output safety after recovery.
- It reports current delta every second.
- It reports final delta against baseline.

Recovery defaults:

- `50 V`
- `60 s`

Recovery voltage is user-configurable and scaled against measured PSU voltage:

```text
raw_value = int(255 * requested_recovery_voltage / measured_psu_voltage)
```

Clamp raw value to firmware output range. Example decision from the session:

```text
PSU reads 200 V
user requests 100 V recovery
raw value sent should be about 127
```

The recovery alternation sends approximately:

```text
OUT <actuator> <raw_value> 0
OUT <actuator> 0 <raw_value>
OUT <actuator> 0 0
```

Use recovery for both error-state and working actuators. Do not require an actuator to be in Error before recovery is available.

### Square Wave Behavior

The dashboard square wave is not a sine wave and not a raw `OUT` operation. It uses normal safe `ACT` writes:

1. send `ACT <actuator> 255` for full forward drive
2. wait 1 second
3. send `ACT <actuator> 0`
4. let firmware-managed discharge run
5. wait for firmware debug confirmation before reactivating

The debug confirmation used by the dashboard is:

```text
DBG:DISCHARGE_STOP,ACT>0
```

The actuator number changes per selected actuator.

If `ACT_FAILED` happens because the actuator is still locked out during discharge, the dashboard should wait and keep watching debug/status rather than crashing. The session explicitly changed behavior away from simply retrying on a fixed timer; reactivation should be based on firmware debug messages that prove discharge stopped.

The square wave runs indefinitely until the user presses Stop, All Off, disconnects, or closes the app.

### Serial Timeout and Status Misalignment

During bench use, detection timed out while waiting for `DIA`:

```text
Timed out waiting for firmware response
Status refresh failed: 'PSU'
```

The root cause was that the dashboard opened the serial transport with a timeout that was too short for blocking diagnostics. After `DIA` timed out, the later firmware response remained in the serial buffer, so subsequent `STS` reads became misaligned and the SDK tried to parse a non-status response as status.

Current dashboard mitigation:

- `SERIAL_TIMEOUT_S = 5.0`
- `Lansing(port, timeout=SERIAL_TIMEOUT_S)`

Current SDK mitigation:

- `Lansing.status()` validates required first-line `STS` fields.
- If required fields are missing, it raises a `ProtocolError` that includes the raw `STS` response lines instead of surfacing a bare `KeyError` such as `'PSU'`.

If a similar issue returns, inspect raw status lines first. Do not assume the board state is wrong until serial response alignment has been ruled out.

### Dashboard Verification Used

The following checks were used after dashboard changes:

```powershell
python -m compileall -q apps\lansing_dashboard\app.py
python -m compileall -q apps\lansing_dashboard\app.py src\fluid_reality\boards\lansing.py
python -m pytest tests\test_lansing.py
```

An offscreen PySide smoke test was also used to verify:

- initial actuator state is `N/A`
- detection can show `Detecting`
- detected-good changes to `Ready`
- disconnect resets cards to `N/A`
- event log preserves `<0.10 mA`

On this Windows machine, the offscreen Qt renderer can display square placeholder glyphs because of headless/offscreen font limitations. Do not confuse that with the macOS screenshot issue; the Mac issue was about real Qt palette/font fallback behavior.

## Hardware Model

The Lansing board controls up to 24 actuators.

- Actuators are numbered `0..23`.
- There are 3 DAC modules.
- Each DAC module has 16 DAC channels.
- Each actuator uses 2 DAC channels:
  - one positive electrode channel
  - one negative electrode channel
- DAC command values are `0..255`.
- The analog/high-voltage amplification stage is downstream of firmware and is not modeled directly in code.

## Actuator Mapping

Each module controls 8 actuators. The module assignment is sequential:

- actuators `0..7`: module `0`
- actuators `8..15`: module `1`
- actuators `16..23`: module `2`

Within each module, the actuator-to-channel map is:

| Module actuator index | Positive channel | Negative channel |
|---:|---:|---:|
| 0 | 1 | 3 |
| 1 | 5 | 7 |
| 2 | 6 | 4 |
| 3 | 2 | 0 |
| 4 | 9 | 11 |
| 5 | 13 | 15 |
| 6 | 14 | 12 |
| 7 | 10 | 8 |

For example:

- actuator `0` maps to module `0`, positive channel `1`, negative channel `3`, CS `14`
- actuator `1` maps to module `0`, positive channel `5`, negative channel `7`, CS `14`

## Critical Firmware Semantics

This is the most important contract.

Normal actuator operation is forward-only:

```text
ACT / stream forward:
positive electrode = value
negative electrode = 0
```

Reverse/negative drive must only happen in two cases:

1. firmware-managed discharge
2. raw manual `OUT`

Firmware-managed discharge:

```text
positive electrode = 0
negative electrode = 255
```

`OUT` is raw/manual and can directly set either electrode:

```text
OUT <actuator> <positive> <negative>
```

Do not reintroduce the old Peoria paired behavior into normal `ACT` or stream mode. The old Peoria behavior was:

```text
positive = value
negative = 256 - value
```

That behavior was explicitly removed for Lansing normal operation because stream mode must stream only positive forward values. Negative values are for discharge or `OUT` only.

## State Machine

Actuator states:

| Value | Name | Meaning |
|---:|---|---|
| 0 | `ACTUATOR_IDLE` | no forward drive and not discharging |
| 1 | `ACTUATOR_FORWARD_ACTIVE` | forward value is greater than 0 |
| 2 | `ACTUATOR_DISCHARGING` | reverse discharge is running |

When an actuator transitions from forward active to disabled:

1. measure active duration
2. increment total runtime
3. store runtime to EEPROM
4. compute discharge time:

```text
discharge_ms = min(active_time_ms, actuatorDischargeTimeMs)
```

5. drive discharge:

```text
positive = 0
negative = 255
```

6. after discharge time expires, return to idle:

```text
positive = 0
negative = 0
```

During discharge:

- the actuator cannot be re-enabled
- `ACT` for that actuator fails
- stream packets for that actuator are ignored/refused by the normal state machine
- `OUT` can still manually override because it is a raw bench/debug command

## Maximum Active Time

`maxActuatorActiveTimeMs` limits continuous forward activation.

Important behavior:

- The active timer starts when the actuator first goes from `0` to `>0`.
- Repeated nonzero `ACT` or stream updates do not reset the active timer.
- If active time reaches `maxActuatorActiveTimeMs`, firmware forces the actuator into discharge.
- Binary stream servicing calls the discharge service so max-active enforcement is not starved while stream bytes are flowing.

## Persistent Storage

The firmware uses Teensy EEPROM for:

- per-actuator total runtime
- persistent config values:
  - max active time
  - discharge time

The firmware uses an EEPROM magic value so first-boot boards do not load garbage runtime/config values. Firmware upgrades should not reset runtime totals if the magic value is valid.

Non-persistent config:

- `SAFE`: always boots on
- `DEBUG`: always boots off

## Power Supply Pins

Two pins control the high-voltage power system:

- `PIN_HV_EN = 5`: turns the high-voltage power supply on/off
- `PIN_HV_CTRL = 23`: connects/disconnects PSU output toward actuators

On boot, both start low/off:

```text
PIN_HV_EN = LOW
PIN_HV_CTRL = LOW
```

Rules:

- `PSC ON` is rejected if PSU is off.
- Normal actuator writes require PSU on.
- Normal actuator writes require PSU connected.
- `INI` and `DIA` require PSU on and connected.
- `OUT` does not require PSU on/connected; it is gated by `SAFE`.

## Firmware Commands

All text commands are 3 letters. Responses start with:

- `OK:` for success/data
- `ER:` for errors
- `DBG:` for debug information

Commands:

| Command | Meaning |
|---|---|
| `VER` | firmware/protocol version |
| `PSU` | turn PSU on/off or read PSU state |
| `PSC` | connect/disconnect PSU output or read connection state |
| `VLT` | read PSU/output voltage |
| `CUR` | read current draw |
| `ACT` | safe normal forward-only actuator control |
| `OUT` | manual/debug raw electrode control |
| `INI` | initialize one actuator |
| `DIA` | diagnose one actuator current behavior |
| `TIM` | read actuator runtime totals |
| `RST` | reset all actuator runtime totals |
| `RBT` | reboot the Lansing board |
| `CFG` | read/write config |
| `STS` | full status snapshot |
| `STR` | enter binary stream mode |

Important distinction:

- `RST` resets all runtime totals.
- `RBT` reboots the board.

## Binary Stream Mode

Enter stream mode with:

```text
STR
```

After `OK:STR`, send 2-byte binary packets:

```text
byte 0 = actuator number, 0..23
byte 1 = actuator value, 0..255
```

Exit stream mode:

```text
[255, 0]
```

Stream mode uses the normal safe forward-only path:

```text
positive = value
negative = 0
```

It still applies:

- runtime tracking
- max active time enforcement
- automatic discharge
- discharge lockout
- PSU on/connected checks

Stream mode should not intentionally drive the reverse electrode high during normal waveform samples.

## SDK Structure

Python package:

```python
import fluid_reality
from fluid_reality import Lansing
```

Main files:

- `src/fluid_reality/boards/lansing.py`: Lansing wrapper
- `src/fluid_reality/boards/lansing_errors.py`: Lansing firmware error catalog
- `src/fluid_reality/protocol.py`: shared OK/ER/DBG protocol parser
- `src/fluid_reality/transport.py`: pyserial transport
- `src/fluid_reality/errors.py`: SDK exceptions
- `tests/test_lansing.py`: tests
- `examples/`: example scripts

The SDK is structured so other board wrappers can be added later under `src/fluid_reality/boards`.

## SDK Lansing Wrapper

Important methods:

- `version()`
- `firmware_version()`
- `power_supply(state=None)`
- `psu_on()`
- `psu_off()`
- `connect_power(state=None)`
- `psc_on()`
- `psc_off()`
- `voltage(measurement_ms=None)`
- `current()`
- `set_actuator(actuator, value)`
- `get_actuator(actuator)`
- `get_actuators()`
- `all_actuators_off()`
- `manual_output(...)`
- `set_manual_output(...)`
- `get_manual_output(...)`
- `manual_outputs()`
- `initialize_actuator(actuator)`
- `diagnose_actuator(actuator)`
- `runtime(actuator=None)`
- `reset_runtimes()`
- `reboot()`
- `config(key, value=None)`
- `max_active_time_ms(value=None)`
- `discharge_time_ms(value=None)`
- `safety(enabled=None)`
- `firmware_debug(enabled=None)`
- `read_config()`
- `status()`
- `enter_stream_mode()`
- `stream_actuator(actuator, value)`
- `stream_values({actuator: value})`
- `stream_sine(...)`
- `exit_stream_mode()`
- `force_text_mode()`

## Debug Logging

Firmware debug is controlled by:

```python
board.firmware_debug(True)
board.firmware_debug(False)
```

SDK debug collection:

- `board.debug_lines`
- `board.flush_debug_lines()`
- constructor `debug_callback=...`
- constructor `debug_logger=...` and `log_debug_messages=True`

## Examples

Example scripts:

- `01_basic_actuator_current.py`
- `02_initialize_and_diagnose.py`
- `03_stream_sine.py`
- `04_debug_logging.py`
- `05_status_snapshot.py`
- `06_manual_output_bench_test.py`
- `07_error_handling.py`
- `08_actuator_pulse_until_key.py`

Important sine-stream behavior:

- `stream_sine()` defaults to `1..255`
- it avoids `0` during the waveform because `0` means disable/discharge
- it sends final `0` at the end to trigger discharge
- for long tests, raise `CFG MAX` or use SDK `--max-active-ms` in the example, otherwise firmware correctly forces discharge mid-waveform

Example:

```powershell
python examples\03_stream_sine.py COM9 --actuator 0 --duration-s 10 --frequency-hz 1 --update-hz 200 --minimum 1 --maximum 255 --max-active-ms 12000 --print-values
```

## Verification Commands

Firmware compile:

```powershell
arduino-cli compile --fqbn teensy:avr:teensy41 <path-to-lansing_firmware_0_1>
```

On the original Windows machine, `arduino-cli` was installed under:

```powershell
$env:LOCALAPPDATA\Programs\arduino-cli
```

SDK tests:

```powershell
cd <sdk-repo>
python -m pytest
python -m compileall -q src examples tests
```

## Hardware Debugging Notes

If reverse/discharge seems wrong, use `OUT` to test raw electrode directions:

```text
CFG SAFE OFF
OUT 0 255 0
OUT 0 0 255
OUT 0 0 0
```

For actuator `0`:

```text
positive channel = 1
negative channel = 3
CS = 14
```

Discharge should command:

```text
OUT-equivalent: positive=0, negative=255
```

Remember that neither electrode goes below board ground by itself. Negative actuator drive is a differential measurement:

```text
Vactuator = Vpositive - Vnegative
```

Use a differential probe or two scope channels with math. Be careful with grounded oscilloscope probe clips.

## Notes For Future Codex

Before making changes:

1. Read this handoff.
2. Read the current firmware `.ino`.
3. Read the current SDK `lansing.py`.
4. Do not assume old chat context is still correct if code differs.

Preserve these user decisions:

- command names should be 3 letters
- responses should start with `OK:` or `ER:`
- debug lines should start with `DBG:`
- actuators are numbered `0..23`
- normal `ACT` and stream are forward-only
- reverse is discharge-only, except raw `OUT`
- `OUT` is for bench testing and should remain safety-gated
- `RST` resets runtimes
- `RBT` reboots the board

