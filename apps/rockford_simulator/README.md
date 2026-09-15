# Rockford Simulator

Self-contained configuration designer and raw-TCP software simulator for the
Fluid Reality Rockford controller. It models eight actuator channels, including
the five built-in ports and optional expansion channels 5 through 7.

The simulator implements the Rockford command protocol used by the Python SDK:
power and telemetry, actuator output, `DT0`/`DT1` detection, diagnosis,
initialization, manual output, binary streaming, network and Bluetooth settings,
firmware-update framing, and the firmware 1.1 voltage-time budget.

## Run the designer

From the SDK repository root:

```powershell
python -m pip install -e .
python -m pip install -r apps/rockford_simulator/requirements.txt
python apps/rockford_simulator/rockford_simulator.py
```

Use **Save configuration** before selecting **Run simulator**. The designer
starts the simulator on `tcp://127.0.0.1:49765` and opens a live protocol log.
The saved JSON contains the Rockford board settings, actuator assignments, and
the actuator profiles used by that board.

Reusable actuator profiles are stored in `standard_configs/`. The controller
has one group with channels 0 through 7. Channels 0 through 4 represent the
built-in ports; channels 5 through 7 represent ports on the optional expansion
card.

## Run without the designer

Start the included single-actuator configuration directly:

```powershell
python apps/rockford_simulator/simulator.py apps/rockford_simulator/sample_configs/01_single_actuator.json
```

Connect with the SDK in another terminal:

```python
from fluid_reality import Rockford

with Rockford("tcp://127.0.0.1:49765") as board:
    print(board.firmware_version())
    print(board.status())
```

The default listener is loopback-only. The protocol is unencrypted and
unauthenticated, so do not bind it to an untrusted network.

## Configuration format

Rockford simulator files use schema version 3:

```json
{
  "schema_version": 3,
  "kind": "rockford-simulator-design",
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

Runtime state is checkpointed in the configuration file. PSU state, power-path
state, actuator output, manual output, current evolution, and voltage-time
balances resume when the simulator restarts.
