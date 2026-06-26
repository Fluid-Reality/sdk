# Fluid Reality SDK

Public Python SDK for Fluid Reality hardware. Provides tools for connecting to Fluid Reality boards, controlling actuator outputs, reading diagnostics, managing device configuration, and building higher-level hardware control workflows.

This package is structured so board-specific wrappers can be added over time. The first wrapper is `Lansing`, which speaks the Lansing firmware serial protocol.

## Installation

For local development:

```powershell
cd C:\research\FluidReality\sdk
python -m pip install -e .
```

For tests:

```powershell
python -m pip install -e . pytest
python -m pytest
```

## Quick Start

```python
from fluid_reality import Lansing

with Lansing("COM5") as board:
    print(board.version())

    board.power_supply(True)
    board.connect_power(True)

    board.set_actuator(0, 180)
    value = board.get_actuator(0)

    board.set_actuator(0, 0)

    print("voltage", board.voltage())
    print("current", board.current())
```

## Lansing Wrapper

`Lansing` uses actuator numbers `0` through `23`, matching the firmware.

Create a board object from a serial port:

```python
from fluid_reality import Lansing

board = Lansing("COM5")
```

Or use it as a context manager so the serial port closes automatically:

```python
with Lansing("COM5") as board:
    print(board.firmware_version())
```

### Command Coverage

The wrapper exposes the full Lansing text-command surface:

- `version()` returns firmware/protocol identity fields.
- `firmware_version()` returns a typed `LansingVersion`.
- `power_supply(state=None)` reads or sets the PSU state.
- `psu_on()` turns the PSU on.
- `psu_off()` turns the PSU off.
- `is_psu_on()` reads the PSU state and returns `True` or `False`.
- `connect_power(state=None)` reads or sets the PSU output connection state.
- `psc_on()` connects PSU output toward the actuator side.
- `psc_off()` disconnects PSU output.
- `is_power_connected()` reads the PSU output connection state and returns `True` or `False`.
- `voltage(measurement_ms=None)` reads PSU/output voltage.
- `current()` reads current draw.
- `set_actuator(actuator, value)` writes a normal forward-only actuator value.
- `get_actuator(actuator)` reads one normal actuator value.
- `get_actuators()` reads all normal actuator values.
- `all_actuators_off()` writes `0` to every actuator through the normal safe actuator path.
- `manual_output(actuator, positive, negative)` writes raw electrode values for bench testing.
- `manual_output(actuator)` reads one raw electrode pair.
- `set_manual_output(actuator, positive, negative)` is an explicit write alias for manual output.
- `get_manual_output(actuator)` is an explicit read alias for manual output.
- `manual_outputs()` reads all raw electrode pairs.
- `initialize_actuator(actuator)` runs the firmware initialization routine.
- `diagnose_actuator(actuator)` runs the firmware current diagnosis routine.
- `runtime(actuator=None)` reads one runtime total or all runtime totals.
- `reset_runtimes()` resets all runtime totals.
- `reboot()` asks the Lansing board to reboot.
- `config(key, value=None)` reads or writes firmware config values.
- `max_active_time_ms(value=None)` reads or writes the maximum active time config.
- `discharge_time_ms(value=None)` reads or writes the discharge time config.
- `safety(enabled=None)` reads or writes the manual-output safety flag.
- `firmware_debug(enabled=None)` reads or writes the firmware debug-message flag.
- `enable_firmware_debug()` enables firmware `DBG:` output.
- `disable_firmware_debug()` disables firmware `DBG:` output.
- `read_config()` returns a typed `LansingConfig`.
- `status()` returns the multi-line firmware status snapshot as a Python dictionary.
- `enter_stream_mode()` switches the board to binary stream mode.
- `stream_actuator(actuator, value)` writes one binary stream packet.
- `stream_values({actuator: value})` writes multiple binary stream packets.
- `stream_sine(...)` streams a sine wave and returns the achieved refresh rate. By default it streams `1..255` and sends `0` only at the end to trigger discharge. Pass `value_callback=callable` to receive each `(actuator, value, elapsed_s)` sample as it is sent.
- `exit_stream_mode()` exits binary stream mode.
- `flush_debug_lines()` returns captured debug lines and clears the local buffer.
- `force_text_mode()` tries to recover a clean text-command boundary after an interrupted binary stream run.
- `raw_command(command, *params)` is available when you need direct access to a firmware command before the SDK grows a typed helper for it.

