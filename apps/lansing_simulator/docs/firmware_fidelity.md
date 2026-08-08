# Firmware 0.1 Fidelity Notes

This document defines which behavior is deliberately modeled from `lansing_firmware_0_1.ino`.

## Mode and parser behavior

- Serial rate is represented as 250000 baud in configuration.
- Startup emits `OK:READY`.
- Text commands are newline terminated; carriage returns are ignored.
- Commands are case-insensitive and exactly three characters.
- Space, tab, and comma are accepted separators.
- At most eight parameters are accepted, each no longer than 15 characters.
- The input line buffer is 96 bytes, with `ER:LINE_TOO_LONG` at the firmware boundary.
- Binary packets contain actuator and value bytes.
- Byte 255 in the actuator position exits binary mode without a response.
- Invalid or unsafe binary actuator writes are ignored.

## Stateful behavior

- Normal writes require PSU on and PSC on.
- Connecting PSC while PSU is off is rejected.
- Turning PSU off does not implicitly clear PSC, matching firmware 0.1.
- Repeated nonzero actuator writes do not reset the continuous-active timer.
- Disabling a forward-active actuator commits runtime and starts reverse discharge.
- Discharge output is positive 0, negative 255.
- Discharge duration is the lesser of active duration and `CFG DIS`.
- The normal path rejects writes while an actuator is discharging.
- Manual `OUT` cancels the normal state without committing its current active interval.
- Manual `OUT` is blocked while `SAFE` is on and does not require PSU or PSC.
- Firmware `INI` resets runtime and then accumulates runtime during its own ten pulses.
- `DIA` disables every actuator before measuring baseline, forward, and discharge current.

## Persistence

Simulator JSON persistence represents the firmware's EEPROM behavior. Atomic replacement prevents partially written state files. Runtime and MAX/DIS configuration survive reboot. SAFE and DEBUG do not.

## Known firmware/SDK edge case preserved

Firmware exits binary mode immediately after consuming byte 255. Any following byte remains unread and is subsequently handled by the text parser. The existing SDK sends `[255, 0]`; therefore the zero byte can become the first byte of the next text line. Strict mode preserves this firmware behavior so integration testing can expose it.

## Intentional simulation abstractions

- Analog averaging uses 10 ms integration slices instead of one ADC read per millisecond. State transitions inside a measurement window remain represented.
- DAC routing and chip-select activity are represented as actuator electrode values rather than SPI bus transactions.
- EEPROM wear, ADC nonlinearity, temperature, and inter-actuator electrical crosstalk are not modeled in version 0.1.
- Debug output contains stable firmware-style categories but does not attempt to reproduce every low-level SPI debug line byte-for-byte.

These abstractions do not alter the normal SDK-visible command structure or board state transitions.
