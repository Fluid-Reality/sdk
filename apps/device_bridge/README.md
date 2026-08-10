# Device Bridge

`Device Bridge` is a small terminal-only raw serial-to-TCP bridge. It can:

- trace exact TX/RX traffic while an SDK application controls a physical board;
- make a physical Lansing board available to an SDK application on another computer;
- capture binary-safe hexadecimal and ASCII logs for troubleshooting.

It does not interpret or modify the protocol.

## Run

From the SDK repository root:

```powershell
python -m pip install -e .
python -m pip install -r apps\device_bridge\requirements.txt
python apps\device_bridge\device_bridge.py --list-ports
python apps\device_bridge\device_bridge.py COM9 --trace both
```

The bridge prints the exact `FLUID_REALITY_VIRTUAL_PORTS` command to use before
starting the dashboard, terminal, tests, or another SDK application. Existing calls
such as `Lansing("COM66")` require no code changes.

Useful examples:

```powershell
# Save a full trace without printing every packet
python apps\device_bridge\device_bridge.py COM9 --log trace.log

# Append hex-only terminal and file traces, using a different alias and baud rate
python apps\device_bridge\device_bridge.py COM9 --baud 115200 --alias LAB_BOARD --trace hex --log trace.log --append

# Expose the board to another computer on a trusted LAN
python apps\device_bridge\device_bridge.py COM9 --tcp 0.0.0.0:8765 --trace both
```

For remote use, set the client computer's mapping to the bridge computer's reachable
address, for example:

```text
FLUID_REALITY_VIRTUAL_PORTS=REMOTE_BOARD=tcp://192.168.1.50:8765
```

Only one SDK client controls the serial device at a time. After disconnection, the
bridge waits for another client. TCP is unauthenticated and unencrypted; bind beyond
loopback only on a trusted network or through a secure tunnel/firewall.

Run `python apps/device_bridge/device_bridge.py --help` for serial framing, flow control,
timeouts, buffer size, logging, and output options.

Trace directions:

- `TX`: SDK/TCP client to physical serial device.
- `RX`: physical serial device to SDK/TCP client.

The alias is implemented by the Fluid Reality SDK. It is not a system-wide COM port
or PTY and is not visible to unrelated third-party serial applications.
