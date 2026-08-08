# Lansing Simulator

`lansing_simulator` is a standalone, firmware-level simulation of the Fluid Reality Lansing actuator controller. It is intended to let developers exercise Lansing applications before physical hardware is available.

The simulator does **not** import, patch, or modify the Fluid Reality SDK. Its public boundary is raw bytes: host bytes enter the simulator and firmware-compatible bytes come back. A real or virtual serial adapter can therefore be added later without changing the board model.

## Current Scope

The application currently provides:

- Lansing firmware 0.1 text command parsing and responses
- binary actuator-stream parsing
- 24 independently configured actuators
- PSU on/off and PSU output connection state
- actuator idle, forward-active, and discharge states
- maximum-active-time enforcement and discharge lockout
- firmware `INI` and `DIA` routines with simulated timing
- voltage and current measurements with deterministic noise and ADC quantization
- current conditioning with a configurable improvement slope and plateau
- EEPROM-like persistence of runtime, timing configuration, and conditioned current
- firmware `DBG:` messages plus separate structured simulator logging
- response latency, response loss, and corruption fault injection
- deterministic manual time for fast script execution and automated tests

Serial-port communication is intentionally not implemented yet. The `[serial]` configuration is reserved so that the communication decision can be made without redesigning the simulator.

## Firmware Compatibility

The following firmware commands are implemented:

| Command | Purpose |
|---|---|
| `VER` | Firmware and protocol identity |
| `PSU` | Read or change PSU state |
| `PSC` | Read or change PSU output connection |
| `VLT` | Read simulated voltage |
| `CUR` | Read simulated current |
| `ACT` | Safe actuator control |
| `OUT` | Direct positive/negative electrode output |
| `INI` | Firmware actuator initialization sequence |
| `DIA` | Baseline/forward/discharge diagnosis |
| `TIM` | Read runtime totals |
| `RST` | Reset all runtime totals |
| `RBT` | Simulate a firmware reboot |
| `CFG` | Read or write firmware configuration |
| `STS` | Return the seven-line status snapshot |
| `STR` | Enter binary actuator-stream mode |

Successful results use `OK:`, errors use the firmware's stable `ER:` codes, and simulated firmware diagnostics use `DBG:`. The simulator's own operational log is never written into the protocol stream.

## Architecture

```text
future serial adapter
        |
        v
raw byte protocol engine
        |
        +-- firmware command parser
        +-- actuator and PSU state machine
        +-- timing and discharge service
        +-- electrical/current model
        +-- conditioning model
        +-- persistent state
        +-- structured event log
```

The primary integration class is `LansingSimulator`:

```python
from lansing_simulator import LansingSimulator, load_config

simulator = LansingSimulator(load_config("config.example.toml"))

assert simulator.startup_bytes() == b"OK:READY\n"
reply = simulator.feed_bytes(b"VER\n")
print(reply.decode("ascii"))
```

`feed_bytes()` deliberately accepts arbitrary chunks. It preserves incomplete text lines, incomplete binary packets, line-size behavior, and the firmware's binary exit semantics.

## Running the Local Harness

No third-party runtime dependencies are required. Python 3.11 or newer is sufficient.

From this directory:

```powershell
python -m pip install -e .
python -m lansing_simulator --config config.example.toml
```

Example session:

```text
OK:READY
sim> VER
OK:FW>Lansing,VERSION>0.1,PROTO>0.1
sim> PSU ON
OK:PSU_ON
sim> PSC ON
OK:PSC_ON
sim> DIA 0
OK:ACT>0,BASE>1.33,FWD>6.47,DIS>6.43
```

The console is only an operator harness; it is not the proposed SDK communication mechanism.

### Scripted execution

```powershell
python -m lansing_simulator --config config.example.toml --script example_session.txt
```

Scripts use a manual clock by default, so long firmware operations complete immediately in wall-clock time while retaining correct simulated durations. Use `--real-time` to execute a script using the configured clock scale.

Local script directives:

- `.advance <seconds>` advances the manual clock and services automatic transitions.
- `.tick` services time-dependent transitions without advancing time.
- `.quit` or `.exit` stops processing.
- Blank lines and lines beginning with `#` are ignored.

## Configuration