### Debug Messages

Firmware debug lines start with `DBG:`. The SDK always consumes these lines before returning the final `OK:` response or raising the final `ER:` response.

Captured debug lines are available through:

```python
board.debug_lines
```

To receive each line live:

```python
with Lansing("COM5", debug_callback=print) as board:
    board.firmware_debug(True)
```

To route debug lines through Python logging:

```python
import logging

logger = logging.getLogger("fluid_reality.lansing")

with Lansing("COM5", debug_logger=logger, log_debug_messages=True) as board:
    board.firmware_debug(True)
```

`firmware_debug(True)` enables the board-side `DBG:` messages. `log_debug_messages=True` controls whether the SDK writes received `DBG:` lines to the Python logger.

## Lansing Firmware Command Reference

This section documents the Lansing firmware serial protocol directly. It is included here so SDK users do not need access to the private firmware repository.

The firmware communicates over the Teensy USB serial port at `250000` baud. The SDK uses that baud rate by default.

The firmware starts in text command mode. Text commands are newline-terminated ASCII lines. Command names are exactly three characters and are case-insensitive because the firmware uppercases the command internally.

Accepted parameter separators are spaces, tabs, and commas. For example, these are equivalent:

```text
ACT 5 180
ACT,5,180
```

Every command result line starts with one of these prefixes:

- `OK:` for success or returned data
- `ER:` for errors

When a response contains several unnamed values, values are comma-separated:

```text
OK:0,0,180,0
```

When a response contains values with different meanings, fields use `NAME>value`:

```text
OK:PSU>ON,PSC>ON,VLT>218.45,CUR>12.34
```

Firmware debug lines start with `DBG:`. Debug lines are informational and can appear before the final `OK:` or `ER:` line for a command. The SDK consumes those lines automatically.

Actuators are numbered `0` through `23`. Actuator output values are `0` through `255`.

### `VER`

Returns the firmware identity and protocol version.

Request:

```text
VER
```

Response:

```text
OK:FW>Lansing,VERSION>0.1,PROTO>0.1
```

Errors:

```text
ER:VER_PARAM_COUNT
```

SDK helpers:

- `board.version()`
- `board.firmware_version()`

### `PSU`

Turns the high-voltage power supply on or off, or reads the tracked PSU state.

Requests:

```text
PSU
PSU ON
PSU OFF
PSU 1
PSU 0
```

Responses:

```text
OK:ON
OK:OFF
OK:PSU_ON
OK:PSU_OFF
```

`PSU` with no parameter returns `OK:ON` or `OK:OFF`. `PSU ON`, `PSU 1`, `PSU OFF`, and `PSU 0` return acknowledgements.

Errors:

```text
ER:PSU_PARAM_COUNT
ER:PSU_PARAM_VALUE
```

Notes:

- Turning the PSU on enables the high-voltage supply control pin.
- Turning the PSU off disables the high-voltage supply control pin.
- Turning the PSU off does not automatically disconnect the PSU output state. For a full shutdown sequence, disconnect output first or call both `PSC OFF` and `PSU OFF`.

SDK helpers:

- `board.power_supply()`
- `board.power_supply(True)`
- `board.power_supply(False)`
- `board.psu_on()`
- `board.psu_off()`
- `board.is_psu_on()`

### `PSC`

Connects or disconnects the PSU output toward the actuator side, or reads the tracked connection state.

Requests:

```text
PSC
PSC ON
PSC OFF
PSC 1
PSC 0
```

Responses:

```text
OK:ON
OK:OFF
OK:PSC_ON
OK:PSC_OFF
```

`PSC` with no parameter returns `OK:ON` or `OK:OFF`. `PSC ON`, `PSC 1`, `PSC OFF`, and `PSC 0` return acknowledgements.

Errors:

```text
ER:PSC_PARAM_COUNT
ER:PSC_PSU_OFF
ER:PSC_PARAM_VALUE
```

