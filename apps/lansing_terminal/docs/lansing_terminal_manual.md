# Lansing Terminal Operator and Command Reference

`lansing_terminal` is the command-line operator interface for the Fluid Reality
Lansing Development Kit. It provides interactive board control for laboratory
use and a non-interactive mode for scripts, test fixtures, and automated setup
benches.

The terminal can:

- discover physical and virtual ports and connect to a Lansing controller;
- control the high-voltage power supply and its connection to the actuator path;
- read voltage, current, configuration, status, and runtime counters;
- detect, diagnose, initialize, fast-initialize, and recover actuators;
- drive normal actuator output through firmware safety controls;
- perform advanced raw positive/negative manual-output tests;
- run a continuous square-wave test in a background thread;
- collect SDK and terminal logs; and
- emit either human-readable text or newline-delimited JSON (NDJSON).

This standalone manual is both an operator guide and a complete command
reference. It includes installation, safety, interactive and automated
operation, every terminal command, JSON schemas, target-current workflows,
exit behavior, and troubleshooting without requiring the shorter application
README.

## Contents

- [Safety](#safety)
- [System requirements](#system-requirements)
- [Installation](#installation)
- [Starting the terminal](#starting-the-terminal)
- [Command-line options](#command-line-options)
- [Operating modes](#operating-modes)
- [Board and actuator model](#board-and-actuator-model)
- [Recommended first-use workflow](#recommended-first-use-workflow)
- [Complete command reference](#complete-command-reference)
- [JSON output](#json-output)
- [Exit status and error handling](#exit-status-and-error-handling)
- [Automation examples](#automation-examples)
- [Troubleshooting](#troubleshooting)

## Safety

The Lansing Development Kit contains a high-voltage power system. Treat the
power-supply state and the PSU connection as separate safety
controls.

- Assemble and position actuators before turning the PSU connection on.
- Do not handle actuator wiring while the PSU connection is on.
- Turn the PSU connection off and then turn the power supply off before
  changing the physical setup.
- Detect an actuator before normal operation.
- Use `set` and `square` only when the actuator state is `Ready`.
- If detection reports `Error`, use `init` or `fast_init` before normal
  operation.
- Use `recover` only as an advanced conditioning procedure when ordinary
  initialization or Fast Init is insufficient or a controlled bench procedure
  requires it.
- `manual set` is raw electrode control. It bypasses the normal actuator state
  workflow and is intended only for qualified bench/debug use.
- Do not leave firmware manual-output safety disabled.

Normal output uses the SDK `ACT` path. The firmware continues to enforce power
state, PSU-connection state, maximum active time, runtime tracking, automatic
discharge, and discharge lockout. Setting a normal actuator output to `0` does
not necessarily make the actuator electrically idle immediately: it requests
firmware-managed reverse discharge.

## System requirements

- Python 3.10 or newer
- A Lansing Development Kit connected over USB
- Permission to access the serial device
- The `fluid-reality` Python package
- `pyserial`, installed as a dependency of `fluid-reality`

The terminal itself does not require a graphical display. It is suitable for
Windows PowerShell, macOS or Linux shells, SSH sessions, and headless lab
machines.

## Installation

Install the checked-out SDK in editable mode before the app requirements so
the terminal uses the source and APIs from the same repository revision.

### Windows PowerShell

```powershell
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk\apps\lansing_terminal
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ..\..
python -m pip install -r requirements.txt
python lansing_terminal.py
```

### macOS or Linux

```bash
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk/apps/lansing_terminal
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ../..
python -m pip install -r requirements.txt
python lansing_terminal.py
```

To verify that the command-line options are available without opening a board:

```bash
python lansing_terminal.py --help
```

## Starting the terminal

The general invocation is:

```text
python lansing_terminal.py [--port PORT] [--verbose] [-j|--json] [-c COMMANDS]
```

With no options, the application starts an interactive disconnected session:

```text
Fluid Reality Lansing terminal. Type 'help' for commands.
lansing(disconnected)>
```

After a successful connection, the prompt changes to:

```text
lansing>
```

### Start disconnected

```bash
python lansing_terminal.py
```

### Connect during startup

```powershell
python lansing_terminal.py --port COM6
```

```bash
python lansing_terminal.py --port /dev/ttyACM0
```

When `--port` succeeds, the terminal validates firmware communication and
immediately prints a complete `status` snapshot.

### Run commands and exit

Use `-c` or `--command` for a semicolon-separated command sequence:

```powershell
python lansing_terminal.py --port COM6 -c "status; psu on; voltage; psuc on; current"
```

The sequence stops on its first error. The terminal stops any square wave,
closes the board, and returns exit status `1`. A successful sequence returns
`0`.

## Command-line options

### `--port PORT`

Connect to `PORT` before starting the interactive loop or executing `-c`.

Typical device names:

| Platform | Example |
| --- | --- |
| Windows | `COM6` |
| macOS | `/dev/cu.usbmodem1101` |
| Linux | `/dev/ttyACM0` or `/dev/ttyUSB0` |

If the port is missing, busy, inaccessible, or not a usable Lansing device, the
failure is reported as a terminal error. An interactive `connect` failure
returns to the disconnected prompt. A startup `--port` failure exits with
status `1`.

### `--verbose`

Print SDK debug records as they are generated. Debug records are also retained
in the terminal log. In JSON mode, verbose records use the `debug_message`
event schema.

This option is distinct from firmware debug. Use `debug on` to show SDK debug
output and `config set DEBUG ON` when firmware-generated `DBG:` messages are
needed.

### `-j`, `--json`

Emit newline-delimited JSON instead of human-readable text. Every output line
is an independent JSON object. This format is intended for pipes, log
collectors, subprocess integrations, and automated test equipment.

In interactive JSON mode, the banner and prompt are suppressed so stdout
remains machine-readable.

```powershell
python lansing_terminal.py -j --port COM6 -c "diagnose 0"
```

### `-c COMMANDS`, `--command COMMANDS`

Execute one or more terminal commands and exit. Separate commands with
semicolons inside one shell-quoted argument.

```bash
python lansing_terminal.py -c "ports"
python lansing_terminal.py --port /dev/ttyACM0 -c "psu on; psuc on; detect; states group 0"
```

In text mode, the terminal echoes each command with its current prompt. In JSON
mode, it emits a `command` object before each command result.

## Operating modes

### Interactive text mode

Interactive text mode is intended for an operator. Commands can be entered one
at a time, errors do not terminate the program, and the prompt indicates
whether a board object is connected.

```text
lansing(disconnected)> ports
COM6    USB Serial Device (COM6)
lansing(disconnected)> connect COM6
[12:10:07] Connected to LANSING firmware 0.1.2 on COM6.
...
lansing>
```

Use `help` to list commands and `help <command>` for the command's built-in
summary.

### Scripted text mode

Scripted mode is intended for shell scripts and repeatable bench sequences:

```powershell
python lansing_terminal.py --port COM6 -c "psu on; psuc on; detect; diagnose 0"
```

It is fail-fast. Any terminal, SDK, firmware, validation, or operating-system
error stops the remaining commands.

### JSON/NDJSON mode

JSON mode preserves progress and asynchronous messages by emitting multiple
JSON objects rather than constructing one final document. Consumers should
parse stdout one line at a time.

PowerShell example:

```powershell
python lansing_terminal.py -j -c "ports" | ForEach-Object { $_ | ConvertFrom-Json }
```

Python subprocess example:

```python
import json
import subprocess

process = subprocess.Popen(
    ["python", "lansing_terminal.py", "-j", "--port", "COM6", "-c", "status; diagnose 0"],
    stdout=subprocess.PIPE,
    text=True,
)

assert process.stdout is not None
for line in process.stdout:
    record = json.loads(line)
    print(record)

return_code = process.wait()
```

## Board and actuator model

### Actuator numbering

The SDK supports actuator indices `0` through `23`, divided into three groups:

| Group | Actuator indices |
| ---: | --- |
| `0` | `0` through `7` |
| `1` | `8` through `15` |
| `2` | `16` through `23` |

Most standard Lansing Development Kits populate group `0` only.

### Actuator states

The terminal maintains SDK-side state for each actuator:

| State | Meaning | Normal output allowed? |
| --- | --- | --- |
| `Unknown` | No diagnosis has been recorded in this terminal session. | No |
| `Ready` | Detection measured an acceptable current change. | Yes |
| `Error` | Detection measured excessive current change. Initialize before use. | No |
| `Not connected` | Detection did not measure a meaningful current change. | No |

Actuators start as `Unknown` whenever a new `Lansing` object is created.
Disconnecting and reconnecting therefore resets the terminal's SDK-side state.

### Detection thresholds

Detection compares forward current with baseline current:

```text
delta_mA = abs(forward_mA - baseline_mA)
```

| Current delta | Classification |
| --- | --- |
| `< 0.1 mA` | `Not connected` |
| `0.1 mA` through `3.0 mA` inclusive | `Ready` |
| `> 3.0 mA` | `Error` |

The discharge-current measurement is reported for diagnostic context but is
not used in the current SDK classification threshold calculation.

### Power controls

The board exposes two independent controls:

1. `psu on` enables the high-voltage power supply.
2. `psuc on` turns on the PSU connection to the actuator path.

Firmware rejects `psuc on` while the PSU is off. Diagnostics, initialization,
and normal actuator output generally require both the PSU and PSU connection
to be on.

### Output and discharge

`set <actuator> <value>` uses normal, forward-only output. Values are integers
from `0` through `255`. A transition to `0` starts automatic discharge. The
firmware can refuse a new nonzero command while discharge is still active.

The `square` command watches firmware debug messages and does not begin the next
forward phase until discharge completion has been confirmed.

## Recommended first-use workflow

### 1. Discover and connect

```text
lansing(disconnected)> ports
lansing(disconnected)> connect COM6
```

### 2. Enable power in the correct order

```text
lansing> psu on
lansing> voltage
lansing> psuc on
lansing> current
```

A powered Lansing kit is commonly around 215–220 V, but use the values and
limits specified for the hardware being tested.

### 3. Detect the actuator

```text
lansing> detect
```

Interpret the result:

- `Ready`: normal output is available.
- `Error`: run `init 0` or `fast_init 0`, then inspect the final detection.
- `Not connected`: keep it off and inspect the physical connection.

### 4. Initialize when necessary

```text
lansing> init 0
```

Initialization takes about two minutes with the default SDK stages. It reports
progress and performs a final diagnosis automatically.

Fast Init is available as an alternate initialization method when a shorter,
adaptive target-current process is preferred:

```text
lansing> fast_init 0 2.0
```

The target must be below the `3.0 mA` Error threshold.

### 5. Run a controlled pulse

```text
lansing> set 0 255
lansing> set 0 0
```

Wait for discharge before attempting another pulse.

### 6. Shut down

```text
lansing> off all
lansing> psuc off
lansing> psu off
lansing> disconnect
```

## Complete command reference

Arguments shown in angle brackets are required. Arguments shown in square
brackets are optional. Type literal words such as `group`, `get`, and `set`
exactly as shown.

Unless explicitly stated otherwise, board commands require an active
connection.

### `help`

```text
help
help <command>
```

Lists all commands or shows the built-in summary for one command.

Examples:

```text
help
help detect
help square
```

In JSON mode, the result is either:

```json
{"event":"help","commands":["config","connect","current"]}
```

or:

```json
{"event":"help","topic":"detect","text":"detect [<actuator>|group <0|1|2>]..."}
```

### `ports`

```text
ports
```

Lists physical serial ports and port aliases configured through
`FLUID_REALITY_VIRTUAL_PORTS`, such as
`COM66=tcp://127.0.0.1:8765`. A board connection is not required.

Text example:

```text
COM6                           serial port
COM66                          virtual TCP port
```

JSON example:

```json
{"event":"serial_port","port":"COM66","description":"virtual TCP port"}
```

One object is emitted per port. If no ports are found:

```json
{"event":"serial_ports","ports":[]}
```

If several ports are listed, unplug and reconnect the Lansing board and compare
the results, or identify the USB serial device through the operating system.

### `connect`

```text
connect <port>
```

Closes any current board object, opens the requested serial or TCP endpoint,
forces a clean text-protocol boundary, reads the firmware version, and prints a
status snapshot.

Examples:

```text
connect COM6
connect /dev/cu.usbmodem1101
connect /dev/ttyACM0
connect tcp://127.0.0.1:8765
```

If a square wave is running, `connect` stops it before changing the connection.
If firmware validation fails during connection, the newly opened serial object
is closed. A missing or busy port is reported without a Python traceback.

Successful connection output includes a log record followed by the same output
as `status`.

### `disconnect`

```text
disconnect
```

Stops any running square wave, closes the serial connection, clears the current
port, and returns to the disconnected state. Calling it while already
disconnected is harmless.

Disconnecting discards SDK-side actuator classifications. After reconnecting,
actuators begin in `Unknown` state and must be detected again.

### `status`

```text
status
```

Reads the board's multi-line status response and reports:

- connected board endpoint;
- PSU state;
- PSU-connection state;
- measured voltage and current;
- maximum-active and discharge timing configuration;
- manual-output safety state;
- firmware-debug state;
- square-wave state; and
- counts of `Ready`, `Error`, `Not connected`, and `Unknown` actuators.

The actuator counts are based on diagnoses recorded by the current SDK object,
not solely on raw firmware actuator state numbers.

JSON example:

```json
{"event":"status","port":"COM6","power_supply":"on","psu_connection":"on","voltage_v":218.4,"current_ma":1.33,"config":{"max_active_ms":5000,"discharge_ms":2000,"safe":true,"debug":false},"square_wave":{"running":false,"actuators":[]},"actuator_counts":{"Unknown":23,"Ready":0,"Error":1,"Not connected":0}}
```

### `psu`

```text
psu
psu on
psu off
```

With no argument, reads the high-voltage power-supply state. With an argument,
sets it.

The accepted values are `on` and `off`.

Turning the PSU on does not connect it to the actuator path. Use `psuc on`
separately when the setup is ready.

JSON example:

```json
{"event":"power_supply","state":"on"}
```

### `psuc`

```text
psuc
psuc on
psuc off
```

With no argument, reads the PSU-connection state. With an argument, turns the
PSU connection to the actuator path on or off.

The accepted values are `on` and `off`. Firmware rejects `psuc on` while the
PSU is off.

JSON example:

```json
{"event":"psu_connection","state":"on"}
```

The reported state is `on` or `off`.

### `voltage`

```text
voltage
voltage <measurement_ms>
```

Reads supply/output voltage in volts. The optional positive integer requests a
firmware-side measurement interval in milliseconds.

Examples:

```text
voltage
voltage 100
```

JSON example:

```json
{"event":"voltage","voltage_v":218.4}
```

`measurement_ms` must be at least `1`.

### `current`

```text
current
```

Reads current in milliamps.

JSON example:

```json
{"event":"current","current_ma":1.33}
```

### `config`

```text
config
config show
config get <MAX|DIS|SAFE|DEBUG>
config set <MAX|DIS|SAFE|DEBUG> <value>
```

Reads or modifies firmware runtime configuration. `config` and `config show`
are equivalent.

| Key | Meaning | Typical value form |
| --- | --- | --- |
| `MAX` | Maximum continuous normal activation time | non-negative milliseconds |
| `DIS` | Maximum automatic discharge duration | non-negative milliseconds |
| `SAFE` | Manual `OUT` safety gate | `ON` or `OFF` |
| `DEBUG` | Firmware `DBG:` message generation | `ON` or `OFF` |

Examples:

```text
config show
config get MAX
config set MAX 12000
config get DIS
config set DIS 2000
config set DEBUG ON
config set SAFE OFF
```

`MAX` and `DIS` are persistent firmware settings. `SAFE` normally boots on and
`DEBUG` normally boots off. Do not leave `SAFE` off after raw manual-output
work.

JSON `show` example:

```json
{"event":"config","max_active_ms":5000,"discharge_ms":2000,"safe":true,"debug":false}
```

JSON `get` and `set` results include `operation`, `key`, and `value`:

```json
{"event":"config","operation":"get","key":"MAX","value":"5000"}
```

### `safety`

```text
safety
safety on
safety off
```

Reads or modifies the firmware manual-output safety gate. This is a convenience
command for `config get SAFE` and `config set SAFE ...`.

Safety affects raw `manual set`/firmware `OUT` control. It is not permission to
bypass the state requirements of normal `set` or `square` operation.

JSON example:

```json
{"event":"safety","enabled":true}
```

### `detect`

```text
detect
detect <actuator>
detect group <0|1|2>
```

Runs the SDK detection workflow for group 0, one actuator, or all eight
actuators in a selected group. With no arguments, `detect` runs group 0,
covering actuators `0-7`.

For each actuator, the SDK:

1. commands every actuator in the same eight-actuator group off;
2. tolerates a normal `ACT_FAILED` lockout if an actuator is discharging;
3. runs the firmware diagnostic;
4. computes the baseline-to-forward current delta;
5. classifies the actuator; and
6. stores the result in the current SDK object.

Examples:

```text
detect
detect 0
detect group 0
detect group 2
```

Text result:

```text
Actuator 0: Error, delta 5.14 mA (baseline 1.33, forward 6.47, discharge 6.43)
```

JSON mode first emits a progress record:

```json
{"event":"detection_started","actuator":0}
```

It then emits the structured result requested for that actuator:

```json
{"actuator":0,"state":"Error","delta_ma":5.14,"baseline_ma":1.33,"forward_ma":6.47,"discharge_ma":6.43}
```

Group detection emits these records separately for each actuator, allowing a
consumer to process results incrementally.

### `diagnose`

```text
diagnose <actuator>
```

Runs the firmware diagnostic for one actuator, applies the SDK detection
thresholds, updates its SDK-side state, and prints the same structured result as
`detect`.

Unlike `detect`, this command does not first command all eight members of the
actuator group off through the SDK detection wrapper. Use `detect` for the
standard first-use workflow and `diagnose` to reclassify after initialization or
recovery.

Example:

```text
diagnose 0
```

### `init`

```text
init <actuator>
```

Runs the SDK's staged actuator initialization/conditioning sequence and then
diagnoses the actuator again.

Prerequisites:

- the board is connected;
- the actuator has already been detected;
- its state is not `Unknown` or `Not connected`;
- measured PSU voltage is greater than zero; and
- the hardware is prepared for a roughly two-minute conditioning sequence.

The default SDK sequence has four 30-second voltage stages:

1. ±25 V
2. ±50 V
3. ±100 V
4. ±200 V

At each stage, initialization alternates raw positive and negative drive every
0.5 seconds. It temporarily disables manual-output safety, commands the manual
output back to zero, restores safety in cleanup, and performs a final diagnosis.

Text mode updates one progress line. JSON mode emits one
`initialization_progress` object for each SDK progress callback:

```json
{"event":"initialization_progress","actuator":0,"elapsed_s":31.1,"total_s":120.0,"stage":2,"stage_count":4,"voltage_v":50.0}
```

Completion produces:

```json
{"event":"initialization_complete","actuator":0,"state":"Ready"}
```

followed by the full actuator detection object.

### `fast_init`

```text
fast_init <actuator> [target_ma]
```

Runs adaptive Fast Init on one detected actuator. The optional target is a
current delta in milliamps. It defaults to `2.0 mA` and must be greater than
zero and strictly below the SDK Error threshold of `3.0 mA`.

Examples:

```text
fast_init 0
fast_init 0 2.0
fast_init 7 1.5
```

Prerequisites:

- the board is connected;
- the PSU is on and the PSU connection is on;
- the actuator has already been detected;
- its state is `Ready` or `Error`, not `Unknown` or `Not connected`;
- measured PSU voltage is greater than zero.

The process temporarily disables manual-output safety, drives a 1 Hz bipolar
manual square wave, and restores safety in cleanup. It starts at the measured
maximum PSU voltage. After each positive/negative cycle, it compares the
measured baseline-to-forward current delta with the target:

| Current-delta error | Voltage step |
| ---: | ---: |
| `0` through `0.2 mA` | `5 V` |
| greater than `0.2 mA` through `1.0 mA` | `10 V` |
| greater than `1.0 mA` | `20 V` |

When the measured delta is above the target, Fast Init lowers the drive
voltage by the step. When the measured delta is below the target, it raises the
drive voltage by the step.

The process ends with:

- success: the actuator is running at maximum voltage and the current delta is
  at or below the target;
- failure: the process runs longer than 60 seconds.

After Fast Init stops, the terminal diagnoses the actuator again and emits the
normal structured actuator result.

Text progress example:

```text
Fast Init 0: 12/60s, delta 2.34 mA, target 2.00 mA, drive 185 V, reducing.
```

JSON records include `fast_init_started`, `fast_init_progress`, and
`fast_init_complete`:

```json
{"event":"fast_init_progress","actuator":0,"elapsed_s":12.1,"duration_s":60.0,"target_delta_ma":2.0,"target_voltage_v":185.0,"next_voltage_v":175.0,"supply_voltage_v":218.0,"baseline_ma":0.82,"forward_ma":3.16,"reverse_ma":2.91,"delta_ma":2.34,"error_ma":0.34,"step_v":10.0,"status":"reducing"}
```

```json
{"event":"fast_init_complete","success":true,"final_state":"Ready","actuator":0,"delta_ma":1.82,"target_delta_ma":2.0,"target_voltage_v":218.0,"status":"success"}
```

### `recover`

```text
recover <actuator> [voltage] [duration_s]
```

Performs advanced manual-output recovery. Optional arguments are positional,
not `name=value` expressions.

Defaults:

- voltage: `50` V
- duration: `60` seconds

Examples:

```text
recover 0
recover 0 75
recover 0 100 90
```

The procedure:

1. measures supply voltage and baseline current;
2. scales the requested recovery voltage from the measured supply voltage;
3. records the current manual-output safety state;
4. disables safety when it was previously enabled;
5. alternates positive and negative manual drive every 0.5 seconds;
6. samples current repeatedly and reports current delta once per second;
7. commands both manual electrodes to zero;
8. restores the previous safety state; and
9. reports average recovery current and its delta from baseline.

Scaling is based on measured supply voltage. For example, a 100 V target with
a 200 V supply drives approximately half of the available supply voltage in
each direction.

The command requires positive voltage and duration values and measured PSU
voltage above zero. The terminal does not automatically reclassify the actuator
after recovery. Run:

```text
diagnose <actuator>
```

JSON recovery records are `recovery_started`, `recovery_progress`, and
`recovery_complete`:

```json
{"event":"recovery_complete","actuator":0,"baseline_ma":1.33,"average_ma":2.04,"delta_ma":0.71}
```

### `set`

```text
set <actuator> <value>
```

Sets normal actuator output. `actuator` must be `0–23`; `value` must be an
integer from `0–255`.

The SDK permits this command only when the selected actuator is `Ready` in the
current session. Run `detect <actuator>` first. Firmware additionally enforces
PSU state, PSU connection, maximum active time, and discharge lockout.

Examples:

```text
set 0 64
set 0 255
set 0 0
```

`set 0 0` starts firmware-managed discharge after a nonzero activation. It does
not use raw manual negative output.

JSON example:

```json
{"event":"actuator_output","actuator":0,"value":255}
```

### `off`

```text
off <actuator>
off all
```

Commands one actuator or all 24 actuators to normal output value `0`.

Examples:

```text
off 0
off all
```

`off all` sends an individual normal off command for every actuator without
requiring SDK `Ready` state. `off <actuator>` uses the state-gated normal SDK
setter and therefore requires that actuator to be `Ready`. An actuator that was
active may enter automatic discharge.

`off all` does **not** stop the square-wave worker. If a square wave is running,
use `square stop`; otherwise the worker can activate its selected actuators
again on its next cycle.

JSON examples:

```json
{"event":"actuators_off","actuator":0}
{"event":"actuators_off","actuator":"all"}
```

### `square`

```text
square start <actuator> [actuator...]
square status
square stop
```

Starts, inspects, or stops a continuous background square-wave test.

Every selected actuator must already be `Ready`. Duplicate actuator arguments
are removed and the final list is sorted.

The runner:

1. remembers the current firmware-debug setting;
2. enables firmware debug when necessary;
3. commands every selected actuator to `255`;
4. holds forward output for one second;
5. commands every selected actuator to `0`;
6. waits one second;
7. watches for `DBG:DISCHARGE_STOP,ACT><actuator>` confirmation for every
   selected actuator; and
8. repeats until stopped.

Examples:

```text
square start 0
square start 0 1 2
square status
square stop
```

The square wave stops when the operator runs `square stop`, `disconnect`,
`connect`, `exit`, or when its worker encounters an unrecoverable error. The
previous firmware-debug setting is restored during cleanup.

The command runs in a background thread, so the interactive terminal remains
available. Status and progress are written as terminal log events. `square
status` emits:

```json
{"event":"square_wave","running":true,"actuators":[0,1,2]}
```

### `runtime`

```text
runtime
runtime <actuator>
```

Reads accumulated actuator runtime. With no argument, prints one record for
each of the 24 actuators. With an actuator argument, prints only that actuator.

Text output chooses an appropriate display unit (`ms`, `s`, `min`, or `h`).
JSON output always reports the original integer milliseconds:

```json
{"event":"runtime","actuator":0,"runtime_ms":12345}
```

### `reset_runtimes`

```text
reset_runtimes
```

Resets the persistent runtime counters for all actuators. This operation has no
per-actuator form and cannot be undone through the terminal.

Do not confuse runtime reset with board reboot. The corresponding firmware
commands are distinct: runtime reset is `RST`; reboot is `RBT`.

JSON result:

```json
{"event":"runtimes_reset"}
```

### `states`

```text
states
states group <0|1|2>
```

Shows SDK-side state for all actuators or one eight-actuator group. When a last
detection record exists, the command also reports baseline, forward, discharge,
and delta measurements.

Examples:

```text
states
states group 0
```

An actuator without a detection record:

```json
{"event":"actuator_state","actuator":0,"state":"Unknown"}
```

An actuator with a detection record:

```json
{"event":"actuator_state","actuator":0,"state":"Ready","delta_ma":0.71,"baseline_ma":1.33,"forward_ma":2.04,"discharge_ma":1.92}
```

These states are session-local. They are not automatically restored after a
disconnect or process restart.

### `manual`

```text
manual get <actuator>
manual set <actuator> <positive> <negative>
```

Reads or writes raw positive/negative electrode values for advanced bench
testing. Both values must be integers from `0–255`.

Examples:

```text
manual get 0
safety off
manual set 0 255 0
manual set 0 0 255
manual set 0 0 0
safety on
```

Unlike normal `set`, `manual set` can address positive and negative electrodes
independently. It is not gated by SDK `Ready` state. Firmware manual-output
safety normally rejects raw writes until safety is deliberately disabled.

Always return both values to zero and restore safety after a raw-output test.

JSON example:

```json
{"event":"manual_output","actuator":0,"positive":255,"negative":0}
```

### `debug`

```text
debug on
debug off
debug file <path>
```

Controls SDK-side diagnostic output:

- `debug on` retains SDK debug lines and prints new lines to the terminal;
- `debug off` retains SDK debug lines but stops printing them; and
- `debug file <path>` sends future SDK debug lines to the specified file.

This command does not itself enable firmware `DBG:` generation. Use:

```text
config set DEBUG ON
```

when firmware debug messages are required.

Paths containing spaces must be quoted:

```text
debug file "C:\logs\lansing session.log"
```

Verbose JSON debug record:

```json
{"event":"debug_message","message":"2026-08-06 12:10:07 | Lansing | status.start"}
```

### `log`

```text
log show
log clear
log save <path>
```

Manages the in-memory terminal event/debug log.

- `log show` prints all retained lines;
- `log clear` removes all retained lines from memory; and
- `log save <path>` writes the current lines as UTF-8 text with one line per
  record.

The log contains timestamped terminal events plus SDK debug lines received by
the configured debug callback. Clearing the log does not change board or
firmware state.

In JSON mode, `log show` emits one object containing a `lines` array:

```json
{"event":"log","lines":["[12:10:07] Connected to LANSING firmware 0.1.2 on COM6."]}
```

### `reboot`

```text
reboot
```

Sends the firmware reboot command. A reboot can temporarily interrupt the
serial protocol. If subsequent commands fail or time out, run `disconnect`,
wait for the serial device to reappear, and connect again.

`reboot` does not reset actuator runtime counters. Use `reset_runtimes` only
when a deliberate runtime reset is required.

JSON result:

```json
{"event":"reboot"}
```

### `exit`, `quit`, and end-of-file

```text
exit
quit
```

Both commands stop any running square wave, close the board connection, and
exit the interactive terminal. Ctrl-D on macOS/Linux invokes the same cleanup
through end-of-file handling. On Windows, the console end-of-file key is
typically Ctrl-Z followed by Enter.

## JSON output

### Format contract

JSON output is NDJSON, not one JSON array:

```text
{...}\n
{...}\n
{...}\n
```

Properties of the format:

- every nonempty stdout line is a complete JSON object;
- measurements are JSON numbers, not strings with appended units;
- units are encoded in field names such as `voltage_v`, `current_ma`,
  `duration_s`, and `runtime_ms`;
- boolean state is represented by JSON booleans where the API naturally
  returns a boolean;
- long operations emit incremental progress objects;
- asynchronous square-wave messages use log records; and
- errors are JSON objects and still affect the process exit status.

Consumers must not assume that one command produces only one object. For
example, scripted detection produces a `command` record, a
`detection_started` record, and the final actuator result.

### Event and result schemas

| Output | Important fields | Produced by |
| --- | --- | --- |
| Command echo | `event="command"`, `command` | Every `-c` command |
| Help | `event="help"`, `commands` or `topic`, `text` | `help` |
| Serial port | `event="serial_port"`, `port`, `description` | `ports` |
| No board endpoints | `event="serial_ports"`, `ports=[]` | `ports` |
| Log | `event="log"`, `timestamp`, `level`, `message` | connection and square-wave events |
| Error | `event="error"`, `timestamp`, `level`, `message` | any handled command error |
| Status | `event="status"`, telemetry, config, square-wave state, counts | `status`, successful `connect` |
| PSU | `event="power_supply"`, `state` | `psu` |
| PSU connection | `event="psu_connection"`, `state` | `psuc` |
| Voltage | `event="voltage"`, `voltage_v` | `voltage` |
| Current | `event="current"`, `current_ma` | `current` |
| Configuration | `event="config"`, configuration fields | `config` |
| Safety | `event="safety"`, `enabled` | `safety` |
| Detection start | `event="detection_started"`, `actuator` | `detect` |
| Detection result | `actuator`, `state`, four current fields | `detect`, `diagnose`, `init`, `fast_init` |
| Initialization progress | `event="initialization_progress"`, timing and stage fields | `init` |
| Initialization complete | `event="initialization_complete"`, `actuator`, `state` | `init` |
| Fast Init start/progress/complete | `event="fast_init_*"`, target, voltage, current, and result fields | `fast_init` |
| Recovery start/progress/complete | `event="recovery_*"`, procedure measurements | `recover` |
| Actuator output | `event="actuator_output"`, `actuator`, `value` | `set` |
| Actuator off | `event="actuators_off"`, `actuator` | `off` |
| Square wave | `event="square_wave"`, `running`, `actuators` | `square status` |
| Runtime | `event="runtime"`, `actuator`, `runtime_ms` | `runtime` |
| Runtime reset | `event="runtimes_reset"` | `reset_runtimes` |
| Actuator state | `event="actuator_state"`, state and optional measurements | `states` |
| Manual output | `event="manual_output"`, electrode values | `manual` |
| SDK debug | `event="debug_message"`, `message` | verbose/debug output |
| Log contents | `event="log"`, `lines` | `log show` |
| Reboot | `event="reboot"` | `reboot` |

### Error schema

Handled errors have this form:

```json
{"event":"error","timestamp":"12:10:07","level":"error","message":"Could not open serial port 'COM4': ..."}
```

The message is intended for people and may contain operating-system details.
Automation should use `event == "error"` and the process exit status as its
primary failure signals rather than matching the complete message string.

### Filtering command echoes

Scripted JSON mode emits the command before its results:

```json
{"event":"command","command":"diagnose 0"}
```

If a consumer wants only result records, ignore objects whose `event` is
`command`. Detection result objects intentionally have no `event` property;
they are identified by the combination of `actuator`, `state`, `delta_ma`,
`baseline_ma`, `forward_ma`, and `discharge_ma`.

## Exit status and error handling

### Interactive mode

Handled errors are printed and the terminal remains open. This includes:

- invalid command syntax or numeric ranges;
- attempts to use board commands while disconnected;
- SDK state violations, such as using `set` before detection;
- firmware `ER:` responses;
- serial transport failures;
- malformed protocol responses; and
- relevant operating-system errors.

Example:

```text
lansing(disconnected)> connect COM4
[12:10:07] Error: Could not open serial port 'COM4': ...
lansing(disconnected)>
```

### Scripted mode

| Exit status | Meaning |
| ---: | --- |
| `0` | Every requested command completed without a handled error. |
| `1` | Startup connection or a scripted command reported an error. |

On a scripted error, remaining commands are skipped and the board is closed.

### Ctrl-C

Ctrl-C at the main interactive prompt closes the board through terminal exit
cleanup. Ctrl-C timing during a blocking firmware or initialization operation
can depend on the platform and serial call in progress; after interruption,
confirm that the PSU connection and power-supply state are safe before
continuing.

## Automation examples

### Discover ports as JSON

```powershell
python lansing_terminal.py -j -c "ports"
```

Example output:

```json
{"event":"command","command":"ports"}
{"event":"serial_port","port":"COM6","description":"USB Serial Device (COM6)"}
```

### Read a status snapshot

```powershell
python lansing_terminal.py -j --port COM6 -c "status"
```

Remember that a successful startup connection already emits a status snapshot;
an explicit `status` command emits another.

### Detect one actuator and preserve the exit code

PowerShell:

```powershell
python lansing_terminal.py -j --port COM6 -c "psu on; psuc on; detect 0" |
    Tee-Object -FilePath detection.ndjson
if ($LASTEXITCODE -ne 0) {
    throw "Lansing terminal command failed"
}
```

Bash:

```bash
python lansing_terminal.py -j --port /dev/ttyACM0 \
  -c "psu on; psuc on; detect 0" | tee detection.ndjson
test "${PIPESTATUS[0]}" -eq 0
```

### Detect an entire group

```powershell
python lansing_terminal.py -j --port COM6 -c "psu on; psuc on; detect"
```

Bare `detect` is equivalent to `detect group 0`. The terminal emits eight
separate detection results rather than one array.

### Initialize actuators to a target current delta

The `lansing_terminal` directory includes a target-current automation workflow
for every supported command environment:

| File | Environment | Implementation |
| --- | --- | --- |
| [`initialize_all.ps1`](../initialize_all.ps1) | Windows PowerShell or PowerShell 7 | Native PowerShell workflow |
| [`initialize_all.sh`](../initialize_all.sh) | Linux, macOS, or another POSIX shell | Launcher for `initialize_all.py` |
| [`initialize_all.bat`](../initialize_all.bat) | Windows Command Prompt | Launcher for `initialize_all.py` |
| [`initialize_all.py`](../initialize_all.py) | Any supported Python platform | Shared implementation used by `.sh` and `.bat` |

All versions use `lansing_terminal` JSON mode internally. They do not attempt
to scrape or interpret human-readable terminal text.

#### What the target means

The target is the actuator's baseline-to-forward current delta, in milliamps:

```text
delta_mA = abs(forward_mA - baseline_mA)
```

It is not the absolute baseline current, forward-current measurement, discharge
current, PSU current limit, or actuator output value.

The target must be from `0.1` through `3.0 mA`:

- below `0.1 mA`, the SDK classifies the actuator as `Not connected`;
- from `0.1` through `3.0 mA`, the SDK classifies it as `Ready`; and
- above `3.0 mA`, the SDK classifies it as `Error`.

A typical invocation using a `1.5 mA` target is:

```powershell
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5
```

```bash
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5
```

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5
```

#### Processing algorithm

The scripts process actuators in sorted numerical order. Duplicate actuator
arguments are removed. With no actuator selection, all indices `0–23` are
processed.

For each actuator, the workflow is:

1. Start a terminal process on the requested board endpoint.
2. Run `psu on; psuc on; detect <actuator>`.
3. Read the final structured detection record from terminal NDJSON.
4. If the actuator is `Not connected`, report it and skip initialization.
5. If it is `Ready` and its delta is already at or below the target, report
   `Target reached` without initializing it.
6. If its delta is above target, run a new terminal process containing `detect
   <actuator>; init <actuator>`.
7. Read the final post-initialization detection record.
8. Continue even if the SDK now reports `Ready` when the delta is still above
   the requested target.
9. Compare the new delta with the previous post-detection delta.
10. Stop successfully when the actuator is `Ready` and
    `delta_ma <= target_delta_ma`.
11. Stop unsuccessfully when the delta no longer improves, the actuator changes
    to `Not connected`, a command fails, an unexpected state is returned, or
    the maximum attempt count is reached.
12. Continue with the next actuator after reporting the result.

Detection and initialization intentionally run in the same terminal process on
each attempt. Every new SDK object starts with the actuator in `Unknown` state,
while `init` requires a preceding detection.

#### Stop conditions

| Condition | Result | Script failure? |
| --- | --- | --- |
| Initial or post-initialization delta reaches target while `Ready` | `Target reached` | No |
| Initial detection is `Not connected` | `Not connected`; no initialization attempted | No |
| Post-initialization state changes to `Not connected` | `Not connected`; processing stops | Yes |
| New delta improvement is less than or equal to the configured minimum | `Stalled` | Yes |
| Maximum initialization attempts are exhausted above target | `Above target` | Yes |
| Terminal, transport, firmware, or protocol command fails | `Command failed` | Yes |
| Detection result is missing or has an unexpected state | `No result` or `Unexpected state` | Yes |

With the default minimum improvement of `0`, every new delta must be strictly
lower than the prior delta. An unchanged or higher delta stops that actuator.
For noisy measurements, use a positive minimum improvement so insignificant
changes do not count as progress.

#### Default safety bounds

- All 24 actuator indices are considered unless an explicit list is supplied.
- No more than 10 initialization attempts are made per actuator by default.
- Every successful repeat must reduce the measured delta.
- The target is constrained to the SDK's connected/ready range.
- The PSU connection is turned off before the PSU is turned off during cleanup.
- Cleanup runs after normal completion, errors, and operator interruption.

Initialization is a long, active conditioning procedure. With default SDK
timing, one attempt takes about two minutes. Ten attempts on one actuator can
therefore take roughly 20 minutes, excluding detection and serial setup time.
Choose an attempt limit appropriate for the supervised hardware procedure.

#### PowerShell syntax

```text
.\initialize_all.ps1
    -Port <string>
    -TargetDeltaMa <double>
    [-Actuators <int[]>]
    [-PythonExecutable <string>]
    [-TerminalPath <string>]
    [-MaxInitializationAttempts <int>]
    [-MinimumDeltaImprovementMa <double>]
    [-LeavePowerOn]
    [-Verbose]
```

PowerShell parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `-Port` | Yes | — | Serial port such as `COM6` or `/dev/ttyACM0` |
| `-TargetDeltaMa` | Yes | — | Target current delta, `0.1–3.0 mA` |
| `-Actuators` | No | `0..23` | Comma-separated PowerShell integer array |
| `-PythonExecutable` | No | `python` | Python used to run `lansing_terminal.py` |
| `-TerminalPath` | No | Adjacent `lansing_terminal.py` | Alternate Lansing terminal path |
| `-MaxInitializationAttempts` | No | `10` | Per-actuator attempt limit, `1–100` |
| `-MinimumDeltaImprovementMa` | No | `0` | Required strict decrease per attempt |
| `-LeavePowerOn` | No | Disabled | Skip automatic PSU-connection and PSU shutdown |
| `-Verbose` | No | Disabled | Print each generated terminal invocation |

Show full comment-based help:

```powershell
Get-Help .\initialize_all.ps1 -Full
```

#### Linux/macOS and Command Prompt syntax

The `.sh` and `.bat` launchers forward their arguments to
`initialize_all.py`:

```text
initialize_all.sh|initialize_all.bat
    --port PORT
    --target-delta-ma MA
    [--actuators N [N ...]]
    [--python-executable PATH]
    [--terminal-path PATH]
    [--max-initialization-attempts N]
    [--minimum-delta-improvement-ma MA]
    [--leave-power-on]
    [--verbose]
```

Shared options:

| Option | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--port PORT` | Yes | — | Serial device used by the Lansing controller |
| `--target-delta-ma MA` | Yes | — | Target current delta, `0.1–3.0 mA` |
| `--actuators N [N ...]` | No | `0–23` | Space-separated actuator indices |
| `--python-executable PATH` | No | Current interpreter | Python used to launch `lansing_terminal.py` |
| `--terminal-path PATH` | No | Adjacent `lansing_terminal.py` | Alternate Lansing terminal path |
| `--max-initialization-attempts N` | No | `10` | Per-actuator attempt limit; must be at least `1` |
| `--minimum-delta-improvement-ma MA` | No | `0` | Required strict delta decrease; cannot be negative |
| `--leave-power-on` | No | Disabled | Skip automatic PSU-connection and PSU shutdown |
| `--verbose` | No | Disabled | Print each generated terminal invocation |
| `-h`, `--help` | No | — | Print argument help and exit |

Show the generated option reference:

```bash
./initialize_all.sh --help
```

```bat
initialize_all.bat --help
```

#### Linux and macOS setup

From the `apps/lansing_terminal` directory:

```bash
chmod +x initialize_all.sh
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5
```

If the executable bit is unavailable after copying or extracting the file, run
it explicitly through a POSIX shell:

```bash
sh initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5
```

The launcher uses `python3` by default. Override the launcher interpreter with
the `PYTHON_EXECUTABLE` environment variable:

```bash
PYTHON_EXECUTABLE=/opt/fluid-reality/bin/python \
    ./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5
```

This environment variable selects the interpreter that runs
`initialize_all.py`. The separate `--python-executable` option selects the
interpreter that the automation process uses to launch `lansing_terminal.py`.
Normally they should refer to the same environment.

#### Windows Command Prompt setup

From the `apps\lansing_terminal` directory:

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5
```

The launcher uses `python` by default. Override it before invocation when the
required packages are installed in another environment:

```bat
set "PYTHON_EXECUTABLE=C:\research\FluidReality\sdk\.venv\Scripts\python.exe"
initialize_all.bat --port COM6 --target-delta-ma 1.5
```

The batch launcher preserves and returns the exit status from
`initialize_all.py`.

#### Selecting actuators

Process only standard group 0.

PowerShell uses a comma-separated array:

```powershell
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5 `
    -Actuators 0,1,2,3,4,5,6,7
```

Shell and batch use space-separated values:

```bash
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5 \
    --actuators 0 1 2 3 4 5 6 7
```

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5 --actuators 0 1 2 3 4 5 6 7
```

Process a noncontiguous selection:

```powershell
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5 -Actuators 0,3,7,12
```

```bash
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5 \
    --actuators 0 3 7 12
```

#### Configuring improvement and attempt limits

Require a decrease greater than `0.05 mA` on every attempt and allow at most
four attempts per actuator:

```powershell
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5 -Actuators 0 `
    -MaxInitializationAttempts 4 `
    -MinimumDeltaImprovementMa 0.05
```

```bash
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5 \
    --actuators 0 \
    --max-initialization-attempts 4 \
    --minimum-delta-improvement-ma 0.05
```

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5 --actuators 0 --max-initialization-attempts 4 --minimum-delta-improvement-ma 0.05
```

The comparison is:

```text
improvement_mA = previous_delta_mA - new_delta_mA
```

Processing stops as `Stalled` when:

```text
improvement_mA <= minimum_required_improvement_mA
```

#### Output and summary

The scripts report:

- initial state and current delta;
- initialization attempt number and prior delta;
- live initialization stage progress where supported;
- new state, new delta, and measured improvement;
- target-reached, stalled, disconnected, or error messages; and
- a final summary covering every processed actuator.

Example summary:

```text
Initialization summary
Actuator  Status          Delta mA  Attempts  Detail
--------  --------------  --------  --------  -------------------------------
0         Target reached  1.000     0         Target reached on initial detection
1         Target reached  1.400     3         Target delta reached
2         Stalled         5.000     1         Delta stopped decreasing
3         Not connected   0.020     0         No initialization attempted
```

The summary is human-readable text. Terminal NDJSON is consumed internally and
is not copied to normal script output. Use `--verbose` or `-Verbose` to show the
generated terminal commands when diagnosing automation behavior.

#### Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Every connected actuator reached the target; initially not-connected actuators were skipped. |
| `1` | At least one actuator stalled, stayed above target, changed to not connected after initialization, or encountered a command/result failure. |
| `2` | `.sh`/`.bat` argument parsing or validation failed. |
| `130` | `.sh`/`.bat` shared workflow was interrupted by the operator. |

The `.sh` launcher uses `exec`, and the `.bat` launcher explicitly forwards the
shared Python process exit code. Shell scripts and CI jobs should test the exit
status instead of relying only on summary text.

Bash example:

```bash
if ./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5; then
    echo "All connected actuators reached target"
else
    code=$?
    echo "Initialization workflow failed with exit code $code" >&2
    exit "$code"
fi
```

Batch example:

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo Initialization workflow failed with exit code %RESULT% 1>&2
    exit /b %RESULT%
)
```

#### Automatic power cleanup

By default, every version attempts this shutdown sequence in cleanup:

```text
psuc off; psu off
```

The sequence is attempted after successful completion, actuator failures, and
operator interruption. A shutdown failure is reported and makes the overall
workflow unsuccessful.

Use the leave-power-on option only when a supervised workflow intentionally
requires it:

```powershell
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5 -LeavePowerOn
```

```bash
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5 --leave-power-on
```

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5 --leave-power-on
```

When selected, the scripts print a prominent warning that the PSU connection
and PSU were intentionally left on.

#### Automation troubleshooting

**The script cannot find Python.** Activate the intended virtual environment,
set `PYTHON_EXECUTABLE`, or pass `-PythonExecutable`/`--python-executable`.

**The script cannot find `lansing_terminal.py`.** Run from the checked-out application
directory or provide `-TerminalPath`/`--terminal-path`.

**The board endpoint repeatedly opens and closes.** This is expected. Each attempt
uses a fresh terminal process, re-establishes a clean protocol boundary, and
detects before initialization.

**An actuator is `Ready` but continues initializing.** `Ready` means its delta
is within `0.1–3.0 mA`. The requested target may be lower than its present
delta, so the target workflow continues until the explicit target is reached.

**An actuator stops as `Stalled`.** Its delta failed to decrease by more than
the configured minimum. The script stops that actuator to avoid repeating a
conditioning sequence without measurable progress.

**An initially missing actuator does not make the run fail.** Initial `Not
connected` is treated as an unpopulated position and skipped. A transition to
`Not connected` after initialization is treated as a failure because the state
changed during active processing.

**The run can take a long time.** Each initialization is approximately two
minutes under default SDK timing. Reduce the actuator selection or maximum
attempt count for a narrower supervised run.

### Safe pulse sequence

```powershell
python lansing_terminal.py --port COM6 -c "psu on; psuc on; detect 0; set 0 255; set 0 0"
```

This example sends the off command immediately after the on command; for a
meaningful timed pulse, use interactive commands with deliberate timing or a
dedicated Python SDK script. Semicolon-separated terminal commands do not add a
delay between commands.

### Save diagnostic output

```powershell
python lansing_terminal.py -j --port COM6 -c "status; diagnose 0; states group 0" |
    Set-Content -Encoding utf8 lansing-diagnostic.ndjson
```

## Troubleshooting

### No board endpoints are listed

1. Confirm that the USB cable supports data, not power only.
2. Confirm that the controller is powered and connected.
3. Disconnect and reconnect the USB cable.
4. Check the operating system's device manager or serial-device list.
5. On Linux, confirm that the user has permission to access the device.
6. Try another USB port or cable.

### The selected port cannot be opened

Typical causes include:

- the port name is wrong or stale;
- the board was re-enumerated under a different port;
- another application already owns the port;
- the user lacks device permission; or
- the device was disconnected.

Run `ports` again, close other serial monitors and dashboard instances, then
retry `connect`. The terminal reports this condition without exiting an
interactive session.

### Connection opens but firmware validation fails

The selected device may not be a Lansing controller, firmware may not be
responding, or the serial stream may contain stale binary data. `connect`
attempts to force text mode before requesting the firmware version. If the
problem persists:

1. disconnect;
2. power-cycle or reboot the board;
3. wait for the serial device to reappear;
4. run `ports`; and
5. connect again.

### A command says to connect first

Board-independent commands include `help`, `ports`, `connect`, `disconnect`,
`square status`, `square stop`, `debug on`, `debug off`, `log show`, `log
clear`, `log save`, `exit`, and `quit`. Commands that read or change hardware
state require a connection. Run:

```text
ports
connect <port>
```

### `psuc on` fails

Turn the PSU on first:

```text
psu on
voltage
psuc on
```

### `set` says the actuator is `Unknown`

Normal actuator output is state-gated by the SDK. Detect it first:

```text
detect 0
states group 0
```

Proceed only if it reports `Ready`.

### Detection reports `Not connected`

The measured current delta was below `0.1 mA`. Keep the PSU connection off,
inspect the actuator and physical connection, confirm port numbering, and
detect again.

### Detection reports `Error`

The measured current delta exceeded `3.0 mA`. Do not use normal output. Run the
staged initialization workflow:

```text
init 0
```

If the final diagnosis remains in `Error`, inspect the physical setup and use
advanced recovery only under the appropriate bench procedure.

### `ACT_FAILED` occurs after commanding an actuator off

The actuator may still be in firmware-managed discharge. Wait for discharge to
complete before reactivating. The `square` runner handles this by watching
firmware discharge-completion messages.

### Status fails after a long diagnostic timeout

A late firmware response can leave serial command/response alignment unclear.
Disconnect and reconnect to restore a clean protocol boundary. If needed,
power-cycle the board and inspect SDK debug output:

```text
debug on
```

### JSON cannot be parsed as one document

The output is NDJSON: parse each line independently. Do not call a parser once
on the entire output as though it were a single array.

### JSON contains more than one record per command

This is expected. Commands can emit command echoes, progress, asynchronous log
messages, and final results. Select records by their `event` property or by the
detection-result field set documented above.

### Firmware debug messages are missing

SDK display and firmware generation are separate controls. A typical diagnostic
setup is:

```text
debug on
config set DEBUG ON
```

Restore the firmware setting afterward if continuous debug output is not
required:

```text
config set DEBUG OFF
debug off
```

## Related documentation

- [Lansing Terminal overview](../README.md)
- [Fluid Reality SDK overview](../../../README.md)
- [Python SDK API reference](../../../docs/api_reference.md)
- [Lansing Development Kit start-here guide](../../../docs/lansing_kit_start_here/README.md)
- [Lansing dashboard](../../lansing_dashboard/README.md)
