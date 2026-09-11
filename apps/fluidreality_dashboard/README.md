# Fluid Reality Dashboard

Desktop dashboard for Fluid Reality boards implementing the SDK `Board`
protocol. The UI adapts to capabilities reported by connected firmware, so
unsupported tools stay hidden or disabled.

After reading the firmware identity, the universal dashboard adopts the
matching Rockford or Lansing SDK profile while preserving the open transport.
This keeps board-specific configuration commands and capabilities correct even
when the connection began through the generic board wrapper.

For the complete operator guide, see the
[dashboard user manual](docs/lansing_dashboard_manual.md).

## Connections

The Connect window supports USB serial, authenticated TCP, TLS with server
certificate verification, and Bluetooth LE with discovery, optional pairing,
and optional access-token authentication. An access token is sent only when one
is entered. Authentication failures are translated into user-facing messages.

## Dashboard Layout

The header shows the Fluid Reality logo and the single title `Dashboard`.
Power, voltage, and current each occupy one metric card. The Power switch turns
the PSU on or off and also controls PSC when the board exposes a separate power
connection.

Actuator cards are grouped in sets of eight. The `STS` response defines the
actuator count. Selecting a card chooses the actuator used by the actuator
tools; selecting a different group runs detection when power is ready.

Board Tools contains Board Settings, Bluetooth Config, Wi-Fi Config, Network
Config, Security & Encryption, Fluid Mesh, Board Terminal, Update Firmware, and
Factory Reset. Each button follows the corresponding firmware capability.

## Detection And Actuator States

When power is on, the dashboard detects the visible actuator group. Firmware
with `DET>0` runs batch `DT0` using one shared baseline and then runs `DT1` only
for actuators found present. Results appear as they arrive.

- `Ready` means the actuator passed detection and can be driven.
- `Error` means current is above the configured DT1 error threshold.
- `Not connected` is assigned only by `DT0` when the minimum detection delta is
  not reached.
- `N/A` means no detection result is available in the dashboard session.

After detection, a later diagnosis does not change an actuator back to
`Not connected`; a new `DT0` is required. The default minimum detection delta
is `0.10 mA`.

## Actuator Tools

Initialize, Fast Init, Diagnose, Recover, and Square Wave use the same window
structure. Play, Stop, and Save controls sit above the plots. While a process
is running, only Stop is active. Starting again after a stop clears the old
plots. Save writes collected samples to CSV.

All time plots show only their rolling last 30 seconds. Current plots never
display a negative axis and place `0 mA` at the bottom. The red progress marker
follows the current sample value, and its label is drawn above plotted data.

### Initialize

Initialize measures one baseline and runs a 1 Hz bipolar sequence at `±25 V`,
`±50 V`, `±100 V`, and `±200 V`, spending 30 seconds at each level. Current is
measured after every voltage change, but current delta is recorded and plotted
only for positive-output samples. A diagnosis runs afterward.

### Fast Init

Fast Init alternates directly between positive and reverse output without 0 V
stops. It begins at full available voltage and adjusts by `5 V`, `10 V`, or
`20 V` according to error from the requested current-delta target. The target
must be greater than zero and below `3.0 mA`. It succeeds when full voltage is
reached at or below the target and otherwise stops after 60 seconds. Only
positive-output current deltas are plotted.

### Diagnose

Diagnose warms the actuator with full forward output for one second and full
reverse output for one second, repeated three times. It then sweeps `0-200 V`
in `10 V` increments and plots current against voltage on a square chart with a
`0-5 mA` current axis.

Two configurable curves divide the chart into healthy (green), caution
(orange), and error (red) regions. The final message uses the complete trace:

- always green: the actuator is in great shape
- any caution/error excursion with a non-red ending: the actuator is
  functional, but initialization is recommended
- a red ending: the actuator is outside the acceptable range and Recover is
  recommended

### Recover

Recover is enabled only for actuators in the `Error` state. It automatically
runs `±25 V`, `±50 V`, `±100 V`, and `±200 V`. Each stage remains active until
the positive-output current delta stays at or below 90% of that voltage's error
curve for three consecutive seconds. Reverse-output current is not used for
qualification or plotted. Markers identify every voltage increase. Run
Diagnose afterward to reclassify the actuator.

### Square Wave

Square Wave alternates one second at full forward voltage with one second at
full reverse voltage. The reverse phase is labeled `Discharging` and plotted as
reverse full voltage. It continues until Stop. Voltage and positive-output
current delta are plotted live and can be saved to CSV.

## Board Tools

- Board Settings edits Lansing timing or Rockford's per-actuator VT budget,
  plus safety, debug, and supported detection thresholds. Rockford shows the
  limit in V·s, its permanent user-modified audit state, and a hardware-damage
  warning before any change. Rockford settings are loaded from one complete
  configuration response, and Save sends only values that actually changed.
- Bluetooth Config enables Bluetooth, configures security, clears bonds, and
  changes only the suffix of the advertised name. Firmware always adds `FR-`.
- Wi-Fi Config selects Client or Access Point mode. Client mode scans and joins
  networks. Access Point mode configures SSID, password, and channel.
- Network Config selects DHCP or static IPv4 settings per interface. Access
  Point mode uses its own static address and subnet; its default address is
  `192.168.24.1`, and the board runs a DHCP server for clients.
- Security & Encryption separately enables access-token authentication and TLS.
  It installs or clears credentials and can create a self-signed certificate.
  Private-key creation uses a Save dialog and confirms before overwriting.
- Fluid Mesh is enabled only when firmware reports `MESH`; Rockford currently
  reports no Fluid Mesh support.
- Board Terminal sends text commands directly through the dashboard's active
  connection and displays the firmware's raw responses. Automatic status
  polling—and therefore background voltage and current queries—is paused for
  the entire time the terminal window is open, then resumes when it closes.
- Update Firmware is enabled only with `FWU>0` over USB, TCP, or TLS. It uploads
  a `.bin`, verifies SHA-256, reboots, and reconnects without sending legacy
  text-recovery bytes during the known reboot. Bluetooth is unsupported.
- Factory Reset is enabled only with `FCR>0` over USB. After confirmation it
  erases persistent configuration, reboots, and reconnects automatically.

## Run

From a cloned SDK checkout:

```powershell
cd C:\research\FluidReality\sdk\apps\fluidreality_dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ..\..
python -m pip install -r requirements.txt
python app.py
```

On macOS or Linux, create the environment with `python3 -m venv .venv` and
activate it with `source .venv/bin/activate`.