Rules:

- `PSC ON` is rejected when the PSU is off.
- `PSC OFF` is accepted even when the PSU is off.
- Normal actuator writes require PSU on and PSU connected.

SDK helpers:

- `board.connect_power()`
- `board.connect_power(True)`
- `board.connect_power(False)`
- `board.psc_on()`
- `board.psc_off()`
- `board.is_power_connected()`

### `VLT`

Reads the voltage coming out of the power supply.

Requests:

```text
VLT
VLT <measurement_ms>
```

Responses:

```text
OK:<volts>
```

Example:

```text
OK:218.45
```

Parameters:

- No parameter uses a short default measurement.
- One numeric parameter averages over that many milliseconds.
- `measurement_ms` must be at least `1`.

Errors:

```text
ER:VLT_PARAM_COUNT
ER:VLT_PARAM_VALUE
```

SDK helper:

- `board.voltage()`
- `board.voltage(measurement_ms=250)`

### `CUR`

Reads present current draw.

Request:

```text
CUR
```

Response:

```text
OK:<mA>
```

Example:

```text
OK:12.34
```

The current measurement window is fixed in firmware. `CUR` does not diagnose a specific actuator; use `DIA` for actuator diagnosis.

Errors:

```text
ER:CUR_PARAM_COUNT
```

SDK helper:

- `board.current()`

### `ACT`

Normal safe actuator control. This is the command that real applications should use.

Requests:

```text
ACT
ACT <actuator>
ACT <actuator> <value>
```

Examples:

```text
ACT
ACT 5
ACT 5 180
ACT 5 0
```

Responses:

All actuator forward values:

```text
OK:v0,v1,v2,...,v23
```

Single actuator forward value:

```text
OK:<actuator>,<value>
```

Successful set:

```text
OK:ACT
```

Parameter rules:

- `actuator` must be `0..23`.
- `value` must be `0..255`.

Safety and state-machine rules:

- Write requires PSU on.
- Write requires PSU connected.
- The actuator cannot be re-enabled while it is discharging.
- A positive value starts or continues forward activation.
- Setting value `0` disables the actuator.
- When an actuator goes from active to disabled, firmware increments runtime, writes runtime to EEPROM, and starts reverse discharge.
- If an actuator exceeds the configured maximum active time, firmware auto-disables it and starts discharge.
- Repeated nonzero updates do not reset the active timer. The continuous active interval starts when the actuator first becomes active and ends when it is disabled or forced into discharge.

Electrode behavior:

- The requested `value` is applied as the positive electrode command.
- The negative electrode command is held at `0` during normal forward operation.
- Reverse/negative drive is only produced by the firmware discharge process or by manual raw `OUT` commands.
- Discharge drives reverse at maximum for the shorter of the prior active time or configured discharge time.

Errors:

```text
ER:ACT_PARAM_COUNT
ER:ACT_ACTUATOR
ER:ACT_VALUE
ER:ACT_PSU_OFF
ER:ACT_PSU_DISCONNECTED
ER:ACT_FAILED
```

SDK helpers:

- `board.get_actuators()`
- `board.get_actuator(actuator)`
- `board.set_actuator(actuator, value)`
- `board.all_actuators_off()`

### `OUT`

Manual direct electrode output for bench testing. This command intentionally bypasses normal actuator safety behavior.

Requests:

```text
OUT
OUT <actuator>
OUT <actuator> <positive> <negative>
```

Examples:

```text
OUT
OUT 5
OUT 5 180 0
OUT 5 0 255
```

Responses:

All actuator electrode pairs:

```text
OK:A0P>pos0,A0N>neg0,A1P>pos1,A1N>neg1,...,A23P>pos23,A23N>neg23
```

Single actuator electrode pair:

```text
OK:ACT><actuator>,POS><positive>,NEG><negative>
```

Successful write:

```text
OK:OUT
```

Parameter rules:

- `actuator` must be `0..23`.
- `positive` must be `0..255`.
- `negative` must be `0..255`.

Important behavior:

