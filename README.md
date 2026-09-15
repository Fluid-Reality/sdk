# Fluid Reality SDK

Python SDK for Fluid Reality Lansing, Rockford, and compatible future hardware.

The package name on PyPI is `fluid-reality`; the Python import package is
`fluid_reality`.

Hardware setup guides:

- [Rockford Development Kit Start Here](docs/rockford_kit_start_here/README.md)
- [Lansing Development Kit Start Here](docs/lansing_kit_start_here/README.md)

## Install

Use Python 3.10 or newer.

```bash
python -m pip install --upgrade pip
python -m pip install fluid-reality
```

## Find the Serial Port

Connect the controller to the computer over USB, then list the serial devices
visible to Python:

```bash
python -m serial.tools.list_ports
```

Use the device name shown by that command when creating `Rockford(...)` or
`Lansing(...)`. Rockford is the default profile in the included examples. The
exact device name depends on the operating system:

- Windows usually reports names such as `COM4` or `COM16`.
- macOS usually reports names under `/dev/cu.*`, for example a USB modem port.
- Linux usually reports names under `/dev/tty*`, for example a USB ACM or USB
  serial device.

If more than one device is listed, unplug the board, run the command again,
then plug it back in and look for the new entry.

## Touch Validation Example

The maintained
[basic actuator example](examples/01_basic_actuator_current.py) connects to a
board, enables power, waits for a valid supply-voltage reading, detects an
actuator, runs a bounded pulse, measures current, and shuts output and power
down even if an error occurs.

```powershell
python examples\01_basic_actuator_current.py COM18 --actuator 0
```

Run the example with `--help` to see all connection and pulse options.

## Core Concepts

`Rockford(endpoint)` and `Lansing(endpoint)` select the hardware profile. An
endpoint can be USB serial, TCP, TLS, or Bluetooth. Use boards as context
managers so the transport closes cleanly.

Rockford supports eight actuator channels, numbered `0` through `7`. The
standard Rockford controller has five built-in actuator ports for channels `0`
through `4`. The optional three-actuator expansion card connects to `EXT CONN`
and adds physical ports for channels `5` through `7`.

The SDK exposes the high-voltage supply and its connection to the actuator path
as separate controls:

- `board.power_supply(True)` turns on the high-voltage supply.
- `board.voltage()` reads the measured supply voltage. A powered Lansing kit is
  typically around 215-220 V.
- `board.connect_power(True)` turns on the PSU connection to the actuator path.
- `board.current()` reads the current drawn by the system in milliamps.

Rockford integrates the power supply, controller, and actuator driver in one
enclosure and has no separate external PSU-connection switch. The Rockford
firmware maps these SDK controls to its internal output path.

Actuators have SDK states:

- `Unknown`: the default state when the board object is created.
- `Ready`: the actuator has been detected and is safe to drive normally.
- `Present`: DT0 found a meaningful current change and DT1 has not completed.
- `Not connected`: DT0 did not measure the configured minimum current delta.
- `Error`: the current delta is too high for normal operation. Run
  `board.initialize(actuator)` before trying to use the actuator. Initialization
  runs a staged recovery sequence and then diagnoses the actuator again. If it
  returns `Ready`, the actuator can be used normally. If it still returns
  `Error`, leave the actuator off, check the physical connection, and contact
  Fluid Reality support before continuing.

Before driving an actuator, call `board.detect(actuator)`. Current Rockford
firmware performs DT0 and DT1 detection on the board. `Not connected` is only a
DT0 result; once an actuator is present, later diagnosis does not return it to
`Not connected` unless DT0 is run again. `set_actuator()` only works when the
SDK state is `Ready`. The default minimum DT0 detection delta is `0.05 mA`.

Actuators may need initialization after storage, shipping, or long periods
without use. If `detect()` returns `Error`, run `board.initialize(actuator)`.
Initialization drives the actuator through a staged recovery sequence and then
diagnoses it again. If initialization succeeds, the state changes to `Ready`.

## Discharge Behavior

Actuator output and discharge are separate phases. Rockford firmware
integrates each actuator's signed voltage-time exposure and maintains a 1:1
forward/reverse balance. Its default per-actuator budget is 10,000 V·s,
equivalent to 200 V for 50 seconds.

When Rockford receives a normal off command, it immediately applies full
reverse until the accumulated VT is cancelled. If an actuator exhausts its VT
budget while still active, firmware gently ramps from full forward to full
reverse at 100 V/s, includes the ramp in the VT calculation, then holds full
reverse until the balance reaches zero. There is no separate continuous
activation-time limit on Rockford. During discharge, an actuator can still feel
active or busy even after it was commanded off; that is expected.

Wait for discharge to finish before starting the next pulse or interpreting the
actuator as idle. The SDK and firmware use this discharge phase to return the
actuator safely toward neutral.

