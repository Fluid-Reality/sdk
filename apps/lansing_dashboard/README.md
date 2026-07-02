# Lansing Dashboard

Desktop dashboard for the Fluid Reality Lansing board.

The UI uses the Fluid Reality logo in `assets/fluid_reality_logo.png` and a palette aligned with the public site: white surfaces, black ink, Fluid red accents, and blue active-state highlights.

For the complete operator guide, see the
[Lansing Development Kit Dashboard User Manual](docs/lansing_dashboard_manual.md).

## Features

- Connect to a Lansing board over a serial COM port.
- View power supply state, output connection state, voltage, current, and timing config.
- View actuators by bank: group 0 shows `0-7`, group 1 shows `8-15`, and group 2 shows `16-23`. Most Lansing Development Kit setups use only one populated group with eight actuators, typically group 0.
- Click an actuator card to select it; initialize, diagnose, and square-wave actions apply to the selected actuator.
- Actuators show `N/A` until detected. A detected-good actuator shows `Ready`. Disconnect resets all actuator cards back to `N/A`.
- When PSU is on and output is connected, the selected group is auto-detected:
  - delta `< 0.1 mA` between baseline and forward current means not connected
  - delta `> 3.0 mA` means error; run `Initialize` first because it normally recovers the actuator by reducing excess current draw
  - otherwise the actuator is shown as `Ready` and available
- Connected actuators expose a configurable `Recover` action, including working and error-state actuators. Use recovery only if initialization does not clear the error. Recovery is for advanced users only: it temporarily disables manual-output safety, alternates raw output between positive and negative manual drive for the requested duration, reports the current delta every second, restores safety, and reports the final delta against baseline.
- Recovery voltage is scaled from the measured PSU voltage. For example, if the PSU reads `200 V` and recovery is set to `100 V`, the raw manual value is about `127/255` in both directions.
- Recovery defaults are `50 V` for `60 s`.
- Run full `Diagnose` again after recovery to reclassify the actuator; if the delta returns to the idle range, the card becomes available again.
- Turn the power supply on/off and connect/disconnect the output.
- Initialize or diagnose a target actuator.
- Run an indefinite square wave on one or more actuators until stopped:
  - 1 second full on at value `255`
  - command off, which triggers firmware-managed discharge
  - wait for firmware debug confirmation that discharge stopped before reactivating

## Run

Clone the SDK repository and enter the dashboard application folder.

macOS or Linux:

```bash
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk/apps/lansing_dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

Windows PowerShell:

```powershell
git clone https://github.com/Fluid-Reality/sdk.git
cd sdk\apps\lansing_dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

The dashboard requirements install the published `fluid-reality` package from
PyPI. The dashboard should use that installed package rather than an editable
SDK checkout.

## Notes

- Normal actuator output uses the SDK `ACT` path, so the firmware still enforces PSU state, connection state, runtime tracking, maximum active time, and discharge lockout.
- Square wave output continues until `Stop`, `All Off`, disconnect, or app close.
- Long `INI` and `DIA` operations run on a background worker thread so the UI remains responsive.
