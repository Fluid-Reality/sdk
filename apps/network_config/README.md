# Fluid Reality Network Configuration

Desktop configuration utility for every SDK board class that inherits from
`NetworkBoard`. It discovers the interfaces reported by the firmware and
contains no Rockford-specific behavior.

## Features

- Open a dedicated connection window for a local USB serial port, Bluetooth LE
  board, or authenticated TCP/TLS endpoint, then display firmware/network status.
- Support connection-only `NetworkBoard` profiles whose network is managed by a
  piggybacked host. For these boards, the app keeps TCP/TLS host, port, token,
  and certificate settings in the connection window and sends no `NET`
  configuration commands.
- Show Wi-Fi controls only for `WifiBoard` devices: enable/disable, scan, join,
  hidden networks, disconnect, and forget credentials.
- Show Ethernet link, speed, duplex, and MAC controls only for `EthernetBoard`
  devices.
- Select DHCP or configure static IPv4 settings for a selected interface.
- Configure the hostname, TCP server port/state, and TCP interface binding.
- Read the authenticated TCP access token automatically after connection, or
  generate a replacement that is applied only when network settings are saved.
- Install a PEM TLS certificate and matching private key, including encrypted-key
  passwords, enable or disable TLS, and erase the stored credentials.
- Create a self-signed TLS server certificate and matching private-key PEM files
  locally, without requiring OpenSSL or another external utility.

Configuration over USB or Bluetooth keeps the control connection available when
Wi-Fi or TCP settings change. When connected over TCP/TLS, the app warns that
saving network settings will close the active connection. Board outputs must be
disabled before firmware network configuration commands are accepted.
TLS credentials and settings are stored in the board's nonvolatile memory. When
TLS is enabled, invalid or missing credentials keep the TCP server offline instead
of silently falling back to plaintext.

## Run on Windows

From the SDK repository:

```powershell
py -m pip install -e .
py -m pip install -r .\apps\network_config\requirements.txt
py .\apps\network_config\app.py
```

The standalone app defaults to `WifiBoard` for current Rockford compatibility.
Use plain `NetworkBoard` for a connection-only device, or supply configurable
capabilities when embedding the window:

```python
from PySide6.QtWidgets import QApplication
from fluid_reality import EthernetBoard, WifiBoard
from apps.network_config.app import NetworkConfigWindow

class MyNetworkBoard(WifiBoard, EthernetBoard):
    actuator_count = 16

app = QApplication([])
window = NetworkConfigWindow(MyNetworkBoard)
window.show()
app.exec()
```