Read Rockford's VT settings with `board.read_config()` or
`board.vt_limit_vs()`. Setting `board.vt_limit_vs(value)` uses whole V·s and
permanently marks the board as user-modified. An incorrect limit can permanently
damage actuators or board electronics. Factory reset restores 10,000 V·s but
does not erase that audit marker. See `examples/13_vt_budget.py` for the guarded
configuration flow.

## Bluetooth

After confirming the controller works over USB serial, Rockford can use the same
command protocol over Bluetooth Low Energy:

Install the optional Bleak dependency:

```bash
python -m pip install "fluid-reality[bluetooth]"
```

Use [09_bluetooth_discovery.py](examples/09_bluetooth_discovery.py) to discover
nearby Fluid Reality controllers:

```bash
python examples\09_bluetooth_discovery.py
```

The program prints each controller with an index. Connect to one by passing its
index, and add `--pair` if operating-system pairing is required:

```bash
python examples\09_bluetooth_discovery.py --connect 0 --pair
```

Run the example with `--help` to see the discovery timeout and access-token
options.

Bluetooth connection files are also supported:

```yaml
format: fluid-reality-connection
version: 1
transport: bluetooth
device: Rockford-3D3731
pair: true
access_token: optional-token
```

Open one with `Rockford.from_connection_file("rockford-bluetooth.yaml")`.

## API Reference

For the complete customer development API reference, including all public
classes, methods, errors, debug output options, streaming helpers, and code
examples, see [docs/api_reference.md](docs/api_reference.md).

## Dashboard

The repository includes a universal desktop dashboard for boards implementing
the shared `Board` protocol. It connects over USB serial, Bluetooth LE, TCP, or
TLS and provides the supported power, telemetry, detection, initialization,
diagnosis, recovery, and square-wave controls.

See [apps/fluidreality_dashboard/README.md](apps/fluidreality_dashboard/README.md)
for installation and usage instructions. The historical
`apps/lansing_dashboard/app.py` command is retained as a compatibility launcher
for the same application.

## Network Configuration

Configure board networking from the
[Fluid Reality Dashboard](apps/fluidreality_dashboard/README.md). Its Board
Tools provide Wi-Fi mode and credentials, per-interface DHCP or static IPv4
settings, TCP binding, access-token management, and TLS provisioning. The
Dashboard discovers each board's supported network interfaces and shows the
controls that apply to that hardware.

## Advanced Connections

These topics build on the basic USB serial workflow and are intended for
simulation, remote connections, saved profiles, and transport development.

### Wi-Fi and saved connection profiles

Rockford firmware 1.1 can also expose the physical board directly over Wi-Fi.
Pass its TCP endpoint and the token retrieved locally with `NET KEY`:

```python
from fluid_reality import Rockford

board = Rockford("tcp://192.168.1.64:49765", network_token="your-device-token")
print(board.network_status())
```

Connections can also be stored in a validated YAML profile and opened directly:

```python
from fluid_reality import Rockford

board = Rockford.from_connection_file("rockford.connection.yaml")
```

The profile supports serial, Bluetooth LE, TCP, and TLS transports. A TLS
profile can embed the public server certificate so it remains portable, or
reference a certificate file relative to the YAML file. Bluetooth profiles can
request operating-system pairing. Profiles may contain an access token and
should therefore be stored and shared as private configuration. Private keys
are never part of a client connection profile.

### Network capability classes

`NetworkBoard` marks any board that can be reached over TCP/TLS, including a
board piggybacking a host whose network is not device-configurable.
`ConfigurableNetworkBoard` adds interface-neutral IP, TCP server,
authentication, diagnostics, and TLS provisioning. `WifiBoard` adds Wi-Fi
discovery and credentials, while `EthernetBoard` adds wired-link control. A
connection-only, Wi-Fi-only, Ethernet-only, or dual-interface board can
therefore expose exactly the features its hardware supports. Capability classes
are designed for cooperative multiple inheritance:

```python
from fluid_reality import BluetoothBoard, EthernetBoard, WifiBoard

class FutureBoard(WifiBoard, EthernetBoard, BluetoothBoard):
    actuator_count = 16
```

For an externally managed network, inherit only from `NetworkBoard`; SDK and
Dashboard TCP/TLS connections remain available, while device-side `NET`
configuration controls remain unavailable.

New capability classes should inherit from `Board`, avoid duplicating board
state, and use `super()` in any constructor they add. This keeps the shared
`Board` base present only once in the method-resolution order.

### Rockford simulator

The self-contained [Rockford Simulator](apps/rockford_simulator/README.md)
provides a graphical configuration designer and a raw-TCP Rockford device for
SDK development without physical hardware. Connect to its default endpoint with
`Rockford("tcp://127.0.0.1:49765")`.

