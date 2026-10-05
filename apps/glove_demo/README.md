# Fluid Reality Glove Demo

A desktop demo for mapping five glove fingers to Fluid Reality actuators and
playing simple activation patterns. The Blueprint interface shows each finger's
actuator number and colors its fingertip red for positive output or blue for
negative output. Color intensity follows the output level.

![Glove Demo, disconnected](blueprint_preview.png)

The [color preview](blueprint_color_preview.png) uses simulated levels. Neither
preview connected to or powered a board.

## Requirements

- Python 3.10 or newer.
- A Rockford or Lansing controller connected by a USB serial port, with its
  supported power supply and actuators.
- The Python SDK and PySide6 dependencies in [requirements.txt](requirements.txt).

This demo's connection window accepts serial ports only. Network and Bluetooth
connections are not part of this app.

## Install and launch

From the SDK repository root on Windows:

```powershell
py -m pip install -e .
py -m pip install -r apps\glove_demo\requirements.txt
py apps\glove_demo\app.py
```

On macOS or Linux, use `python3` instead of `py` and forward slashes in paths.
Launching the window alone does not open a board or apply output.

## Connect and run

1. Click **Connect**, then select a detected serial port or enter one manually.
2. The app reads the firmware `VER` response to identify Lansing or Rockford.
   There is no board-type choice. An unknown identity stops the connection
   instead of guessing a profile.
3. On connection, the app checks actuator readiness once. This briefly powers
   the board for detection, then powers it off. It checks every channel on the
   identified board, so the first connection can take some time. Detection may
   energize actuators; keep the glove clear while connecting.
4. Assign an actuator number to each finger, choose a pattern, and click
   **Run Demo**. The default mapping is pinky `0`, ring `1`, middle `2`, index
   `3`, thumb `4`. Numbers may be repeated; two fingers mapped to the same
   actuator show the same output.
5. Click **Stop Demo** to end the pattern. Mappings, pattern, and connection
   controls are locked while the demo runs. The connection remains available
   for another run after output cleanup finishes.

The app uses the startup readiness result on later runs rather than repeating
actuator detection each time. If hardware changes after connecting, disconnect
and reconnect to detect it again.

## Patterns

| Pattern | Output sequence |
| --- | --- |
| Pulse | All mapped outputs ramp from `0` to `+255` over 0.5 s, hold `-255` for 0.25 s, then rest at `0` for 0.25 s. The ideal signed output-time balances each cycle. |
| Snap | All mapped outputs ramp from `+255` to `-255` over 1 s, then repeat. |
| Slow Wave | Each finger position receives `+255` for 1 s in pinky-to-thumb order; firmware discharge finishes before the next finger. |
| Fast Wave | The same sequence with a 0.25 s positive phase per finger, followed by discharge. |
| Square Wave | All mapped outputs alternate `+255` for 0.5 s and `-255` for 0.5 s. |

Red and blue show signed activation at the fingertip; a darker marker means a
lower magnitude. The wave modes use the board's reported output during
discharge when available. Bipolar outputs are commanded to the five physical
channels sequentially, so their transitions are approximately synchronized,
not simultaneous. Serial latency can affect timing.

## Safety and shutdown

- The app refuses to start a pattern if any mapped actuator was not `Ready` at
  connection time, or if the supply voltage is zero.
- Pulse, Snap, and Square Wave use manual positive/negative output. They
  temporarily disable firmware `SAFE` and restore its previous setting on
  cleanup. The app limits accumulated signed output-time and compensates on
  stop, but this is **not** a measurement of electrode charge or a guarantee
  of safe exposure. Supervise hardware use.
- Slow Wave and Fast Wave retain firmware safety and wait for firmware-managed
  discharge. Firmware limits may shorten a requested active interval.
- Stopping, disconnecting, or closing clears output and powers the board off.
  Closing the window waits for cleanup rather than abandoning a running worker.

## Troubleshooting

- **No serial port listed:** check the cable, driver, and operating-system port
  assignment. You can type a port name manually.
- **Port busy or connection failed:** close other software using that port and
  reconnect. The app will show the board error.
- **Unsupported board firmware:** the `VER` response must identify `Lansing` or
  `Rockford`. The app will not choose one based on the port name.
- **Supply voltage is 0 V:** check the controller's external power adapter.
- **Actuator not Ready:** inspect the glove connection, then disconnect and
  reconnect to run detection again.

## Files and tests

- [app.py](app.py): Qt window and serial-port connection dialog.
- [hand_view.py](hand_view.py): Blueprint hand rendering and fingertip colors.
- [worker.py](worker.py): firmware identification, one-time detection, pattern
  scheduling, and shutdown cleanup.
- [assets/fluid-reality-icon.png](assets/fluid-reality-icon.png): window icon,
  copied from this repository's Fluid Reality dashboard assets.

Run the hardware-free tests from the repository root:

```powershell
py -m pytest tests\test_glove_demo.py -q
```

The hand outline adapts [“Hand left.svg” by Cy21](https://commons.wikimedia.org/wiki/File:Hand_left.svg)
under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/). It was
mirrored, filled, recolored, and combined with fingertip markers.
