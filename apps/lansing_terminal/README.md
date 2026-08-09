# Lansing Terminal

Command-line operator interface for the Fluid Reality Lansing Development Kit.

Use the terminal when a graphical desktop is unavailable, from an SSH session,
or when a scriptable interface is preferable to the Lansing Dashboard. It
supports interactive commands, fail-fast command sequences, and
newline-delimited JSON for automation.

For installation details, safety guidance, the complete command reference,
JSON schemas, automation behavior, exit codes, and troubleshooting, see the
[Lansing Terminal Operator and Command Reference](docs/lansing_terminal_manual.md).

## Features

- List physical serial ports and configured virtual-port aliases.
- Control the high-voltage power supply and PSU connection independently.
- Read voltage, current, configuration, status, and runtime counters.
- Detect individual actuators or eight-actuator groups.
- Show `Unknown`, `Ready`, `Error`, and `Not connected` actuator states.
- Diagnose, initialize, and recover actuators.
- Run adaptive Fast Init with a configurable current-delta target below the
  `3.0 mA` Error threshold.
- Control normal actuator output through SDK and firmware safety checks.
- Perform advanced positive/negative manual-output bench tests.
- Run continuous square-wave tests with firmware discharge confirmation.
- Capture SDK and terminal event logs.
- Emit human-readable text or newline-delimited JSON.
- Initialize actuators to a target current delta using PowerShell, POSIX shell,
  or Windows batch automation.

## Install

Install the checked-out SDK in editable mode before the app requirements.

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

## Quick Start

List board endpoints without opening a board:

```bash
python lansing_terminal.py -c "ports"
```

Start an interactive session and connect from the terminal:

```text
python lansing_terminal.py
lansing(disconnected)> ports
lansing(disconnected)> connect <serial-port-or-tcp-endpoint>
lansing> psu on
lansing> voltage
lansing> psuc on
lansing> current
lansing> detect
lansing> init 0
lansing> fast_init 0 2.0
```

Use `init <actuator>` for the staged SDK initialization workflow. Use
`fast_init <actuator> [target_ma]` for adaptive Fast Init.
Use bare `detect` to detect the standard group 0 actuator range, `0-7`.

Connect during startup:

```powershell
python lansing_terminal.py --port COM6
```

To expose a TCP simulator as a selectable port alias, set
`FLUID_REALITY_VIRTUAL_PORTS` before starting the terminal:

```powershell
$env:FLUID_REALITY_VIRTUAL_PORTS="COM66=tcp://127.0.0.1:8765"
python lansing_terminal.py -c "ports"
```

Selecting `COM66` connects to TCP; other COM ports remain physical. Multiple
aliases may be separated by semicolons. A TCP endpoint can also be passed directly:

```powershell
python lansing_terminal.py --port tcp://127.0.0.1:8765
```

Run a fail-fast command sequence:

```powershell
python lansing_terminal.py --port COM6 `
    -c "psu on; psuc on; detect; fast_init 0 2.0; diagnose 0"
```

Emit newline-delimited JSON:

```powershell
python lansing_terminal.py -j --port COM6 -c "status; diagnose 0"
```

## Target-Current Automation

The terminal includes equivalent target-current initialization workflows for
PowerShell, Linux/macOS, and Windows Command Prompt. Each workflow detects
actuators, initializes while current delta improves, stops at the requested
target or on a stall, enforces an attempt limit, and shuts power down during
cleanup by default.

PowerShell:

```powershell
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5
```

Linux or macOS:

```bash
./initialize_all.sh --port /dev/ttyACM0 --target-delta-ma 1.5
```

Windows Command Prompt:

```bat
initialize_all.bat --port COM6 --target-delta-ma 1.5
```

See the [automation reference](docs/lansing_terminal_manual.md#initialize-actuators-to-a-target-current-delta)
for platform setup, every option, stop conditions, exit codes, and safety
behavior.

## Documentation

- [Lansing Terminal Operator and Command Reference](docs/lansing_terminal_manual.md)
- [Fluid Reality SDK overview](../../README.md)
- [Python SDK API reference](../../docs/api_reference.md)
- [Lansing Development Kit start-here guide](../../docs/lansing_kit_start_here/README.md)
- [Lansing Dashboard](../lansing_dashboard/README.md)