### Device bridge

To inspect exact TX/RX traffic or expose a physical board to another computer, run
the terminal-only [Device Bridge](apps/device_bridge/README.md). It exposes a
physical serial board through a direct TCP or TLS endpoint and can print or save
binary-safe hexadecimal and ASCII traces. Its client endpoint can
require the SDK's network-token authentication handshake. Its SDK-facing
`DeviceBridgeBoard` inherits from `ConfigurableNetworkBoard`. It handles all
`NET` commands inside the bridge, exposes its host TCP/TLS listener as the
configurable interface, and never forwards `NET` traffic to the serial board.
Its configuration is persisted with timestamped backups.

### Developing simulated-device listeners

The SDK exposes a raw byte-stream listener for device simulators. It performs
no text decoding or message framing, so protocols can switch freely between
line commands and binary streaming:

```python
from fluid_reality import TcpDeviceListener

with TcpDeviceListener("127.0.0.1", 49765) as listener:
    while True:
        with listener.accept() as connection:
            while chunk := connection.read_bytes(4096):
                response = protocol.feed(chunk)
                if response:
                    connection.write_bytes(response)
```

`write_bytes()` uses `sendall()` semantics. Applications should keep protocol
buffering, command terminators, binary packet boundaries, and mode transitions
inside their protocol engine.

## Terminal

The repository includes a command-line terminal for connecting to a Lansing
board, controlling the power supply and PSU connection, viewing telemetry and
configuration, detecting and diagnosing actuators, running initialization and
recovery, controlling actuator output, and operating square-wave tests. It
supports interactive use, semicolon-separated command sequences, and
newline-delimited JSON output for automation.

The terminal directory also includes PowerShell, Linux/macOS shell, and Windows
batch workflows for detecting actuators and initializing them to a target
current delta while monitoring improvement and enforcing bounded stop
conditions.

See [apps/lansing_terminal/README.md](apps/lansing_terminal/README.md) for
installation, the complete command reference, JSON schemas, automation options,
and platform-specific usage instructions.

## Examples

Example scripts are available in [examples](examples). Board examples accept a
USB serial port, a `tcp://`, `tls://`, or `ble://` endpoint, or a YAML profile:

```powershell
python examples\05_status_snapshot.py COM18
python examples\05_status_snapshot.py tcp://192.168.24.1:49765 --access-token TOKEN
python examples\05_status_snapshot.py --connection-file board.connection.yaml
python examples\05_status_snapshot.py COM5 --board lansing
```

Rockford is the default hardware profile. Pass `--board lansing` for Lansing.
Use `python <example> --help` for each example's complete options.

The universal dashboard identifies connected firmware and adopts the matching
Rockford or Lansing SDK profile without reopening the transport. Rockford's
typed configuration snapshot includes its VT budget, audit state, safety and
debug flags, and detection thresholds reported by current firmware.

- [01_basic_actuator_current.py](examples/01_basic_actuator_current.py):
  power the board, connect the output, detect one actuator, pulse it, and read
  current.
- [02_initialize_and_diagnose.py](examples/02_initialize_and_diagnose.py):
  detect an actuator, initialize it when needed, and report diagnosis results.
- [03_stream_sine.py](examples/03_stream_sine.py):
  stream a sine waveform to one actuator with board-specific Lansing timing or
  guarded Rockford VT configuration.
- [04_debug_logging.py](examples/04_debug_logging.py):
  enable SDK and firmware debug output and save it to a log file.
- [05_status_snapshot.py](examples/05_status_snapshot.py):
  print firmware identity, capabilities, and a full status snapshot.
- [06_manual_output_bench_test.py](examples/06_manual_output_bench_test.py):
  run low-level output and timed-current bench commands using the board's
  electrical model.
- [07_error_handling.py](examples/07_error_handling.py):
  show how to catch SDK exceptions and print recovery guidance.
- [08_actuator_pulse_until_key.py](examples/08_actuator_pulse_until_key.py):
  repeatedly pulse one actuator until a key is pressed.
- [09_bluetooth_discovery.py](examples/09_bluetooth_discovery.py):
  discover Fluid Reality Bluetooth boards and optionally connect to one.
- [10_network_configuration.py](examples/10_network_configuration.py):
  inspect or update Wi-Fi mode, IP, hostname, and TCP settings.
- [11_firmware_update.py](examples/11_firmware_update.py):
  upload and verify a firmware image over USB, TCP, or TLS.
- [12_factory_reset.py](examples/12_factory_reset.py):
  factory-reset a Rockford board over USB with explicit confirmation.
- [13_vt_budget.py](examples/13_vt_budget.py):
  inspect Rockford's VT budget and require explicit risk confirmation before a
  persistent change.
