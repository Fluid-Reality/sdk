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

