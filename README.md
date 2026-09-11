# Fluid Reality SDK

Python SDK for Fluid Reality Lansing, Rockford, and compatible future hardware.

The package name on PyPI is `fluid-reality`; the Python import package is
`fluid_reality`.

## Install

Use Python 3.10 or newer.

```bash
python -m pip install --upgrade pip
python -m pip install fluid-reality
```

For Bluetooth connectivity, install the optional Bleak dependency:

```bash
python -m pip install "fluid-reality[bluetooth]"
```

### Bluetooth

Rockford uses the same command protocol over USB, TCP/TLS, and Bluetooth LE:

```python
from fluid_reality import Rockford, discover_bluetooth_boards

devices = discover_bluetooth_boards(timeout=5)
with Rockford(devices[0].endpoint, network_token="optional-token") as board:
    print(board.firmware_version())
    print(board.status())
```

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

## Find the Serial Port

Connect the Lansing board over USB, then list the serial devices visible to
Python:

```bash
python -m serial.tools.list_ports
```

Use the device name shown by that command when creating `Lansing(...)`.
The exact name depends on the operating system:

- Windows usually reports names such as `COM4` or `COM16`.
- macOS usually reports names under `/dev/cu.*`, for example a USB modem port.
- Linux usually reports names under `/dev/tty*`, for example a USB ACM or USB
  serial device.

If more than one device is listed, unplug the board, run the command again,
then plug it back in and look for the new entry.

### Simulator port aliases

Existing applications can expose a Lansing TCP simulator under a selectable
port name without code changes. Set `FLUID_REALITY_VIRTUAL_PORTS` before starting
the application:

```powershell
$env:FLUID_REALITY_VIRTUAL_PORTS="COM66=tcp://127.0.0.1:49765"
```

Only `Lansing("COM66")` uses the mapped TCP endpoint. Selecting another COM
port opens that physical serial port normally. Separate multiple mappings with
semicolons. See
[apps/lansing_simulator/README.md](apps/lansing_simulator/README.md) for the
simulator command and platform-specific examples.

List physical serial ports together with configured aliases:

```python
from fluid_reality import list_ports

print(list_ports())
# Example: ["COM1", "COM2", "COM66"]
```

Every returned value can be passed directly to `Lansing(...)`.

Rockford firmware 1.0 can also expose the physical board directly over Wi-Fi.
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
dashboard TCP/TLS connections remain available, but Network Setup does not send
device-side `NET` commands.

New capability classes should inherit from `Board`, avoid duplicating board
state, and use `super()` in any constructor they add. This keeps the shared
`Board` base present only once in the method-resolution order.

To inspect exact TX/RX traffic or expose a physical board to another computer, run
the terminal-only [Device Bridge](apps/device_bridge/README.md). It maps
an SDK virtual alias to a physical serial port and can print or save binary-safe
hexadecimal and ASCII traces. Its client endpoint can use raw TCP or TLS and can
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

## Touch Validation Example

The maintained
[basic actuator example](examples/01_basic_actuator_current.py) connects to a
board, enables power, detects an actuator, runs a bounded pulse, measures
current, and shuts output and power down even if an error occurs.

```powershell
python examples\01_basic_actuator_current.py COM18 --actuator 0
python examples\01_basic_actuator_current.py COM5 --board lansing --actuator 0
```

The same example accepts `tcp://`, `tls://`, and `ble://` endpoints, access
tokens, TLS trust settings, or a YAML connection profile. Run it with `--help`
for all connection and pulse options.

## Core Concepts

`Rockford(endpoint)` and `Lansing(endpoint)` select the hardware profile. An
endpoint can be USB serial, TCP, TLS, or Bluetooth. Use boards as context
managers so the transport closes cleanly.

The power supply and PSU connection to the actuator path are separate:

- `board.power_supply(True)` turns on the high-voltage supply.
- `board.voltage()` reads the measured supply voltage. A powered Lansing kit is
  typically around 215-220 V.
- `board.connect_power(True)` turns on the PSU connection to the actuator path.
- `board.current()` reads the current drawn by the system in milliamps.

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
SDK state is `Ready`.

Actuators may need initialization after storage, shipping, or long periods
without use. If `detect()` returns `Error`, run `board.initialize(actuator)`.
Initialization drives the actuator through a staged recovery sequence and then
diagnoses it again. If initialization succeeds, the state changes to `Ready`.

## Discharge Behavior

Actuator output and discharge are also separate phases. When an actuator is
turned on with `board.set_actuator(actuator, value)`, it runs forward. When it
is turned off with `board.set_actuator(actuator, 0)`, the board does not simply
stop instantly. It automatically discharges the actuator by running it in the
opposite direction for the same amount of time it was driven forward, up to the
configured discharge limit.

This means an actuator that was active for 250 ms will discharge for about
250 ms after it is turned off. An actuator that was active for longer will also
discharge longer, but the Lansing firmware limits normal forward activation to
at most 5 seconds and limits discharge to at most 2 seconds. During discharge,
the actuator can still feel active or busy even though you already commanded it
off. That is expected behavior.

Wait for discharge to finish before starting the next pulse or interpreting the
actuator as idle. The SDK and firmware use this discharge phase to return the
actuator safely toward neutral.

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

The reusable [Network Configuration app](apps/network_config/README.md) can
configure any board class that inherits from `NetworkBoard`. It discovers the
board's network interfaces and shows only the relevant Wi-Fi and/or Ethernet
controls, with per-interface IPv4 settings, TCP binding, TLS, and access-token
management.

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

- [01_basic_actuator_current.py](examples/01_basic_actuator_current.py):
  power the board, connect the output, detect one actuator, pulse it, and read
  current.
- [02_initialize_and_diagnose.py](examples/02_initialize_and_diagnose.py):
  detect an actuator, initialize it when needed, and report diagnosis results.
- [03_stream_sine.py](examples/03_stream_sine.py):
  stream a sine waveform to one actuator.
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