- `OUT` writes are blocked while the `SAFE` config is on.
- `SAFE` boots on every time.
- `OUT` does not enforce maximum active time.
- `OUT` does not start automatic discharge.
- `OUT` does not increment runtime.
- `OUT` cancels normal actuator timing/discharge state for that actuator.
- `OUT` does not require PSU on or connected. It is gated by the `SAFE` flag.

Errors:

```text
ER:OUT_PARAM_COUNT
ER:OUT_ACTUATOR
ER:OUT_POS_VALUE
ER:OUT_NEG_VALUE
ER:OUT_SAFETY_ON
ER:OUT_FAILED
```

SDK helpers:

- `board.manual_outputs()`
- `board.get_manual_output(actuator)`
- `board.set_manual_output(actuator, positive, negative)`
- `board.manual_output(...)`
- `board.safety(False)` to allow manual writes
- `board.safety(True)` to re-enable safety

### `INI`

Initializes one actuator.

Request:

```text
INI <actuator>
```

Example:

```text
INI 5
```

Response:

```text
OK:INI
```

Behavior:

1. Requires PSU on.
2. Requires PSU connected.
3. Resets the selected actuator runtime total to `0`.
4. Runs 5 short cycles: forward max for `0.5 s`, then off/discharge/wait for `0.5 s`.
5. Runs 5 long cycles: forward max for `1 s`, then off/discharge/wait for `1 s`.

Errors:

```text
ER:INI_PARAM_COUNT
ER:INI_ACTUATOR
ER:INI_PSU_OFF
ER:INI_PSU_DISCONNECTED
ER:INI_FAILED
```

Notes:

- `INI` is blocking while it runs.
- `INI` uses normal safe actuator control.
- There is no separate per-actuator runtime reset command; initialization is the per-actuator reset path.

SDK helper:

- `board.initialize_actuator(actuator)`

### `DIA`

Diagnoses one actuator by measuring current at baseline, forward activation, and discharge.

Request:

```text
DIA <actuator>
```

Example:

```text
DIA 5
```

Response:

```text
OK:ACT><actuator>,BASE><mA>,FWD><mA>,DIS><mA>
```

Example:

```text
OK:ACT>5,BASE>1.23,FWD>14.56,DIS>7.89
```

Behavior:

1. Requires PSU on.
2. Requires PSU connected.
3. Turns all actuators off.
4. Waits for active discharge processes to finish.
5. Measures baseline current.
6. Activates the requested actuator forward at max for 1 second.
7. Measures forward current.
8. Deactivates the actuator, triggering discharge.
9. Measures current during discharge.
10. Waits for discharge completion.

Errors:

```text
ER:DIA_PARAM_COUNT
ER:DIA_ACTUATOR
ER:DIA_PSU_OFF
ER:DIA_PSU_DISCONNECTED
ER:DIA_FAILED
```

Notes:

- `DIA` is blocking while it runs.
- `DIA` uses normal safe actuator control.
- `DIA` affects runtime totals because it activates the actuator.

SDK helper:

- `board.diagnose_actuator(actuator)`

### `TIM`

Reads actuator total runtime in milliseconds.

Requests:

```text
TIM
TIM <actuator>
```

Responses:

All runtimes:

```text
OK:t0,t1,t2,...,t23
```

Single actuator runtime:

```text
OK:<actuator>,<runtime_ms>
```

Errors:

```text
ER:TIM_PARAM_COUNT
ER:TIM_ACTUATOR
```

Notes:

- Runtime totals are stored in EEPROM.
- When an actuator is currently active, the returned value includes the active interval even if it has not yet been committed to EEPROM.
- Runtime is committed when an actuator transitions from enabled to disabled.

SDK helper:

- `board.runtime()`
- `board.runtime(actuator)`

### `RST`

Resets all actuator runtime totals.

Request:

```text
RST
```

Response:

```text
OK:RST
```

Errors:

```text
ER:RST_PARAM_COUNT
```

Behavior:

- Resets all 24 runtime totals to `0`.
- Writes all reset values to EEPROM.
- Does not reset only one actuator. Use `INI <actuator>` when one actuator should be initialized and reset.

SDK helper:

- `board.reset_runtimes()`

### `RBT`

Reboots the Lansing board.

Request:

```text
RBT
```

Response:

