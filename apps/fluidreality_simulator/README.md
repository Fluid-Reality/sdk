# Fluid Reality Simulator

The Fluid Reality Simulator provides one configuration designer and raw TCP
simulator for Lansing and Rockford controllers. Use it for SDK and application
development when physical hardware is unavailable.

The selected board profile controls the simulated firmware identity, actuator
count, commands, and capabilities:

| Profile | Actuator channels | Behavior |
|---|---:|---|
| **Lansing** | 24, in three groups of eight | Lansing power, actuator, detection, initialization, diagnosis, manual-output, and streaming commands |
| **Rockford** | 8 | Rockford detection, output-current, network, Bluetooth, firmware-update, and voltage-time-budget commands |

## Run The Designer

From the SDK repository root:

```powershell
python -m pip install -e .
python -m pip install -r apps/fluidreality_simulator/requirements.txt
python apps/fluidreality_simulator/fluidreality_simulator.py
```

Choose **Lansing (24 actuators)** or **Rockford (8 actuators)** in the Board
menu. Assign actuator profiles to the required channels, save the board
configuration, then select **Run simulator**. The designer listens on
`tcp://127.0.0.1:49765` and opens a separate protocol log. The log records
received commands as `RX:`, responses as `TX:`, and current-model details as
`LOG:`.

Rockford uses group 0 only. Channels 0 through 4 represent its built-in ports;
channels 5 through 7 represent the optional three-actuator expansion card.

Reusable actuator profiles are stored in `standard_configs/`. Each profile has
a GUID. A standard profile takes precedence when a board-embedded profile has
the same GUID. Editing a profile creates a new GUID so the standard remains
unchanged.

The designer remembers the last board configuration it opened. **Run
simulator** requires a saved configuration because runtime state is
checkpointed into that JSON file.

## Run Without The Designer

Start either included sample directly:

```powershell
# Lansing
python apps/fluidreality_simulator/simulator.py apps/fluidreality_simulator/sample_configs/01_lansing_single_actuator.json

# Rockford
python apps/fluidreality_simulator/simulator.py apps/fluidreality_simulator/sample_configs/02_rockford_single_actuator.json
```

The simulator reads the profile from the configuration. Use `--board lansing`
or `--board rockford` to override it. Use `--tcp HOST:PORT` to change the
listener address.

Connect with the matching SDK board class:

```python
from fluid_reality import Lansing, Rockford

with Lansing("tcp://127.0.0.1:49765") as board:
    print(board.status())

# Use Rockford(...) when running a Rockford configuration.
```

The default listener accepts one controlling SDK client at a time and returns
to listening after that client disconnects. It binds to loopback so it is
available only on the local computer. The raw TCP protocol is not encrypted or
authenticated.

## Configuration File

New board files use schema version 3 and identify the selected profile with
`board_type`:

```json
{
  "schema_version": 3,
  "kind": "fluidreality-simulator-design",
  "board_type": "rockford",
  "name": "Rockford development board",
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

Group and port keys are numeric strings. Lansing accepts groups 0 through 2;
Rockford accepts group 0. Ports are numbered 0 through 7 in each group. Each
assignment refers to an actuator profile included in the file or available in
`standard_configs/`.

Configurations created by the former Lansing and Rockford simulator apps are
accepted and are converted to the unified format the next time they are saved.

## Simulation Model

Current is modeled as the board's base current plus the contribution from each
active actuator, followed by configured noise. Forward drive reduces an
actuator's modeled current toward its minimum. Idle and discharge time recover
it toward its maximum starting current.

The simulator models text actuator commands, binary streaming, manual output,
detection, diagnosis, initialization, and runtime state. The Rockford profile
also models its two-stage detection flow, output-current command, network and
Bluetooth settings, framed firmware updates, and voltage-time configuration.
Physical timing and electrical behavior remain approximations intended for
software testing.
