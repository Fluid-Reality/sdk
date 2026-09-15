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

## Connecting to the Controller

A new controller is available over USB serial by default. Connect by serial
first, then use the
[Dashboard](apps/fluidreality_dashboard/README.md#board-tools) to configure
Bluetooth, Wi-Fi, TCP, access-token authentication, or TLS. The SDK can then
connect by serial, TCP/TLS, or Bluetooth.

You can pass an endpoint directly to `Rockford(...)` or `Lansing(...)`, or save
the connection settings in a YAML connection file. A connection file keeps the
transport, address, and authentication settings together so applications and
examples can open the same controller consistently:

```python
from fluid_reality import Rockford

with Rockford.from_connection_file("rockford.connection.yaml") as board:
    print(board.status())
```

Use the board as a context manager, as shown above, so the connection closes
cleanly.

Every connection file starts with these fields:

```yaml
format: fluid-reality-connection
version: 1
transport: serial  # serial, tcp, tls, or bluetooth
```

The remaining fields depend on the transport. Connection files can contain
access tokens, so store and share them as private configuration. They never
contain TLS private keys.

### Serial

Serial is the only connection method available before the controller is
configured. Connect the controller to the computer over USB, then list the
serial devices visible to Python:

```bash
python -m serial.tools.list_ports
```

Typical device names are `COM4` on Windows, `/dev/cu.usbmodem...` on macOS, and
`/dev/ttyACM...` or `/dev/ttyUSB...` on Linux. If several devices are listed,
unplug the controller, run the command again, reconnect it, and look for the new
entry.

Connect directly:

```python
from fluid_reality import Rockford

with Rockford("COM18") as board:
    print(board.status())
```

A serial connection file uses `serial_port` and an optional `baudrate`. The
default baud rate is `250000`:

```yaml
format: fluid-reality-connection
version: 1
transport: serial
serial_port: COM18
baudrate: 250000
```

### TCP/TLS

Configure Wi-Fi, the TCP server, access-token authentication, and TLS from the
[Dashboard](apps/fluidreality_dashboard/README.md#board-tools) while connected
over USB serial. TCP sends unencrypted traffic. TLS encrypts the connection
and should verify the controller certificate.

Connect directly over TCP:

```python
from fluid_reality import Rockford

with Rockford(
    "tcp://192.168.1.64:49765",
    network_token="your-device-token",
) as board:
    print(board.network_status())
```

A TCP connection file uses `host`, `port`, and an optional `access_token`:

```yaml
format: fluid-reality-connection
version: 1
transport: tcp
host: 192.168.1.64
port: 49765
access_token: your-device-token
```

Connect directly over TLS with the public certificate created or installed in
the Dashboard:

```python
from fluid_reality import Rockford

with Rockford(
    "tls://192.168.1.64:49765",
    network_token="your-device-token",
    tls_ca_file="rockford-certificate.pem",
    tls_server_hostname="rockford.local",
) as board:
    print(board.network_status())
```

A TLS connection file adds a `tls` section. `certificate_file` may be absolute
or relative to the connection file. `server_hostname` is the name in the
certificate:

```yaml
format: fluid-reality-connection
version: 1
transport: tls
host: 192.168.1.64
port: 49765
access_token: your-device-token
tls:
  certificate_file: rockford-certificate.pem
  server_hostname: rockford.local
  verify_hostname: true
```

The `tls` section can use `certificate` instead of `certificate_file` to embed
the PEM certificate in the YAML file. Certificate and hostname verification are
enabled by default.

For local development, the
[Rockford Simulator](apps/rockford_simulator/README.md) listens at
`tcp://127.0.0.1:49765`. The [Device Bridge](apps/device_bridge/README.md) can
expose a serial controller over TCP or TLS and capture TX/RX traffic.

### Bluetooth

Enable and configure Bluetooth from the
[Dashboard](apps/fluidreality_dashboard/README.md#board-tools) while connected
over USB serial. Install the optional Bluetooth dependency:

```bash
python -m pip install "fluid-reality[bluetooth]"
```

Use [09_bluetooth_discovery.py](examples/09_bluetooth_discovery.py) to discover
nearby controllers and connect to one by its displayed index:

```bash
python examples\09_bluetooth_discovery.py
python examples\09_bluetooth_discovery.py --connect 0 --pair
```

Run the example with `--help` for discovery timeout and access-token options.

A Bluetooth connection file uses `device`, optional operating-system pairing,
and an optional access token:

```yaml
format: fluid-reality-connection
version: 1
transport: bluetooth
device: FR-Rockford-3D3731
pair: true
access_token: optional-token
```

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

## API Reference

For the complete customer development API reference, including all public
classes, methods, errors, debug output options, streaming helpers, and code
examples, see [docs/api_reference.md](docs/api_reference.md).

## Dashboard

The Fluid Reality Dashboard configures and operates Lansing and Rockford
controllers over USB serial, Bluetooth LE, TCP, or TLS. It provides power,
telemetry, detection, initialization, diagnosis, recovery, and square-wave
controls.

See [apps/fluidreality_dashboard/README.md](apps/fluidreality_dashboard/README.md)
for installation and usage instructions.

## Terminal

The Fluid Reality Terminal connects to Lansing and Rockford controllers,
controls power and actuator output, displays telemetry and configuration, and
runs detection, diagnosis, initialization, recovery, and square-wave tests. It
supports interactive use, command sequences, and newline-delimited JSON output
for automation.

The terminal directory also includes PowerShell, Linux/macOS shell, and Windows
batch workflows for detecting actuators and initializing them to a target
current delta while monitoring improvement and enforcing bounded stop
conditions.

See [apps/fluidreality_terminal/README.md](apps/fluidreality_terminal/README.md) for
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