```text
OK:RBT
```

After sending `OK:RBT`, the firmware flushes serial output, waits briefly, and performs a Teensy software reset. The USB serial port may disconnect and reconnect during the reboot.

Errors:

```text
ER:RBT_PARAM_COUNT
```

SDK helper:

- `board.reboot()`

### `CFG`

Reads or writes firmware configuration.

Requests:

```text
CFG MAX
CFG MAX <milliseconds>
CFG DIS
CFG DIS <milliseconds>
CFG SAFE
CFG SAFE ON
CFG SAFE OFF
CFG SAFE 1
CFG SAFE 0
CFG DEBUG
CFG DEBUG ON
CFG DEBUG OFF
CFG DEBUG 1
CFG DEBUG 0
```

Responses:

```text
OK:MAX><milliseconds>
OK:DIS><milliseconds>
OK:SAFE>ON
OK:SAFE>OFF
OK:DEBUG>ON
OK:DEBUG>OFF
OK:CFG_MAX
OK:CFG_DIS
OK:CFG_SAFE
OK:CFG_DEBUG
```

Keys:

| Key | Meaning | Persisted |
|---|---|---|
| `MAX` | Maximum continuous actuator active time in milliseconds | Yes |
| `DIS` | Maximum discharge time in milliseconds | Yes |
| `SAFE` | Manual `OUT` safety flag | No |
| `DEBUG` | Firmware debug-message output flag | No |

Rules:

- `MAX` and `DIS` require numeric values greater than or equal to `0`.
- `SAFE` accepts `ON`, `OFF`, `1`, or `0`.
- `SAFE` always boots as `ON`.
- `DEBUG` accepts `ON`, `OFF`, `1`, or `0`.
- `DEBUG` always boots as `OFF`.

Errors:

```text
ER:CFG_PARAM_COUNT
ER:CFG_KEY
ER:CFG_VALUE
```

SDK helpers:

- `board.config(key)`
- `board.config(key, value)`
- `board.max_active_time_ms()`
- `board.max_active_time_ms(value)`
- `board.discharge_time_ms()`
- `board.discharge_time_ms(value)`
- `board.safety()`
- `board.safety(enabled)`
- `board.firmware_debug()`
- `board.firmware_debug(enabled)`
- `board.read_config()`

### `STS`

Returns a detailed multi-line status snapshot.

Request:

```text
STS
```

Response:

```text
OK:PSU>ON,PSC>ON,VLT>218.45,CUR>12.34,CFG_MAX>5000,CFG_DIS>2000,SAFE>ON,DEBUG>OFF,STREAM>TEXT
OK:ACT_VALUES>0,0,180,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0
OK:OUT_VALUES>A0P>0,A0N>0,A1P>0,A1N>0,A2P>180,A2N>76,...
OK:ACT_STATES>0,0,1,0,...
OK:ACTIVE_MS>0,0,124,0,...
OK:TOTAL_MS>0,5300,1200,0,...
OK:DISCHARGE_MS_LEFT>0,0,0,0,...
```

Fields:

- `PSU`: tracked power supply state, `ON` or `OFF`.
- `PSC`: tracked PSU output connection state, `ON` or `OFF`.
- `VLT`: measured voltage.
- `CUR`: measured current.
- `CFG_MAX`: maximum active time.
- `CFG_DIS`: maximum discharge time.
- `SAFE`: manual output safety flag.
- `DEBUG`: firmware debug flag.
- `STREAM`: `TEXT` or `BIN`.
- `ACT_VALUES`: normal forward actuator values for actuators `0..23`.
- `OUT_VALUES`: positive and negative electrode output values for actuators `0..23`.
- `ACT_STATES`: actuator state enum values.
- `ACTIVE_MS`: current continuous active interval per actuator.
- `TOTAL_MS`: persisted total runtime plus any current active interval.
- `DISCHARGE_MS_LEFT`: remaining discharge time per actuator.

Actuator state values:

| Value | Meaning |
|---:|---|
| `0` | Idle |
| `1` | Forward active |
| `2` | Discharging |

Errors:

```text
ER:STS_PARAM_COUNT
```

SDK helper:

- `board.status()`

