# Fluid Reality SDK

Python SDK and desktop tools for Fluid Reality hardware.

The package published on PyPI is `fluid-reality`; the Python import package is
`fluid_reality`.

## Getting Started

Use Python 3.10 or newer.

To run the Lansing Dashboard, clone the SDK repository and enter the dashboard
application folder.

macOS or Linux:

```bash
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk/apps/lansing_dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ../..
python -m pip install -r requirements.txt
python app.py
```

Windows PowerShell:

```powershell
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk\apps\lansing_dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ..\..
python -m pip install -r requirements.txt
python app.py
```

## Install

Install the SDK from PyPI:

```powershell
python -m pip install fluid-reality
```

For local development from this repository:

```powershell
python -m pip install -e .
```

The SDK requires Python 3.10 or newer.

## Quick Start

```python
from fluid_reality import ActuatorState, Lansing

with Lansing("COM5") as board:
    print(board.version())

    board.power_supply(True)
    board.connect_power(True)

    state = board.detect(0)
    if state is not ActuatorState.READY:
        raise RuntimeError(f"Actuator 0 is {state.value}")

    board.set_actuator(0, 180)
    print("actuator 0:", board.get_actuator(0))

    board.all_actuators_off()

    print("voltage:", board.voltage())
    print("current:", board.current())
```

## Lansing Board Wrapper

`Lansing` is the first board wrapper included in the SDK. It uses actuator
numbers `0` through `23`.

Create a board from a serial port:

```python
from fluid_reality import Lansing

board = Lansing("COM5")
```

Or use it as a context manager so the serial connection closes automatically:

```python
from fluid_reality import Lansing

with Lansing("COM5") as board:
    print(board.status())
```

Common operations:

- `power_supply(state=None)` reads or sets the power-supply state.
- `psu_on()` and `psu_off()` are convenience helpers.
- `connect_power(state=None)` reads or sets the output-connection state.
- `psc_on()` and `psc_off()` are convenience helpers.
- `voltage(measurement_ms=None)` reads voltage.
- `current()` reads current.
- `set_debug_out(destination)` configures SDK debug output. Use `None` to
  disable output, a `Path` for file logging, an open file handler, or a callback
  function such as `print` to process each debug line.
- `detect(actuator)` diagnoses one actuator and updates its SDK state.
- `actuator_state(actuator)` reads the cached SDK state.
- `initialize(actuator)` runs the staged SDK initialization sequence and returns
  the resulting actuator state.
- `set_actuator(actuator, value)` writes a normal actuator value only when the
  actuator state is `Ready`.
- `get_actuator(actuator)` reads one actuator value.
- `get_actuators()` reads all actuator values.
- `all_actuators_off()` commands all actuators off.
- `manual_output(...)`, `set_manual_output(...)`, and `get_manual_output(...)`
  provide direct electrode-level bench-control helpers.
- `diagnose_actuator(actuator)` runs an actuator current diagnosis.
- `runtime(actuator=None)` reads one runtime total or all runtime totals.
- `reset_runtimes()` resets all runtime totals.
- `config(key, value=None)` reads or writes supported device settings.
- `max_active_time_ms(value=None)` reads or writes maximum active time.
- `discharge_time_ms(value=None)` reads or writes discharge time.
- `safety(enabled=None)` reads or writes manual-output safety.
- `status()` returns a detailed board-state dictionary.
- `stream_sine(...)` streams a sine waveform and returns the achieved refresh
  rate.

## Dashboard App

The repository includes a PySide6 dashboard for Lansing boards:

```text
apps/lansing_dashboard/app.py
```

Run it from a local checkout:

```bash
cd sdk/apps/lansing_dashboard
python -m pip install -e ../..
python -m pip install -r requirements.txt
python app.py
```

Dashboard capabilities:

- Connect and disconnect from a serial port.
- Turn the power supply on or off.
- Connect or disconnect the output path.
- View voltage, current, and timing settings.
- Browse actuators by group: `0-7`, `8-15`, and `16-23`. Most Lansing Development Kit setups use only one populated group with eight actuators, typically Group 0.
- Auto-detect the selected actuator group when power is ready.
- Select actuators by clicking cards.
- Diagnose selected actuators.
- Recover selected actuators with configurable voltage and duration.
- Initialize selected actuators with a staged progress display.
- Run a square wave on the selected actuator until stopped.
- Watch detailed board events in the log panel.

For the full customer-facing operating guide, see
[docs/lansing_dashboard_manual.md](docs/lansing_dashboard_manual.md).

See [apps/lansing_dashboard/README.md](apps/lansing_dashboard/README.md) for
dashboard-specific notes.

## Examples

Example scripts live in [examples](examples):

- [01_basic_actuator_current.py](examples/01_basic_actuator_current.py):
  turn on power, connect output, activate one actuator, and read current.
- [02_initialize_and_diagnose.py](examples/02_initialize_and_diagnose.py):
  initialize and diagnose one actuator.
- [03_stream_sine.py](examples/03_stream_sine.py):
  stream a sine wave to one actuator.
- [04_debug_logging.py](examples/04_debug_logging.py):
  collect board diagnostic output through Python logging.
- [05_status_snapshot.py](examples/05_status_snapshot.py):
  print a full board-state dictionary.
- [06_manual_output_bench_test.py](examples/06_manual_output_bench_test.py):
  run direct bench-control output.
- [07_error_handling.py](examples/07_error_handling.py):
  catch SDK exceptions and print recovery guidance.
- [08_actuator_pulse_until_key.py](examples/08_actuator_pulse_until_key.py):
  repeatedly pulse an actuator until a key is pressed.

Run an example from a local checkout:

```powershell
python examples\05_status_snapshot.py COM5
```

## Errors

SDK exceptions are exported from `fluid_reality` and include structured details
for device, transport, and parsing failures.

## Development

Install development dependencies and run tests:

```powershell
python -m pip install -e .
python -m pip install pytest
python -m pytest
```

Useful validation commands:

```powershell
python -m compileall -q src examples tests apps
python -m pytest
```

## Package Metadata

- PyPI package: `fluid-reality`
- Import package: `fluid_reality`
- Current version: `0.1.0`
- License: MIT