Configuration uses standard TOML and requires no YAML dependency. Start with [config.example.toml](config.example.toml), then consult the [complete configuration reference](docs/configuration_reference.md).

Paths in the configuration are resolved relative to the configuration file, not the process working directory.

### Electrical quantities

`board_baseline_ma` represents the current with the PSU on and no connected actuator energized. An actuator's `forward_delta_ma_at_255` is its additional current at output 255.

For example:

```text
baseline current = 1.33 mA
actuator delta   = 5.14 mA
forward current  = 6.47 mA
```

This matches the SDK's use of `abs(forward - baseline)` for actuator classification.

Actuator current scales linearly with the DAC command. A disconnected actuator contributes no current. Positive output uses the forward delta; negative output uses the discharge delta.

Actuator-level faults can be placed below the firmware-visible output cache:

```toml
[actuators."5".faults]
stuck_positive_output = -1
stuck_negative_output = 255
short_circuit_delta_ma_at_255 = 8.0
intermittent_disconnect_probability = 0.05
```

`-1` means that a physical output follows its commanded value. A value from 0 through 255 forces that physical electrode output while `ACT`, `OUT`, and `STS` continue to report what the firmware requested. Short-circuit current is added in proportion to the largest effective electrode output. Intermittent disconnection is evaluated independently for each measurement slice.

### Noise and repeatability

Voltage, board-current, and per-actuator current noise are independent Gaussian terms. `random_seed` makes a run repeatable. Changing the seed produces another deterministic noise sequence.

Measurements are quantized using the configured ADC resolution, reference voltage, and firmware conversion multipliers before being formatted to two decimal places.

### Conditioning and improvement

Conditioning is based on actual electrode activity, so it works for both firmware `INI` and the alternating manual `OUT` waveform used by host applications.

One dose-second is one simulated second at DAC value 255. Lower DAC values accumulate dose proportionally. For example, two seconds at value 128 are approximately one dose-second.

```toml
[actuators."0".conditioning]
enabled = true
improvement_ma_per_dose_second = 0.01
minimum_delta_ma = 1.8
```

The forward and discharge deltas decrease by the configured slope until they reach `minimum_delta_ma`. Setting the slope to zero or setting `enabled = false` creates an actuator that stops improving. The minimum creates a deterministic plateau for testing “stops improving” behavior.

### Persistence

The simulator persists:

- total actuator runtime
- `CFG MAX`
- `CFG DIS`
- conditioned forward-current deltas
- conditioned discharge-current deltas

Like firmware, `SAFE` always returns to on and `DEBUG` returns to off after reboot. PSU, PSC, actuator output, active, and discharge states are volatile.

Set `persist_state = false` for isolated tests.

### Fault injection

The initial fault controls apply to complete simulator responses:

```toml
[faults]
response_delay_ms = 0
drop_response_probability = 0.0
corrupt_response_probability = 0.0
```

These and actuator-level open/disconnected, short-current, stuck-output, and intermittent-connection faults are deterministic for a given random seed. Scripted time-specific failure scenarios can be added later without changing the protocol engine.

## Logging

Simulator logging is structured JSON Lines. Every record includes:

- UTC timestamp
- simulated monotonic time
- session identifier
- event name
- event-specific fields

Recorded events include raw RX/TX traffic, parsed commands, power transitions, actuator output and state changes, runtime persistence, conditioning progress, measurements, diagnoses, initialization, and injected communication faults.

Logs rotate according to `max_bytes` and `backup_count`. They are written out-of-band and can never be mistaken for firmware responses.

Firmware debug mode remains separately available through:

```text
CFG DEBUG ON
```

Those `DBG:` lines intentionally travel through the emulated firmware connection.

## Testing

The test suite uses only Python's standard library:

```powershell
python -m unittest discover -s tests -v
```

Tests run with a manual clock, disabled persistence, and disabled logging. They do not access the SDK or any serial device.

## Deferred Communication Layer

No serial backend has been selected or installed. A later adapter only needs to:

1. Read bytes from its endpoint.
2. Pass those bytes to `LansingSimulator.feed_bytes()`.
3. Write the returned bytes to the endpoint.
4. Call `tick()` regularly so asynchronous debug events can be transmitted.

This supports a Windows virtual COM pair, Linux PTY pair, or physical serial bridge while keeping the protocol and board behavior identical.
