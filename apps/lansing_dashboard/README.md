# Lansing Dashboard

This command opens the
[Fluid Reality Dashboard](../fluidreality_dashboard/README.md) for existing
Lansing installations.

## Connections

- USB serial
- TCP with an optional access token
- TLS with certificate verification
- Bluetooth LE with discovery, optional pairing, and optional access-token authentication

For controls and operating instructions, see the
[Fluid Reality Dashboard README](../fluidreality_dashboard/README.md).

## Run

Install the application requirements, then launch the compatibility entrypoint:

```powershell
cd C:\research\FluidReality\sdk
py -m pip install -r apps\lansing_dashboard\requirements.txt
py apps\lansing_dashboard\app.py
```
