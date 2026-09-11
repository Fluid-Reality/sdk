# Fluid Reality Network Configuration

Desktop configuration utility for every SDK board class that inherits from
`NetworkBoard`. It discovers the interfaces reported by the firmware and
contains no Rockford-specific behavior.

## Features

- Open a dedicated connection window for a local USB serial port, Bluetooth LE
  board, or authenticated TCP/TLS endpoint, then display firmware/network status.
- Support connection-only `NetworkBoard` profiles whose network is managed by a
  piggybacked host. For these boards, the app keeps TCP/TLS host, port, token,
  and encryption settings in the connection window and sends no `NET`
  configuration commands.
- Show Wi-Fi controls only for `WifiBoard` devices: enable/disable, select Client
  or Access Point mode, scan and join client networks, or configure the board's
  access-point network name, password, radio channel, static address, and subnet.
- Show Ethernet link, speed, duplex, and MAC controls only for `EthernetBoard`
  devices.
- Select DHCP or configure static IPv4 settings for a selected client-mode
  interface. In Access Point mode, configure the AP address and subnet directly;
  the default is `192.168.24.1/24`, and the board supplies addresses to clients
  with its DHCP server.
- Configure the hostname, TCP server port/state, and TCP interface binding.
- Enable or disable access-token authentication independently of the stored
  token. Read the token over a local connection, copy it, or generate a
  replacement that is applied only when settings are saved.
- Install a PEM TLS certificate and matching private key, including encrypted-key
  passwords, enable or disable TLS, and erase the stored credentials.
- Browse for a private key or create a new key file with a Save dialog, then
  create a self-signed TLS server certificate locally without OpenSSL. Existing
  destination files require overwrite confirmation.

Configuration over USB or Bluetooth keeps the control connection available when
Wi-Fi or TCP settings change. When connected over TCP/TLS, the app warns that
saving network settings will close the active connection. Board outputs must be
disabled before firmware network configuration commands are accepted.
TLS credentials and settings are stored in the board's nonvolatile memory. When
TLS is enabled, invalid or missing credentials keep the TCP server offline instead
of silently falling back to plaintext.

If TCP authentication is enabled and no valid token is supplied, the UI reports
that the board requires an access token instead of exposing the raw
`NET AUTH` firmware response.

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
