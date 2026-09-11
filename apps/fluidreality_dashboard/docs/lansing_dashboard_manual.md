# Fluid Reality Dashboard User Manual

Revision: September 11, 2026

## Purpose

The Fluid Reality Dashboard connects to Rockford and Lansing boards, displays
power and telemetry, detects actuators, runs conditioning and diagnostic
procedures, and exposes supported board configuration tools. The interface
adapts to the connected firmware; unsupported actions remain disabled.

The dashboard reads the firmware identity after connecting and adopts the
matching SDK hardware profile without reopening the transport. This ensures
that Rockford and Lansing receive only configuration commands supported by
their firmware.

## Install And Run

Use Python 3.10 or newer. From a cloned SDK checkout:

```powershell
cd C:\research\FluidReality\sdk
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install -r apps\fluidreality_dashboard\requirements.txt
python apps\fluidreality_dashboard\app.py
```

The dashboard requires `fluid-reality[bluetooth]>=0.2.3`. On macOS or Linux,
create the environment with `python3 -m venv .venv` and activate it with
`source .venv/bin/activate`.

## Connect To A Board

Click `Connect`, choose a transport, enter its settings, and click `OK`.

- Serial lists available USB ports.
- Network accepts a host, port, optional access token, and optional TLS server
  certificate verification.
- Bluetooth discovers nearby devices whose advertised names identify them as
  Fluid Reality boards. Pairing and access-token authentication are available
  when the firmware requires them.

Authentication errors are translated into actionable messages. A TCP timeout
means the selected host and port did not accept a connection; verify the
board's Wi-Fi address, subnet, TCP-server setting, and local network route.

Virtual simulator aliases can be added before launch:

```powershell
$env:FLUID_REALITY_VIRTUAL_PORTS="COM66=tcp://127.0.0.1:49765"
```

Multiple aliases are separated by semicolons.

## Main Window

The connection card shows the active endpoint and firmware identity. The Power
toggle controls the PSU and also controls PSC when the board exposes it.
Voltage and current cards show live telemetry.

Actuator cards show the current classification:

| State | Meaning |
|---|---|
| `N/A` | No detection result is available in this dashboard session. |
| `Ready` | The actuator was detected and passed its current check. |
| `Error` | The actuator current exceeded the configured error threshold. |
| `Not connected` | DT0 did not detect the minimum current delta. |
| `Active` | The actuator is being driven. |
| `Discharging` | The actuator is returning its accumulated drive balance. |

Only DT0 assigns `Not connected`. Once an actuator has been detected, later
diagnosis does not change it back to `Not connected`; run DT0 again to make a
new connection determination. The default detection-current delta is 0.10 mA.

## Process Windows

Initialize, Fast Init, Diagnose, Recover, and Square Wave use consistent Play,
Stop, and Save controls. While a process is running, only Stop is enabled.
Playing again after a stop clears the prior trace. Save writes the collected
samples to CSV.

Time plots show a rolling 30-second window. Voltage plots display positive
magnitude on a 0-250 V axis even when the board is driving in reverse. Current
plots always start at 0 mA and never show a negative axis. The red progress
marker follows the newest value, and the numeric label is drawn above the data
line so it remains readable.

### Initialize

Initialize measures one baseline and sends a 1 Hz bipolar sequence at ±25 V,
±50 V, ±100 V, and ±200 V for 30 seconds per level. A current measurement is
taken after every voltage change, but the plotted current delta uses only the
positive-output readings. Diagnosis runs automatically afterward.

### Fast Init

Fast Init alternates directly between positive and reverse output without 0 V
stops. It begins at full available voltage and adjusts by 5 V, 10 V, or 20 V
according to error from the requested current-delta target. The target must be
greater than zero and below 3.0 mA. It succeeds when full voltage is reached at
or below the target and otherwise stops after 60 seconds. Only positive-output
current deltas are plotted.

### Diagnose

Diagnose first warms the actuator with one second at full forward voltage and
one second at full reverse voltage, repeated three times. It then sweeps from
0 V to 200 V in 10 V increments and plots voltage on the horizontal axis and
current on the vertical axis. The current axis spans 0-5 mA.

Two configurable curves divide the plot into healthy green, caution orange,
and error red regions. The final assessment considers the complete trace:

- A trace that remains green reports that the actuator is in great shape.
- A caution or temporary error excursion with a non-red ending reports that
  the actuator is functional and recommends initialization.
