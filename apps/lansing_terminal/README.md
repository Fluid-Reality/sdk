# Lansing Terminal

Interactive command-line controller for the Fluid Reality Lansing board.

Use this app when a graphical desktop is not available, for example from an SSH
session, a lab machine without a display, or an automated setup bench where a
terminal workflow is preferred.

## Features

- List serial ports and connect to a Lansing board.
- Turn the high-voltage power supply on or off.
- Connect or open the actuator output path.
- Read voltage, current, timing configuration, safety, firmware debug, and
  actuator runtime.
- Detect one actuator or an eight-actuator group.
- Show actuator states: `Unknown`, `Ready`, `Error`, and `Not connected`.
- Diagnose and initialize actuators using the SDK stateful workflow.
- Run advanced recovery with configurable voltage and duration.
- Run an indefinite square wave until stopped:
  - 1 second full on at value `255`
  - command off for firmware-managed discharge
  - wait for firmware discharge debug confirmation before reactivating
- Save or print the terminal event log.
- Use `help` and `help <command>` for command-specific guidance.

## Install

macOS or Linux:

```bash
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk/apps/lansing_terminal
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Windows PowerShell:

```powershell
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk\apps\lansing_terminal
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

The terminal requirements install the published `fluid-reality` package from
PyPI.

## Quick Start

Start the app:

```bash
python app.py
```

Run commands non-interactively with `-c`. Separate commands with semicolons:

```bash
python app.py -c "ports"
python app.py --port <serial-port> -c "status; psu on; voltage; output on; current"
```

The app stops on the first command error, closes the board connection, and
returns a non-zero process exit code.

List ports:

```text
lansing(disconnected)> ports
```

Connect to the board. Use the serial port reported by `ports`, for example
`COM4`, `/dev/cu.usbmodem...`, or `/dev/ttyACM0`.

```text
lansing(disconnected)> connect <serial-port>
```

Enable the power supply and output connection:

```text
lansing> psu on
lansing> voltage
lansing> output on
lansing> current
```

Detect and use actuator 0:

```text
lansing> detect 0
lansing> initialize 0
lansing> set 0 255
lansing> set 0 0
```

Only run `initialize` when detection reports `Error` or when an actuator needs
conditioning after storage. `set` works only when the actuator state is
`Ready`.

## Common Commands

```text
help
help detect
ports
connect <serial-port>
disconnect
status
psu [on|off]
output [on|off]
voltage [measurement_ms]
current
config show
config get <MAX|DIS|SAFE|DEBUG>
config set <MAX|DIS|SAFE|DEBUG> <value>
safety [on|off]
detect <actuator>
detect group <0|1|2>
states [group <0|1|2>]
diagnose <actuator>
initialize <actuator>
recover <actuator> [voltage=50] [duration_s=60]
set <actuator> <value>
off <actuator>|all
square start <actuator> [actuator...]
square stop
square status
runtime [actuator]
reset_runtimes
manual get <actuator>
manual set <actuator> <positive> <negative>
debug on
debug off
debug file <path>
log show
log clear
log save <path>
reboot
exit
```

Use `help <command>` inside the app for details and safety context.
