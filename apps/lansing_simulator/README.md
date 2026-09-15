# Fluid Reality Simulator Designer

Configuration designer and raw-TCP software board simulator. Choose the Lansing
24-actuator profile or Rockford eight-actuator profile in the designer toolbar.
The Rockford profile models capabilities, `DT0`/`DT1`, `OUC`, network/AP,
Bluetooth configuration, framed firmware updates, and the Rockford 1.1 VT
configuration/status protocol. Its default is 10,000 V·s; setting `VT_LIMIT`
also sets the user-modified audit flag. USB-only factory reset is correctly
rejected because the simulator itself is a TCP endpoint.

## Run

```powershell
python -m pip install -e .
python -m pip install -r apps/lansing_simulator/requirements.txt
python apps/lansing_simulator/lansing_simulator.py
```

Run these commands from the repository root. The editable install ensures the
apps use this checkout rather than an older globally installed SDK.

Use **Save configuration** to write one self-contained Lansing board JSON file.
It includes the board values, actuator assignments, and every named actuator
configuration used by the board.

Reusable actuator configurations live in `standard_configs/`. The designer
loads that folder on startup. Saving an individual actuator defaults to that
folder. Every actuator configuration has a GUID; editing a configuration forks
it to a new GUID. When a board-embedded configuration and a standard
configuration share a GUID, the standard configuration takes precedence.

The designer opens the most recently used board on startup. If no recent board
exists, its name and path remain empty. **Run simulator** requires the current
design to be saved, starts the TCP simulator, and opens a separate resizable
log window. The log records received commands as `RX:`, transmitted responses
as `TX:`, and connected-actuator current state on each `CUR` command as `LOG:`.

## Board configuration format

Board files use schema version 3 and contain only enabled groups and populated
actuator ports. Group and port keys are numeric strings because JSON object keys
are strings. An assignment contains only its configuration GUID; connection is
board state and is not stored inside an actuator profile.

```json
{
  "schema_version": 3,
  "kind": "lansing-simulator-design",
  "board_type": "rockford",
  "groups": {
    "0": {
      "actuators": {
        "0": {"configuration_guid": "..."}
      }
    }
  },
  "actuator_configurations": []
}
```

The simulator rejects unsupported schemas, invalid group/port keys, missing
configuration GUIDs, and actuator profiles whose minimum running current is
greater than their maximum starting current.

## Run the device simulator

TCP mode exposes the selected firmware byte stream through the SDK's
`TcpDeviceListener`. It works on Windows, macOS, and Linux without virtual COM
drivers or PTYs. Start the simulator on the loopback interface:

```powershell
python apps/lansing_simulator/simulator.py apps/lansing_simulator/sample_configs/01_single_actuator.json --tcp 127.0.0.1:49765
```

To run the Rockford profile directly, add `--board rockford`.

Connect by passing the simulator endpoint directly to the appropriate board
class:

```python
from fluid_reality import Lansing

with Lansing("tcp://127.0.0.1:49765") as board:
    print(board.status())
```

TCP is deliberately bound to `127.0.0.1` in these examples. The raw connection
is not encrypted or authenticated and should not be exposed to an untrusted
network. The simulator initially accepts one controlling SDK client. When that
client disconnects, it returns to listening for the next connection.

The protocol uses newline-terminated ASCII commands and CRLF-terminated ASCII
responses. Binary stream mode uses two-byte actuator/value packets and has no
checksum. The simulator does not depend on modem-control signals or exact baud
timing.

Current is modeled as PSU base current plus each active actuator's activation
fraction times its evolving current, followed by configured noise. Forward
drive reduces actuator current by the configured per-volt, per-second running
rate down to the minimum. Offline and discharge time recover current at the
offline rate up to the maximum starting current. Text `ACT`, binary stream,
manual `OUT`, `DIA`, and `INI` activation paths are represented. The Rockford
profile also exposes its direct top/digital-bottom `OUC` path, two-stage
detection protocol, `VT>1` capability, and VT configuration/status fields. The
simulator models that protocol surface; physical 100 V/s forced-discharge
timing remains firmware behavior.

The simulator requires `fluid-reality>=0.2.4` for the complete Rockford
configuration snapshot, VT-budget support, and the raw TCP listener API. No
virtual COM port, PTY, kernel driver, or administrator access is required.

When a board JSON is used, runtime state is checkpointed into its
`simulation_state` section. PSU state, PSC state, actuator activation, manual
outputs, and evolving actuator currents resume after the simulator restarts.