- A red ending reports that the actuator is outside the acceptable range and
  recommends recovery.

The most recent current value and unit remain visible while the trace is being
drawn.

### Recover

Recover is available only for an actuator classified as `Error`. It runs
±25 V, ±50 V, ±100 V, and ±200 V. At each stage, the positive-output current
delta must remain at or below 90% of the diagnostic error curve for three
continuous seconds before recovery advances. Reverse-output current is neither
used for qualification nor plotted. A marker identifies every voltage
increase. Run Diagnose afterward to reclassify the actuator.

### Square Wave

Square Wave alternates one second at full forward voltage with one second at
full reverse voltage. The reverse phase is labeled `Discharging` and is shown
as full voltage magnitude in the plot. The process continues until Stop.
Voltage and positive-output current delta are plotted live and can be saved to
CSV.

## Board Tools

Buttons are enabled only when the connected firmware and transport support the
operation.

### Board Settings

Board Settings edits Lansing timing or Rockford's per-actuator VT budget,
manual-output safety, firmware logging, and supported detection thresholds.
Rockford settings load from one comprehensive `CFG` response, and Save writes
only values that changed.

Rockford displays its VT limit in V·s. The default is 10,000 V·s, equivalent to
200 V for 50 seconds. Changing it sets a permanent audit marker. Factory reset
restores the default budget but intentionally preserves that marker. An
incorrect VT limit can permanently damage actuators or board electronics, so
the dashboard requires explicit risk confirmation before a change.

### Bluetooth Config

Bluetooth Config enables Bluetooth, configures security, clears bonds, and
changes the advertised-name suffix. Firmware always prepends `FR-`; the user
edits only the portion after that prefix.

### Wi-Fi, Network, And Security

Wi-Fi Config selects Client or Access Point mode. Network Config selects DHCP
or static IPv4 settings and enables the TCP server. Security & Encryption
separately controls access-token authentication and TLS credentials.

When changing the network address, save the new address before disconnecting.
If access-token authentication is enabled, enter that token in the Connect
window. A board response stating authentication is required means the token
was omitted or access-token use was not enabled in the connection settings.

### Fluid Mesh

Fluid Mesh is enabled only when firmware reports the `MESH` capability.
Rockford currently reports no Fluid Mesh support.

### Board Terminal

Board Terminal uses the dashboard's active connection to send firmware text
commands and shows the raw `OK:` or `ER:` responses. Enter a command and press
Enter or click Send. The next command remains disabled until the response is
complete. Clear removes displayed terminal history without affecting the
board.

Automatic status polling is paused from the moment the terminal opens until it
closes. The dashboard therefore sends no background status commands to refresh
voltage or current while the terminal is in use. Polling resumes automatically
after the window closes.

### Update Firmware

Update Firmware requires `FWU>0` and supports USB, TCP, or TLS, not Bluetooth.
Choose the firmware `.bin`, click Update, and leave the board connected while
the image uploads and verifies. The board then reboots and the dashboard
reconnects automatically. During this known reboot, the dashboard does not
send legacy text-recovery bytes, preventing a delayed parser error from being
mistaken for the new firmware identity.

### Factory Reset

Factory Reset requires `FCR>0` and is available only over USB serial. The
confirmation warning lists the persistent settings that will be erased. After
confirmation, the board resets, reboots, and the dashboard reconnects over the
same serial port.

## Event Log And Troubleshooting

The Event Log records connections, detection, actuator operations, firmware
responses, and failures. Enable Verbose only when command-level diagnostics are
needed; Save Log exports the session.

| Symptom | Check |
|---|---|
| Serial port is missing | Refresh the list, reconnect USB, and check the driver. |
| TCP connection times out | Verify the board IP, subnet, port, TCP-server toggle, and PC route. |
| Authentication is required | Enable Use access token and enter the configured token. |
| Board Settings reports an unsupported key | Confirm firmware identity and use SDK/dashboard 0.2.3 or newer. |
| Actuator is `Not connected` | Inspect wiring, then rerun DT0. |
| Actuator is `Error` | Run Initialize and Diagnose; use Recover if it still ends in error. |
| A tool button is disabled | Connect through a supported transport and confirm the firmware advertises its capability. |

## Safe Shutdown

Stop any running process, turn Power off, wait for voltage to fall to a safe
level, and disconnect the dashboard before handling hardware. Only trained
operators should work with the high-voltage system.