### `STR`

Switches from text command mode to binary stream mode.

Request:

```text
STR
```

Response:

```text
OK:STR
```

Errors:

```text
ER:STR_PARAM_COUNT
```

After `OK:STR`, the firmware expects binary packets instead of newline-terminated text commands.

SDK helpers:

- `board.enter_stream_mode()`
- `board.stream_actuator(actuator, value)`
- `board.stream_values({actuator: value})`
- `board.stream_sine(...)`
- `board.exit_stream_mode()`

### Binary Stream Mode

Binary stream mode is intended for high-speed updates where text parsing overhead is too expensive.

Each packet is 2 bytes:

| Byte | Meaning |
|---:|---|
| `0` | Actuator number |
| `1` | Actuator value |

Actuator byte:

- `0..23`: actuator number
- `255`: exit binary stream mode and return to text mode

Value byte:

- `0..255`: normal forward actuator value

Example packets:

```text
[5, 180]
[6, 0]
[7, 255]
```

Exit stream mode:

```text
[255, 0]
```

Important behavior:

- Binary stream mode uses the normal safe actuator path, so it streams forward-only values.
- Runtime tracking still applies.
- Maximum active time enforcement still applies.
- Automatic discharge still applies.
- Discharge lockout still applies.
- If a stream keeps an actuator active longer than `CFG MAX`, firmware forces that actuator into discharge even if nonzero stream packets continue arriving.
- Discharge starts after the actuator receives value `0`. A host stream should avoid sending `0` during a continuous waveform unless it intentionally wants to stop and discharge. Send a final zero before exiting stream mode.
- Binary stream does not intentionally drive the reverse electrode high during normal waveform samples.
- Binary stream writes only apply when PSU is on and PSU output is connected.
- Invalid or unsafe stream packets are silently ignored to preserve speed. This includes packets for an actuator that is currently discharging.
- If the host keeps streaming after discharge completes, later valid nonzero packets can start a new active interval.
- Exiting stream mode does not print a response.

### Recommended Host Sequences

Startup:

```text
VER
PSU ON
PSC ON
STS
```

Normal single-actuator pulse:

```text
ACT 5 180
ACT 5 0
```

Shutdown:

```text
ACT 5 0
PSC OFF
PSU OFF
```

Binary streaming:

```text
PSU ON
PSC ON
STR
```

Then write binary packets until done and exit with `[255, 0]`.

### Error Handling

Firmware errors returned as `ER:` responses raise `FirmwareError`.

```python
from fluid_reality import FirmwareError, Lansing

try:
    with Lansing("COM5") as board:
        board.set_actuator(0, 180)
except FirmwareError as error:
    print(error.code)
    if error.info is not None:
        print(error.info.meaning)
        print(error.info.common_cause)
        print(error.info.recovery)
```

Every known Lansing `ER:` code is enriched with meaning, common cause, and recovery guidance.

