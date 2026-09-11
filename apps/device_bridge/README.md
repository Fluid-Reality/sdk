# Device Bridge

`Device Bridge` is a small terminal-only serial-to-TCP/TLS bridge. It can:

- trace exact TX/RX traffic while an SDK application controls a physical board;
- make a serial Fluid Reality board available to an SDK application on another
  computer;
- capture binary-safe hexadecimal and ASCII logs for troubleshooting;
- require the same `NET AUTH` token handshake used by Fluid Reality network boards;
- encrypt the client connection with a PEM TLS certificate and private key.

The bridge is transport-transparent: it does not translate Lansing commands,
Rockford commands, binary streaming, or firmware-update frames. It forwards
ordinary firmware traffic unchanged. It intercepts every
`NET` command and processes it locally, so a serial-only board behaves like a
network-capable board without receiving commands it does not implement.

The SDK-facing `DeviceBridgeBoard` profile inherits from
`ConfigurableNetworkBoard`. It reports `HOST` plus every active host adapter
that has a usable IPv4 address. Each adapter exposes its name, address, and MAC
through `NET IF`, and can be selected as the TCP/TLS listener's bind target.
The bridge supports local status, diagnostics, hostname, access-token, TCP
listener, and TLS credential commands.
Wi-Fi and host IP-address changes are rejected locally as unsupported or
externally managed and are never forwarded to serial.

## Run

From the SDK repository root:

```powershell
python -m pip install -e .
python -m pip install -r apps\device_bridge\requirements.txt
python apps\device_bridge\device_bridge.py --list-ports
python apps\device_bridge\device_bridge.py COM9 --trace both
```

Network settings persist by default in
`%USERPROFILE%\.fluidreality\device_bridge.json`. Use `--config FILE` to select
another location. Before every overwrite, the existing file is renamed with a
local date/time suffix such as
`device_bridge.20260907-153012-123456.json`; the new configuration is then
written atomically.

The bridge prints the exact `FLUID_REALITY_VIRTUAL_PORTS` command to use before
starting the dashboard, terminal, tests, or another SDK application. Existing
calls such as `Lansing("COM66")` or `Rockford("COM66")` require no code changes;
the client must still choose the correct hardware profile.

Useful examples:

```powershell
# Save a full trace without printing every packet
python apps\device_bridge\device_bridge.py COM9 --log trace.log

# Append hex-only terminal and file traces, using a different alias and baud rate
python apps\device_bridge\device_bridge.py COM9 --baud 115200 --alias LAB_BOARD --trace hex --log trace.log --append

# Expose the board to another computer on a trusted LAN
python apps\device_bridge\device_bridge.py COM9 --tcp 0.0.0.0:49765 --trace both

# Authenticated TCP
python apps\device_bridge\device_bridge.py COM9 --tcp 0.0.0.0:49765 --token bridge-secret

# Disable a token saved previously in the configuration file
python apps\device_bridge\device_bridge.py COM9 --no-token

# Authenticated and encrypted TLS
python apps\device_bridge\device_bridge.py COM9 --tcp 0.0.0.0:49765 `
  --token bridge-secret `
  --tls-cert bridge-cert.pem `
  --tls-key bridge-key.pem
```

For remote use, set the client computer's mapping to the bridge computer's reachable
address, for example:

```text
FLUID_REALITY_VIRTUAL_PORTS=REMOTE_BOARD=tls://192.168.1.50:49765
```

Only one SDK client controls the serial device at a time. After disconnection, the
bridge waits for another client. For a remote connection, use both `--token` and
`--tls-cert`/`--tls-key`. In the dashboard connection window, select TLS, enter the
bridge computer's hostname/IP and port, enter the same token, and select the server
certificate as the trust certificate. The certificate must contain the hostname or
IP used by the client, or the client must supply the matching certificate-name
override.

Raw unauthenticated TCP remains available when the security options are omitted for
backward-compatible loopback testing. The bridge consumes a configured `NET AUTH`
handshake itself and never forwards the token command to the serial firmware.

The bridge's own `NET` configuration describes the host listener, not the
attached board's Wi-Fi radio. Host IP assignment and Wi-Fi association remain
externally managed. Factory reset is not proxied as a bridge setting.

Python code that starts a bridge can open its locally configurable board proxy
over either protocol:

```python
from apps.device_bridge.bridge import DeviceBridge

with DeviceBridge("COM9", port=49765, network_token="bridge-secret") as bridge:
    board = bridge.open_board(timeout=2.0)
    try:
        print(board.firmware_version())
    finally:
        board.close()
```

When the bridge uses TLS, pass `tls_ca_file`, `tls_fingerprint`, and/or
`tls_server_hostname` to `open_board()` using the same trust settings accepted
by other SDK network connections.

`NET TCP SET` applies enabled state, port, and bind target atomically. Commands
that change the listener or TLS mode send their response on the current
connection, save and rotate the configuration, close that connection, and then
restart on the newly configured endpoint. TLS certificate and private-key data
uploaded through `NET TLS` are stored beside the selected JSON configuration.

Run `python apps/device_bridge/device_bridge.py --help` for serial framing, flow control,
timeouts, buffer size, logging, and output options.

Trace directions:

- `TX`: SDK/TCP client to physical serial device.
- `RX`: physical serial device to SDK/TCP client.

The alias is implemented by the Fluid Reality SDK. It is not a system-wide COM port
or PTY and is not visible to unrelated third-party serial applications.
