# Lansing Dashboard

Compatibility launcher for the universal
[Fluid Reality Dashboard](../fluidreality_dashboard/README.md). Existing Lansing
launch commands and shortcuts continue to work, while the application receives
the same board-independent UI and connection features as the canonical
dashboard.

## Connections

- USB serial
- TCP with an optional access token
- TLS with certificate verification
- Bluetooth LE with discovery, optional pairing, and optional access-token authentication

Actuator count, current-detection threshold, and optional PSU-connection
capabilities are read from the connected board. This allows the same dashboard
to adapt to Lansing, Rockford, and future boards implementing the shared SDK
protocol.

The compatibility launcher includes the canonical dashboard's current
play/stop/save actuator workflows, live voltage/current plots, capability-gated
Board Tools, firmware update, Bluetooth and Wi-Fi configuration, and USB-only
factory reset. Behavior is documented only in the canonical
[Fluid Reality Dashboard README](../fluidreality_dashboard/README.md) so the two
entry points cannot drift.

## Run

Install the application requirements, then launch the compatibility entrypoint:

```powershell
cd C:\research\FluidReality\sdk
py -m pip install -r apps\lansing_dashboard\requirements.txt
py apps\lansing_dashboard\app.py
```

The implementation lives in `apps/fluidreality_dashboard`; this launcher does
not maintain a separate copy, preventing the two dashboards from drifting.
