# Fluid Reality Python SDK API Reference

Customer development reference for the `fluid-reality` Python package.

The PyPI package is named `fluid-reality`. The Python import package is named
`fluid_reality`.

This document focuses on the Lansing Development Kit API provided by
`fluid_reality.Lansing`.

## Index

- [Installation](#installation)
- [Finding The Serial Port](#finding-the-serial-port)
- [Minimal Touch Validation](#minimal-touch-validation)
- [Imports](#imports)
- [Lansing Class Constants](#lansing-class-constants)
- [Object Lifecycle](#object-lifecycle)
- [Data Types](#data-types)
- [Power And Telemetry](#power-and-telemetry)
- [Actuator State And Detection](#actuator-state-and-detection)
- [Initialization](#initialization)
- [Normal Actuator Output](#normal-actuator-output)
- [Runtime Counters](#runtime-counters)
- [Configuration](#configuration)
- [Debug Output](#debug-output)
- [Manual Output And Advanced Bench Control](#manual-output-and-advanced-bench-control)
- [Streaming](#streaming)
- [Low-Level Protocol Utilities](#low-level-protocol-utilities)
- [Response Objects](#response-objects)
- [Errors](#errors)
- [Recommended Customer Workflows](#recommended-customer-workflows)
- [Notes On Safety And Timing](#notes-on-safety-and-timing)

## Installation

Use Python 3.10 or newer.

```bash
python -m pip install --upgrade pip
python -m pip install fluid-reality
```

Verify the import:

```python
from fluid_reality import Lansing

print(Lansing.actuator_count)
```

## Finding The Serial Port

Connect the Lansing board over USB, then list the serial ports visible to
Python:

```bash
python -m serial.tools.list_ports
```

Use the device name shown by that command when creating `Lansing(...)`.

Typical examples:

- Windows: `COM4`, `COM16`
- macOS: `/dev/cu.usbmodem...`
- Linux: `/dev/ttyACM...` or `/dev/ttyUSB...`

## Minimal Touch Validation

This is the smallest recommended end-to-end flow for a connected actuator:

```python
import time

from fluid_reality import ActuatorState, Lansing

port = "<serial-port>"
actuator = 0

with Lansing(port) as board:
    board.power_supply(True)
    print(f"Voltage: {board.voltage():.2f} V")

    board.connect_power(True)
    print(f"Current: {board.current():.2f} mA")

    state = board.detect(actuator)
    if state is ActuatorState.ERROR:
        state = board.initialize(actuator)

    if state is not ActuatorState.READY:
        raise RuntimeError(f"Actuator {actuator} is {state.value}")

    input(f"Touch actuator {actuator}, then press Enter.")

    board.set_actuator(actuator, 255)
    time.sleep(0.250)
    board.set_actuator(actuator, 0)
    time.sleep(0.250)
```

## Imports

The primary exports are available from `fluid_reality`.

```python
from fluid_reality import (
    ActuatorDetection,
    ActuatorState,
    Diagnosis,
    ErrorInfo,
    FirmwareError,
    FluidRealityError,
    Lansing,
    LansingConfig,
    LansingVersion,
    ManualOutput,
    ProtocolError,
    TransportError,
)
```

## Lansing Class Constants

These constants define the Lansing SDK model:

| Constant | Value | Meaning |
| --- | ---: | --- |
| `Lansing.actuator_count` | `24` | Total supported actuator indices: `0` through `23`. |
| `Lansing.default_timeout_s` | `45.0` | Default serial read timeout. Long enough for diagnostics and initialization. |
| `Lansing.min_output` | `0` | Minimum normal or manual output value. |
| `Lansing.max_output` | `255` | Maximum normal or manual output value. |
| `Lansing.not_connected_delta_ma` | `0.1` | Detection delta below this is `Not connected`. |
| `Lansing.error_delta_ma` | `3.0` | Detection delta above this is `Error`. |
| `Lansing.initialization_stages_v` | `(25.0, 50.0, 100.0, 200.0)` | Initialization voltage stages. |
| `Lansing.initialization_stage_duration_s` | `30.0` | Seconds per initialization stage. |
| `Lansing.initialization_phase_interval_s` | `0.5` | Alternation interval during initialization. |

Example:

```python
from fluid_reality import Lansing

print(Lansing.actuator_count)      # 24
print(Lansing.max_output)          # 255
print(Lansing.default_timeout_s)   # 45.0
```

## Object Lifecycle

### `Lansing(port=None, *, baudrate=250000, timeout=45.0, transport=None, debug_callback=None, debug_logger=None, log_debug_messages=False, **serial_kwargs)`

Create a Lansing board wrapper.

Pass either:

- `port`: serial-port device name, or
- `transport`: custom transport object for tests or advanced integrations.

Use a context manager whenever possible so the serial connection closes
cleanly.

```python
from fluid_reality import Lansing

with Lansing("<serial-port>") as board:
    print(board.firmware_version())
```

Explicit close:

```python
from fluid_reality import Lansing

board = Lansing("<serial-port>")
try:
    print(board.status())
finally:
    board.close()
```

Custom timeout:

```python
from fluid_reality import Lansing

board = Lansing("<serial-port>", timeout=60.0)
board.close()
```

Custom serial kwargs are passed to `pyserial.Serial`.

```python
from fluid_reality import Lansing

board = Lansing("<serial-port>", write_timeout=2.0)
board.close()
```

## Data Types

### `ActuatorState`

Enum of SDK actuator states.

Values:

| State | Value | Meaning |
| --- | --- | --- |
| `ActuatorState.UNKNOWN` | `"Unknown"` | Default state when the board object is created. |
| `ActuatorState.READY` | `"Ready"` | Actuator has been detected and can be driven normally. |
| `ActuatorState.ERROR` | `"Error"` | Current delta is too high; initialize before normal use. |
| `ActuatorState.NOT_CONNECTED` | `"Not connected"` | Detection did not measure meaningful current delta. |

Example:

```python
from fluid_reality import ActuatorState

if state is ActuatorState.READY:
    print("ready to drive")
```

### `Diagnosis`

Current measurements returned by `diagnose_actuator()`.

Fields:

- `actuator: int`
- `baseline_ma: float`
- `forward_ma: float`
- `discharge_ma: float`

Example:

```python
diagnosis = board.diagnose_actuator(0)
print(diagnosis.baseline_ma)
print(diagnosis.forward_ma)
print(diagnosis.discharge_ma)
```

### `ActuatorDetection`

Classified diagnosis result returned by `detect_actuator()`,
`initialize_actuator_stateful()`, and `classify_diagnosis()`.

Fields:

- `actuator: int`
- `state: ActuatorState`
- `baseline_ma: float`
- `forward_ma: float`
- `discharge_ma: float`
- `delta_ma: float`

Example:

```python
detection = board.detect_actuator(0)
print(detection.state.value)
print(f"delta: {detection.delta_ma:.2f} mA")
```

### `ManualOutput`

Manual positive/negative electrode output returned by `get_manual_output()` or
`manual_output(actuator)` with no output values.

Fields:

- `actuator: int`
- `positive: int`
- `negative: int`

Example:

```python
output = board.get_manual_output(0)
print(output.positive, output.negative)
```

### `LansingVersion`

Typed firmware version returned by `firmware_version()`.

Fields:

- `firmware: str`
- `version: str`
- `protocol: str`

Example:

```python
version = board.firmware_version()
print(version.firmware, version.version, version.protocol)
```

### `LansingConfig`

Typed configuration returned by `read_config()`.

Fields:

- `max_active_ms: int`
- `discharge_ms: int`
- `safe: bool`
- `debug: bool`

Example:

```python
config = board.read_config()
print(config.max_active_ms)
print(config.discharge_ms)
print(config.safe)
print(config.debug)
```

## Power And Telemetry

### `power_supply(state=None) -> str`

Read or set the high-voltage power-supply state.

`state` can be `True`, `False`, `"ON"`, `"OFF"`, `"1"`, `"0"`, `1`, or `0`.

Examples:

```python
print(board.power_supply())      # read current state
print(board.power_supply(True))  # turn on
print(board.power_supply(False)) # turn off
```

### `psu_on() -> None`

Convenience wrapper for `power_supply(True)`.

```python
board.psu_on()
```

### `psu_off() -> None`

Convenience wrapper for `power_supply(False)`.

```python
board.psu_off()
```

### `is_psu_on() -> bool`

Return `True` when the power supply is on.

```python
if not board.is_psu_on():
    board.psu_on()
```

### `connect_power(state=None) -> str`

Read or set the output connection state.

The power supply can be on while the output path is still open. Use
`connect_power(True)` only when the actuator setup is ready.

```python
print(board.connect_power())      # read current output state
print(board.connect_power(True))  # close/connect output
print(board.connect_power(False)) # open/disconnect output
```

### `psc_on() -> None`

Convenience wrapper for `connect_power(True)`.

```python
board.psc_on()
```

### `psc_off() -> None`

Convenience wrapper for `connect_power(False)`.

```python
board.psc_off()
```

### `is_power_connected() -> bool`

Return `True` when the output connection is closed.

```python
if board.is_power_connected():
    print("output connected")
```

### `voltage(measurement_ms=None) -> float`

Read measured supply voltage in volts.

`measurement_ms` can request a firmware-side measurement interval. It must be
`>= 1` when provided.

```python
voltage = board.voltage()
print(f"{voltage:.2f} V")

averaged = board.voltage(measurement_ms=100)
print(f"{averaged:.2f} V")
```

### `current() -> float`

Read measured current in milliamps.

```python
current_ma = board.current()
print(f"{current_ma:.2f} mA")
```

### `status() -> dict[str, object]`

Read a multi-line status snapshot from the board.

Returned keys:

- `psu`: power-supply state string
- `psc`: output-connection state string
- `voltage`: float voltage in volts
- `current`: float current in milliamps
- `config`: dict with `max_active_ms`, `discharge_ms`, `safe`, `debug`
- `stream`: stream mode state
- `actuator_values`: tuple of 24 normal actuator values
- `manual_outputs`: dict mapping actuator to `(positive, negative)`
- `actuator_states`: tuple of 24 firmware actuator state numbers
- `active_ms`: tuple of 24 active-time counters
- `total_ms`: tuple of 24 total runtime counters
- `discharge_ms_left`: tuple of 24 remaining discharge timers

Example:

```python
status = board.status()
print(status["psu"], status["psc"])
print(status["voltage"], status["current"])
print(status["config"]["max_active_ms"])
print(status["discharge_ms_left"][0])
```

## Actuator State And Detection

Normal actuator drive is stateful. All SDK actuator states start as
`ActuatorState.UNKNOWN`. Before calling `set_actuator()`, detect the actuator
and make sure it is `Ready`.

### `actuator_state(actuator) -> ActuatorState`

Read the cached SDK state for one actuator.

```python
state = board.actuator_state(0)
print(state.value)
```

### `actuator_states -> tuple[ActuatorState, ...]`

Read all cached SDK actuator states.

```python
for actuator, state in enumerate(board.actuator_states):
    print(actuator, state.value)
```

### `detect(actuator) -> ActuatorState`

Detect one actuator and return only its classified state.

Detection turns the actuator's group off, runs diagnosis, calculates current
delta, updates the cached SDK state, and returns:

- `Ready`
- `Error`
- `Not connected`

```python
from fluid_reality import ActuatorState

state = board.detect(0)
if state is ActuatorState.READY:
    print("actuator 0 is ready")
elif state is ActuatorState.ERROR:
    print("actuator 0 needs initialization")
else:
    print(f"actuator 0 is {state.value}")
```

### `detect_actuator(actuator) -> ActuatorDetection`

Detect one actuator and return the full `ActuatorDetection`.

```python
detection = board.detect_actuator(0)
print(detection.state.value)
print(detection.baseline_ma)
print(detection.forward_ma)
print(detection.discharge_ma)
print(detection.delta_ma)
```

### `last_detection(actuator) -> ActuatorDetection | None`

Return the latest stored detection for one actuator, or `None` if that actuator
has not been detected in this `Lansing` object.

```python
latest = board.last_detection(0)
if latest is not None:
    print(latest.state.value, latest.delta_ma)
```

### `diagnose_actuator(actuator) -> Diagnosis`

Run firmware diagnosis for one actuator and return raw measurements. This does
not itself return an `ActuatorState`; use `classify_diagnosis()` if you need to
update SDK state from the result.

```python
diagnosis = board.diagnose_actuator(0)
print(f"baseline: {diagnosis.baseline_ma:.2f} mA")
print(f"forward: {diagnosis.forward_ma:.2f} mA")
print(f"discharge: {diagnosis.discharge_ma:.2f} mA")
```

### `classify_diagnosis(diagnosis) -> ActuatorDetection`

Classify a `Diagnosis`, update the cached actuator state, store the detection,
and return `ActuatorDetection`.

```python
diagnosis = board.diagnose_actuator(0)
detection = board.classify_diagnosis(diagnosis)
print(detection.state.value)
```

Classification thresholds:

- `delta_ma < 0.1`: `Not connected`
- `delta_ma > 3.0`: `Error`
- otherwise: `Ready`

## Initialization

Initialization is intended for actuators that detect as `Error`, especially
after storage, shipping, or long idle periods.

The stateful initialization sequence:

1. Requires the actuator to have been detected first.
2. Rejects `Unknown` and `Not connected`.
3. Reads supply voltage.
4. Temporarily disables manual-output safety.
5. Alternates positive/negative manual output at approximately 1 Hz through
   voltage stages `25 V`, `50 V`, `100 V`, and `200 V`.
6. Restores safety.
7. Runs diagnosis again.
8. Updates and returns the actuator state.

### `initialize(actuator, *, progress_callback=None) -> ActuatorState`

Run stateful initialization and return the resulting state.

```python
from fluid_reality import ActuatorState

state = board.detect(0)
if state is ActuatorState.ERROR:
    state = board.initialize(0)

if state is ActuatorState.READY:
    print("actuator recovered")
```

Progress callback example:

```python
def on_progress(progress: dict[str, float | int]) -> None:
    print(
        f"{progress['elapsed_s']:.0f}/{progress['total_s']:.0f}s "
        f"stage {progress['stage_index']}/{progress['stage_count']} "
        f"{progress['stage_voltage']:.0f} V"
    )

state = board.initialize(0, progress_callback=on_progress)
print(state.value)
```

### `initialize_actuator_stateful(actuator, *, progress_callback=None) -> ActuatorDetection`

Run the same stateful initialization as `initialize()`, but return the full
`ActuatorDetection`.

```python
detection = board.initialize_actuator_stateful(0)
print(detection.state.value)
print(detection.delta_ma)
```

### `initialize_actuator(actuator) -> None`

Send the firmware `INI` command directly.

This is a lower-level firmware command retained for compatibility. For customer
applications, prefer the stateful `initialize()` method because it implements
the dashboard recovery sequence and returns the final SDK state.

```python
board.initialize_actuator(0)
```

## Normal Actuator Output

Normal actuator output uses the firmware `ACT` path and enforces the SDK
`Ready` state. Values are integers from `0` through `255`.

Important discharge behavior:

- `set_actuator(actuator, value > 0)` drives the actuator forward.
- `set_actuator(actuator, 0)` commands off and triggers firmware-managed
  discharge.
- Discharge runs in the opposite direction for approximately the forward
  active time, up to the configured discharge limit.

### `set_actuator(actuator, value) -> None`

Set one actuator to a normal output value.

Requires cached SDK state `Ready`; call `detect()` first.

```python
import time

from fluid_reality import ActuatorState

state = board.detect(0)
if state is not ActuatorState.READY:
    raise RuntimeError(state.value)

board.set_actuator(0, 255)
time.sleep(0.250)
board.set_actuator(0, 0)
time.sleep(0.250)
```

### `get_actuator(actuator) -> int`

Read one normal actuator value.

```python
value = board.get_actuator(0)
print(value)
```

### `get_actuators() -> tuple[int, ...]`

Read all normal actuator values.

```python
values = board.get_actuators()
print(values[0])
print(len(values))  # 24
```

### `all_actuators_off() -> None`

Command all actuators off.

```python
board.all_actuators_off()
```

## Runtime Counters

### `runtime(actuator=None) -> int | tuple[int, ...]`

Read total runtime in milliseconds.

Pass an actuator number to read one actuator. Omit the argument to read all
actuators.

```python
runtime_0_ms = board.runtime(0)
print(runtime_0_ms)

all_runtime_ms = board.runtime()
print(all_runtime_ms[0])
```

### `reset_runtimes() -> None`

Reset all runtime totals.

```python
board.reset_runtimes()
```

## Configuration

### `config(key, value=None) -> str`

Read or write a raw configuration value.

Common keys:

- `MAX`: maximum active time in milliseconds
- `DIS`: maximum discharge time in milliseconds
- `SAFE`: manual-output safety, `ON` or `OFF`
- `DEBUG`: firmware debug output, `ON` or `OFF`

Examples:

```python
print(board.config("MAX"))
print(board.config("DIS"))
print(board.config("SAFE"))
print(board.config("DEBUG"))

board.config("DEBUG", "ON")
board.config("DEBUG", "OFF")
```

### `max_active_time_ms(value=None) -> int`

Read or set maximum active time in milliseconds.

```python
current_max = board.max_active_time_ms()
print(current_max)

board.max_active_time_ms(5000)
```

### `discharge_time_ms(value=None) -> int`

Read or set maximum discharge time in milliseconds.

```python
current_discharge = board.discharge_time_ms()
print(current_discharge)

board.discharge_time_ms(2000)
```

### `safety(enabled=None) -> bool`

Read or set manual-output safety.

Manual-output safety affects raw/manual `OUT` commands, not normal `ACT`
commands. Leave safety enabled unless you are intentionally performing advanced
bench control.

```python
print(board.safety())

board.safety(False)
try:
    board.set_manual_output(0, 20, 0)
finally:
    board.set_manual_output(0, 0, 0)
    board.safety(True)
```

### `read_config() -> LansingConfig`

Read typed configuration values.

```python
config = board.read_config()
print(config.max_active_ms)
print(config.discharge_ms)
print(config.safe)
print(config.debug)
```

## Debug Output

There are two debug paths:

1. SDK debug output, configured with `set_debug_out()`.
2. Firmware `DBG:` lines, enabled with `firmware_debug(True)`.

### `set_debug_out(destination) -> None`

Configure SDK debug output.

Supported destinations:

- `None`: disable debug output
- `pathlib.Path`: append debug lines to a file
- file-like object with `write`
- callback function accepting one formatted line, such as `print`

Strings are not accepted as file paths. Use `Path("file.log")`.

Examples:

```python
board.set_debug_out(print)
board.current()
board.set_debug_out(None)
```

```python
from pathlib import Path

board.set_debug_out(Path("lansing-debug.log"))
board.status()
board.set_debug_out(None)
```

```python
lines: list[str] = []
board.set_debug_out(lines.append)
board.voltage()
print(lines[-1])
```

### `firmware_debug(enabled=None) -> bool`

Read or set firmware debug output.

```python
print(board.firmware_debug())

board.firmware_debug(True)
board.current()
board.firmware_debug(False)
```

### `enable_firmware_debug() -> None`

Convenience wrapper for `firmware_debug(True)`.

```python
board.enable_firmware_debug()
```

### `disable_firmware_debug() -> None`

Convenience wrapper for `firmware_debug(False)`.

```python
board.disable_firmware_debug()
```

### `debug_lines -> tuple[str, ...]`

Property inherited from the base board wrapper. Returns collected firmware
`DBG:` lines that have been received while reading command responses.

```python
board.firmware_debug(True)
board.current()
for line in board.debug_lines:
    print(line)
```

### `flush_debug_lines() -> tuple[str, ...]`

Return collected firmware debug lines and clear the internal buffer.

```python
lines = board.flush_debug_lines()
print(lines)
```

### Constructor debug options

`debug_callback`, `debug_logger`, and `log_debug_messages` apply to firmware
`DBG:` lines encountered by the text protocol.

Callback example:

```python
def on_firmware_debug(line: str) -> None:
    print("firmware:", line)

with Lansing("<serial-port>", debug_callback=on_firmware_debug) as board:
    board.firmware_debug(True)
    board.status()
```

Logger example:

```python
import logging

logger = logging.getLogger("lansing.firmware")
logging.basicConfig(level=logging.DEBUG)

with Lansing("<serial-port>", debug_logger=logger, log_debug_messages=True) as board:
    board.firmware_debug(True)
    board.status()
```

## Manual Output And Advanced Bench Control

Manual output uses the firmware `OUT` path. It controls positive and negative
manual electrode outputs directly and is intended for bench/debug/recovery
operations. Normal applications should prefer `set_actuator()`.

Values are integers `0` through `255`.

### `manual_output(actuator, positive=None, negative=None) -> ManualOutput | None`

With no `positive` or `negative`, read one actuator's manual output.

With both values provided, write manual output and return `None`.

```python
output = board.manual_output(0)
print(output.positive, output.negative)

board.safety(False)
try:
    board.manual_output(0, 25, 0)
    board.manual_output(0, 0, 25)
    board.manual_output(0, 0, 0)
finally:
    board.safety(True)
```

### `set_manual_output(actuator, positive, negative) -> None`

Write manual output values.

```python
board.safety(False)
try:
    board.set_manual_output(0, 50, 0)
    board.set_manual_output(0, 0, 0)
finally:
    board.safety(True)
```

### `get_manual_output(actuator) -> ManualOutput`

Read one actuator's manual output.

```python
output = board.get_manual_output(0)
print(output.actuator, output.positive, output.negative)
```

### `manual_outputs() -> dict[int, tuple[int, int]]`

Read manual outputs for all actuators.

```python
outputs = board.manual_outputs()
positive, negative = outputs[0]
print(positive, negative)
```

## Streaming

Streaming writes raw bytes for high-rate output. It is an advanced mode. Normal
applications should use `set_actuator()` unless they need continuous generated
waveforms.

Always exit stream mode when finished.

### `enter_stream_mode() -> None`

Enter firmware binary stream mode.

```python
board.enter_stream_mode()
```

### `stream_actuator(actuator, value) -> None`

Write one stream update for one actuator.

```python
board.enter_stream_mode()
try:
    board.stream_actuator(0, 128)
    board.stream_actuator(0, 0)
finally:
    board.exit_stream_mode()
```

### `stream_values(values) -> None`

Write multiple stream updates from a dict mapping actuator numbers to values.

```python
board.enter_stream_mode()
try:
    board.stream_values({0: 120, 1: 180})
    board.stream_values({0: 0, 1: 0})
finally:
    board.exit_stream_mode()
```

### `stream_sine(actuator, *, duration_s, frequency_hz, update_hz=100.0, minimum=1, maximum=255, discharge_on_finish=True, value_callback=None) -> float`

Generate and stream a sine waveform for one actuator. Returns the achieved
update rate in hertz.

```python
rate = board.stream_sine(
    0,
    duration_s=5.0,
    frequency_hz=1.0,
    update_hz=100.0,
    minimum=1,
    maximum=255,
)
print(f"achieved {rate:.1f} Hz")
```

With value callback:

```python
def on_value(actuator: int, value: int, elapsed_s: float) -> None:
    print(actuator, value, elapsed_s)

rate = board.stream_sine(
    0,
    duration_s=2.0,
    frequency_hz=0.5,
    value_callback=on_value,
)
```

### `exit_stream_mode() -> None`

Exit stream mode.

```python
board.exit_stream_mode()
```

### `force_text_mode() -> tuple[str, ...]`

Try to recover a clean text-command boundary after stream mode or an
interrupted stream run. Returns drained lines, if any.

```python
drained = board.force_text_mode()
print(drained)
print(board.status())
```

## Low-Level Protocol Utilities

These APIs are useful for diagnostics, test tools, and advanced integrations.
Most customer applications should prefer the typed methods above.

### `raw_command(command, *params, ok_lines=1) -> list[Response]`

Send a raw three-letter firmware command and return parsed protocol responses.

```python
responses = board.raw_command("VER")
print(responses[0].fields)
```

Multiple expected OK lines:

```python
responses = board.raw_command("STS", ok_lines=7)
print(len(responses))
```

### `drain_input() -> tuple[str, ...]`

Drain any already-available input lines if supported by the transport.

```python
for line in board.drain_input():
    print(line)
```

### `reset_input_buffer() -> None`

Reset the serial input buffer if supported by the transport.

```python
board.reset_input_buffer()
```

### `reboot() -> None`

Command the board to reboot.

```python
board.reboot()
```

After rebooting, close and reopen the `Lansing` object before continuing.

## Response Objects

`raw_command()` returns `Response` objects from `fluid_reality.protocol`.

Fields:

- `status: str`
- `raw: str`
- `payload: str`
- `values: tuple[str, ...]`
- `fields: dict[str, str]`

Properties:

- `ok: bool`
- `error_code: str` for error responses

Example:

```python
response = board.raw_command("VER")[0]
print(response.ok)
print(response.raw)
print(response.payload)
print(response.fields)
```

## Errors

All SDK errors inherit from `FluidRealityError`.

### `TransportError`

Raised when the serial transport cannot open, read, or write.

```python
from fluid_reality import TransportError, Lansing

try:
    board = Lansing("<serial-port>")
except TransportError as exc:
    print(f"transport failed: {exc}")
```

### `ProtocolError`

Raised when a device response does not match the expected protocol.

```python
from fluid_reality import ProtocolError

try:
    status = board.status()
except ProtocolError as exc:
    print(f"protocol error: {exc}")
```

### `FirmwareError`

Raised when firmware returns an `ER:` response.

Attributes:

- `code: str`
- `raw: str`
- `fields: dict[str, str]`
- `info: ErrorInfo | None`

Example:

```python
from fluid_reality import FirmwareError

try:
    board.set_actuator(0, 255)
except FirmwareError as exc:
    print(exc.code)
    print(exc.raw)
    if exc.info is not None:
        print(exc.info.meaning)
        print(exc.info.common_cause)
        print(exc.info.recovery)
```

### `ErrorInfo`

Human-readable firmware error metadata.

Fields:

- `meaning: str`
- `common_cause: str`
- `recovery: str`

Example:

```python
try:
    board.raw_command("BAD")
except FirmwareError as exc:
    info = exc.info
    if info is not None:
        print(info.meaning)
        print(info.recovery)
```

## Recommended Customer Workflows

### Startup

```python
from fluid_reality import Lansing

with Lansing("<serial-port>") as board:
    board.force_text_mode()
    print(board.firmware_version())
    board.power_supply(True)
    print(board.voltage())
    board.connect_power(True)
    print(board.current())
```

### Detect And Drive One Actuator

```python
import time

from fluid_reality import ActuatorState, Lansing

with Lansing("<serial-port>") as board:
    board.power_supply(True)
    board.connect_power(True)

    state = board.detect(0)
    if state is ActuatorState.ERROR:
        state = board.initialize(0)

    if state is not ActuatorState.READY:
        raise RuntimeError(state.value)

    board.set_actuator(0, 255)
    time.sleep(0.250)
    board.set_actuator(0, 0)
    time.sleep(0.250)
```

### Detect A Standard Eight-Actuator Kit

```python
from fluid_reality import Lansing

with Lansing("<serial-port>") as board:
    board.power_supply(True)
    board.connect_power(True)

    for actuator in range(8):
        detection = board.detect_actuator(actuator)
        print(actuator, detection.state.value, detection.delta_ma)
```

### Shut Down Safely

```python
board.all_actuators_off()
board.connect_power(False)
board.power_supply(False)
```

## Notes On Safety And Timing

- Use `detect()` before normal output.
- Treat `Error` as requiring initialization before normal use.
- Treat `Not connected` as a wiring or intentionally-empty-port condition.
- Normal output values are `0..255`.
- Actuators discharge after being commanded off.
- A longer forward activation creates a longer discharge, up to the configured
  discharge limit.
- The default serial timeout is 45 seconds because detection and initialization
  can take longer than ordinary status reads.