| Code | Meaning | Common cause | Recovery |
|---|---|---|---|
| `BAD_COMMAND` | A received text line could not be parsed as a valid command. | Command name was shorter than 3 characters, longer than 3 characters, malformed, or otherwise failed parser validation. | Send a newline-terminated 3-letter text command. Check that fields are separated by spaces, tabs, or commas. |
| `LINE_TOO_LONG` | The incoming text line exceeded the serial line buffer. | The command line was longer than the firmware serial buffer. | Shorten the command. For high-speed actuator updates, use binary stream mode. |
| `UNKNOWN_COMMAND` | The command parsed correctly, but no handler exists for that command. | Typo, unsupported command, or old host software using a removed command name. | Compare the command against the Lansing command list. All command names are exactly 3 characters. |
| `VER_PARAM_COUNT` | Version query received parameters, but it expects none. | Host sent extra fields after the version command. | Send the version command by itself. |
| `STR_PARAM_COUNT` | Binary stream mode request received parameters, but it expects none. | Host sent extra fields after the stream command. | Send the stream command by itself, then switch the host to binary packet writes after `OK:STR`. |
| `RBT_PARAM_COUNT` | Reboot command received parameters, but it expects none. | Host sent extra fields after the reboot command. | Send the reboot command by itself. |
| `PSU_PARAM_COUNT` | PSU on/off command received the wrong number of parameters. | More than one parameter was sent. | Send no parameter to read state, or one parameter: `ON`, `OFF`, `1`, or `0`. |
| `PSU_PARAM_VALUE` | PSU command parameter was not recognized. | Parameter was not `ON`, `OFF`, `1`, or `0`. | Use one of the accepted values. |
| `PSC_PARAM_COUNT` | PSU connection command received the wrong number of parameters. | More than one parameter was sent. | Send no parameter to read state, or one parameter: `ON`, `OFF`, `1`, or `0`. |
| `PSC_PSU_OFF` | The host tried to connect PSU output while the PSU was off. | PSU output cannot be connected unless the PSU is already on. | Turn the PSU on first, then connect the PSU output. |
| `PSC_PARAM_VALUE` | PSU connection parameter was not recognized. | Parameter was not `ON`, `OFF`, `1`, or `0`. | Use one of the accepted values. |
| `CUR_PARAM_COUNT` | Current read command received parameters, but it expects none. | Host may be using an older protocol where current diagnosis was part of current read. | Send the current-read command by itself. Use actuator diagnosis for actuator-specific current testing. |
| `VLT_PARAM_COUNT` | Voltage read command received too many parameters. | More than one measurement-time parameter was sent. | Send no parameter for a quick read, or one numeric measurement time in milliseconds. |
| `VLT_PARAM_VALUE` | Voltage measurement-time parameter was invalid. | Parameter was non-numeric or less than `1`. | Send a positive integer measurement time in milliseconds. |
| `ACT_PARAM_COUNT` | Normal actuator command received too many parameters. | More than two parameters were sent. | Send no parameters to read all values, one actuator number to read one value, or actuator number plus output value to set. |
| `ACT_ACTUATOR` | Actuator parameter was invalid. | Actuator was non-numeric, negative, or outside `0..23`. | Use actuator numbers `0` through `23`. |
| `ACT_VALUE` | Actuator output value was invalid. | Value was non-numeric, negative, or greater than `255`. | Use values `0` through `255`. |
| `ACT_PSU_OFF` | Host tried to set an actuator while the PSU was off. | Normal actuator writes require the PSU to be on. | Turn the PSU on first. |
| `ACT_PSU_DISCONNECTED` | Host tried to set an actuator while PSU output was disconnected. | Normal actuator writes require PSU output to be connected. | Connect PSU output first. |
| `ACT_FAILED` | The actuator set request was valid, but firmware refused to apply it. | Most commonly, the actuator is currently discharging and cannot be re-enabled yet. | Wait for discharge to finish, or check status for discharge time remaining. |
| `OUT_PARAM_COUNT` | Manual output command received an unsupported number of parameters. | Two parameters or more than three parameters were sent. | Send no parameters to read all electrode pairs, one actuator number to read one pair, or actuator number plus positive and negative values to write. |
| `OUT_ACTUATOR` | Manual output actuator parameter was invalid. | Actuator was non-numeric, negative, or outside `0..23`. | Use actuator numbers `0` through `23`. |
| `OUT_POS_VALUE` | Manual positive electrode value was invalid. | Positive value was non-numeric, negative, or greater than `255`. | Use values `0` through `255`. |
| `OUT_NEG_VALUE` | Manual negative electrode value was invalid. | Negative value was non-numeric, negative, or greater than `255`. | Use values `0` through `255`. |
| `OUT_SAFETY_ON` | Manual output write was blocked by the safety flag. | Safety defaults on at boot and blocks direct electrode writes. | Disable safety through configuration only during controlled bench testing. |
| `OUT_FAILED` | Manual output write was valid but the raw electrode write failed. | Invalid internal state or failed lower-level validation. | Check actuator number and firmware status. Reboot if state appears inconsistent. |
| `INI_PARAM_COUNT` | Initialization command received the wrong number of parameters. | No actuator number or more than one parameter was sent. | Send exactly one actuator number. |
| `INI_ACTUATOR` | Initialization actuator parameter was invalid. | Actuator was non-numeric, negative, or outside `0..23`. | Use actuator numbers `0` through `23`. |
| `INI_PSU_OFF` | Host tried to initialize an actuator while PSU was off. | Initialization drives the actuator and requires power. | Turn the PSU on first. |
| `INI_PSU_DISCONNECTED` | Host tried to initialize an actuator while PSU output was disconnected. | Initialization drives the actuator and requires connected output. | Connect PSU output first. |
| `INI_FAILED` | Initialization started but failed during one of its drive/off cycles. | A lower-level actuator write failed, or firmware timed out while waiting for discharge between pulses. | Check status and debug output, wait for discharge to finish, then retry. |
| `DIA_PARAM_COUNT` | Diagnosis command received the wrong number of parameters. | No actuator number or more than one parameter was sent. | Send exactly one actuator number. |
| `DIA_ACTUATOR` | Diagnosis actuator parameter was invalid. | Actuator was non-numeric, negative, or outside `0..23`. | Use actuator numbers `0` through `23`. |
| `DIA_PSU_OFF` | Host tried to diagnose an actuator while PSU was off. | Diagnosis drives the actuator and requires power. | Turn the PSU on first. |
| `DIA_PSU_DISCONNECTED` | Host tried to diagnose an actuator while PSU output was disconnected. | Diagnosis drives the actuator and requires connected output. | Connect PSU output first. |
| `DIA_FAILED` | Diagnosis could not complete. | Firmware could not bring actuators to idle, activate the actuator, or start discharge measurement. | Check status for active/discharging actuators, wait for discharge completion, then retry. |
| `TIM_PARAM_COUNT` | Runtime query received too many parameters. | More than one parameter was sent. | Send no parameters for all runtimes, or one actuator number for one runtime. |
| `TIM_ACTUATOR` | Runtime actuator parameter was invalid. | Actuator was non-numeric, negative, or outside `0..23`. | Use actuator numbers `0` through `23`. |
| `RST_PARAM_COUNT` | Runtime reset command received parameters, but it expects none. | Host attempted a per-actuator reset or sent extra fields. | Send reset with no parameters. Individual runtime reset is done through actuator initialization. |
| `CFG_PARAM_COUNT` | Configuration command received the wrong number of parameters. | No key, or more than two fields, were sent. | Send one key to read, or key plus value to write. |
| `CFG_KEY` | Configuration key was not recognized. | Key was not one of the supported configuration keys. | Use `MAX`, `DIS`, `SAFE`, or `DEBUG`. |
| `CFG_VALUE` | Configuration value was invalid. | Numeric config received a non-numeric or negative value, or a boolean config received a bad value. | Use a non-negative integer for time values. Use `ON`, `OFF`, `1`, or `0` for boolean values. |
| `STS_PARAM_COUNT` | Status command received parameters, but it expects none. | Host sent extra fields. | Send status with no parameters. |

