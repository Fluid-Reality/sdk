# Fluid Reality SDK

Python SDK for Fluid Reality Lansing Development Kit hardware.

The package name on PyPI is `fluid-reality`; the Python import package is
`fluid_reality`.

## Install

Use Python 3.10 or newer.

```bash
python -m pip install --upgrade pip
python -m pip install fluid-reality
```

## Find the Serial Port

Connect the Lansing board over USB, then list the serial devices visible to
Python:

```bash
python -m serial.tools.list_ports
```

Use the device name shown by that command when creating `Lansing(...)`.
The exact name depends on the operating system:

- Windows usually reports names such as `COM4` or `COM16`.
- macOS usually reports names under `/dev/cu.*`, for example a USB modem port.
- Linux usually reports names under `/dev/tty*`, for example a USB ACM or USB
  serial device.

If more than one device is listed, unplug the board, run the command again,
then plug it back in and look for the new entry.

## Touch Validation Example

This example powers the board, detects actuator `0`, initializes it if needed,
then asks the user to touch the actuator while it pulses once:

- full on for 250 ms
- off for 250 ms while the board discharges it in the opposite direction

Save this as `touch_validation.py`.

```python
import sys
import time

from fluid_reality import ActuatorState, Lansing


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python touch_validation.py <serial-port> <actuator>")
        print("Find the port with: python -m serial.tools.list_ports")
        raise SystemExit(2)

    port = sys.argv[1]
    actuator = int(sys.argv[2])

    with Lansing(port) as board:
        print("Connected.")

        board.power_supply(True)
        voltage = board.voltage()
        print(f"Power supply voltage: {voltage:.2f} V")

        board.connect_power(True)
        print(f"Idle current: {board.current():.2f} mA")

        state = board.detect(actuator)
        print(f"Actuator {actuator} state after detection: {state.value}")

        if state is ActuatorState.ERROR:
            print("Actuator needs initialization. This can take about two minutes.")
            state = board.initialize(actuator)
            print(f"Actuator {actuator} state after initialization: {state.value}")

        if state is not ActuatorState.READY:
            raise RuntimeError(
                f"Actuator {actuator} is {state.value}; it is not ready to drive."
            )

        input(f"Touch actuator {actuator}, then press Enter to run the touch validation.")

        print(f"Actuator {actuator} full on for 250 ms.")
        board.set_actuator(actuator, 255)
        time.sleep(0.250)

        print(f"Actuator {actuator} off for 250 ms while it discharges.")
        board.set_actuator(actuator, 0)
        time.sleep(0.250)

        board.all_actuators_off()
        print(f"Done. You should have felt actuator {actuator} during the pulse.")


if __name__ == "__main__":
    main()
```

Run it with the serial port you found earlier:

```bash
python touch_validation.py <serial-port> <actuator>
```

For example, replace `<serial-port>` with the port name reported on your
machine, such as a Windows `COM...` device, a macOS `/dev/cu...` device, or a
Linux `/dev/tty...` device. To validate actuator 0, pass `0` as the actuator
number.

## Core Concepts

`Lansing(port)` opens the board connection. Use it as a context manager so the
serial port closes cleanly when the script exits.

The power supply and output connection are separate:

- `board.power_supply(True)` turns on the high-voltage supply.
- `board.voltage()` reads the measured supply voltage. A powered Lansing kit is
  typically around 215-220 V.
- `board.connect_power(True)` connects the supply to the output path.
- `board.current()` reads the current drawn by the system in milliamps.

Actuators have SDK states:

- `Unknown`: the default state when the board object is created.
- `Ready`: the actuator has been detected and is safe to drive normally.
- `Not connected`: the SDK did not measure a meaningful current change.
- `Error`: the current delta is too high for normal operation. Run
  `board.initialize(actuator)` before trying to use the actuator. Initialization
  runs a staged recovery sequence and then diagnoses the actuator again. If it
  returns `Ready`, the actuator can be used normally. If it still returns
  `Error`, leave the actuator off, check the physical connection, and contact
  Fluid Reality support before continuing.

Before driving an actuator, call `board.detect(actuator)`. `set_actuator()` only
works when that actuator is `Ready`.

Actuators may need initialization after storage, shipping, or long periods
without use. If `detect()` returns `Error`, run `board.initialize(actuator)`.
Initialization drives the actuator through a staged recovery sequence and then
diagnoses it again. If initialization succeeds, the state changes to `Ready`.

## Discharge Behavior

Actuator output and discharge are also separate phases. When an actuator is
turned on with `board.set_actuator(actuator, value)`, it runs forward. When it
is turned off with `board.set_actuator(actuator, 0)`, the board does not simply
stop instantly. It automatically discharges the actuator by running it in the
opposite direction for the same amount of time it was driven forward, up to the
configured discharge limit.

This means an actuator that was active for 250 ms will discharge for about
250 ms after it is turned off. An actuator that was active for longer will also
discharge longer, but the Lansing firmware limits normal forward activation to
at most 5 seconds and limits discharge to at most 2 seconds. During discharge,
the actuator can still feel active or busy even though you already commanded it
off. That is expected behavior.

Wait for discharge to finish before starting the next pulse or interpreting the
actuator as idle. The SDK and firmware use this discharge phase to return the
actuator safely toward neutral.