## Examples

Example scripts live in [examples](examples):

- [01_basic_actuator_current.py](examples/01_basic_actuator_current.py): turn on PSU, measure voltage, connect PSU output, activate one actuator, then measure current.
- [02_initialize_and_diagnose.py](examples/02_initialize_and_diagnose.py): turn on PSU, connect PSU output, initialize one actuator, then diagnose it.
- [03_stream_sine.py](examples/03_stream_sine.py): enter binary stream mode, stream a sine wave between `1` and `255`, then send final `0` for discharge and print achieved refresh rate. Add `--print-values` to print every streamed actuator value. Use `--minimum` and `--maximum` to change the waveform range.
- [04_debug_logging.py](examples/04_debug_logging.py): enable firmware debug output and route `DBG:` lines through Python logging.
- [05_status_snapshot.py](examples/05_status_snapshot.py): print a full status dictionary.
- [06_manual_output_bench_test.py](examples/06_manual_output_bench_test.py): disable `OUT` safety temporarily and write raw electrode values.
- [07_error_handling.py](examples/07_error_handling.py): catch `FirmwareError` and print meaning, cause, and recovery.
- [08_actuator_pulse_until_key.py](examples/08_actuator_pulse_until_key.py): repeatedly run `ACT` for 1 second, turn the actuator off, wait for discharge to finish, and stop when a key is pressed.
