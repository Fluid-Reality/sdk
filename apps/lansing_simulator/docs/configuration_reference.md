# Lansing Simulator Configuration Reference

The simulator reads one TOML document. Unknown sections and unknown fields are rejected so spelling mistakes do not silently change a test scenario. All relative file paths are resolved from the directory containing the configuration file.

## Root

| Field | Type | Default | Description |
|---|---|---:|---|
| `schema_version` | integer | `1` | Configuration schema. Version 1 is currently supported. |

## `[serial]`

This section reserves communication settings until a serial backend is selected.

| Field | Type | Default | Description |
|---|---|---:|---|
| `port` | string | empty | Future real or virtual serial endpoint. |
| `baudrate` | integer | `250000` | Lansing firmware serial rate. |

## `[simulation]`

| Field | Type | Default | Description |
|---|---|---:|---|
| `random_seed` | integer | `12345` | Seed controlling measurement noise and probabilistic faults. |
| `time_scale` | float | `1.0` | Simulated seconds per wall-clock second for the real-time clock. |
| `strict_firmware_0_1` | boolean | `true` | Preserve firmware 0.1 edge cases, including leaving bytes after the binary exit sentinel unread. |

Script mode uses a manual clock unless `--real-time` is supplied, so `time_scale` does not affect normal scripted runs.

## `[firmware]`

| Field | Type | Default | Description |
|---|---|---:|---|
| `name` | string | `Lansing` | `FW` returned by `VER`. |
| `version` | string | `0.1` | `VERSION` returned by `VER`. |
| `protocol` | string | `0.1` | `PROTO` returned by `VER`. |
| `actuator_count` | integer | `24` | Fixed at 24 for firmware 0.1 compatibility. |
| `max_active_ms` | integer | `5000` | Initial maximum continuous forward time. Persisted `CFG MAX` replaces it. |
| `discharge_ms` | integer | `2000` | Initial discharge limit. Persisted `CFG DIS` replaces it. |
| `persist_state` | boolean | `true` | Enable EEPROM-like JSON state. |
| `state_file` | string | `lansing_simulator_state.json` | Persistent-state location. |

## `[power]`

| Field | Type | Default | Unit | Description |
|---|---|---:|---|---|
| `voltage_v` | float | `220.0` | V | Settled PSU feedback voltage while PSU is on. |
| `voltage_noise_stddev_v` | float | `0.25` | V | Gaussian voltage-noise standard deviation. |
| `voltage_when_off_v` | float | `0.0` | V | Voltage feedback while PSU is off. |
| `current_when_off_ma` | float | `0.0` | mA | Current feedback while PSU is off. |
| `startup_delay_ms` | integer | `0` | ms | Linear voltage ramp duration after `PSU ON`. |

## `[current]`

| Field | Type | Default | Description |
|---|---|---:|---|
| `board_baseline_ma` | float | `1.33` | PSU-on current before actuator contributions. |
| `board_noise_stddev_ma` | float | `0.02` | Gaussian board-current noise. |
| `adc_bits` | integer | `12` | ADC resolution. |
| `adc_reference_v` | float | `3.3` | ADC reference voltage. |
| `current_conversion_ma_per_v` | float | `20.0` | Firmware current conversion multiplier. |
| `voltage_conversion_v_per_v` | float | `137.5` | Firmware voltage-feedback conversion multiplier. |

## `[actuator_defaults]`

These values apply to every actuator without an override.

| Field | Type | Default | Description |
|---|---|---:|---|
| `connected` | boolean | `true` | Whether the actuator contributes electrical current. `false` represents an open or absent actuator. |
| `forward_delta_ma_at_255` | float | `2.0` | Initial additional current at positive output 255. |
| `discharge_delta_ma_at_255` | float | `2.0` | Initial additional current at negative output 255. |
| `current_noise_stddev_ma` | float | `0.03` | Gaussian noise while that actuator is energized. |

## `[actuator_defaults.conditioning]`

| Field | Type | Default | Description |
|---|---|---:|---|
| `enabled` | boolean | `true` | Allow electrode activity to improve the simulated actuator. |
| `improvement_ma_per_dose_second` | float | `0.0025` | Delta reduction per second at output 255. Output values scale dose linearly. |
| `minimum_delta_ma` | float | `1.5` | Improvement plateau. Delta never decreases below this value. |

Set `enabled = false`, use a zero slope, or set a plateau above the application's target to exercise the “stopped improving” path.

## `[actuator_defaults.faults]`

| Field | Type | Default | Description |
|---|---|---:|---|
| `stuck_positive_output` | integer | `-1` | `-1` follows firmware output; `0..255` forces the physical positive electrode. |
| `stuck_negative_output` | integer | `-1` | `-1` follows firmware output; `0..255` forces the physical negative electrode. |
| `short_circuit_delta_ma_at_255` | float | `0.0` | Additional short-circuit current at effective output 255. |
| `intermittent_disconnect_probability` | float | `0.0` | Probability that an actuator is electrically absent during one 10 ms measurement slice. |

Firmware output queries report requested values. Stuck-output faults alter the electrical model beneath that cache, which lets tests reproduce a board that reports a command correctly while hardware behaves differently.

## `[actuators."N"]`

Override any actuator-default field for actuator `N`, where `N` is `0` through `23`. Nested conditioning and fault tables are independently inherited.

```toml
[actuators."7"]
connected = true
forward_delta_ma_at_255 = 5.4

[actuators."7".conditioning]
improvement_ma_per_dose_second = 0.01
minimum_delta_ma = 2.2

[actuators."7".faults]
intermittent_disconnect_probability = 0.02
```

## `[logging]`

| Field | Type | Default | Description |
|---|---|---:|---|
| `enabled` | boolean | `true` | Write JSONL records to a file. |
| `file` | string | `logs/lansing_simulator.jsonl` | Log file location. |
| `console` | boolean | `false` | Also write JSONL to stderr. |
| `include_raw_bytes` | boolean | `true` | Include hexadecimal RX/TX payloads. |
| `max_bytes` | integer | `5000000` | Rotate before the next event would exceed this size. Zero disables size rotation. |
| `backup_count` | integer | `3` | Number of rotated files retained. |

Each session receives a UUID and SHA-256 configuration hash. Logs use UTC wall time plus simulated monotonic milliseconds.

## `[faults]`

These faults apply to complete protocol responses.

| Field | Type | Default | Description |
|---|---|---:|---|
| `response_delay_ms` | integer | `0` | Delay before returning a generated group of lines. |
| `drop_response_probability` | float | `0.0` | Probability of discarding an entire generated response. |
| `corrupt_response_probability` | float | `0.0` | Probability of flipping one bit in the first response byte. |

All probabilities are in the inclusive range `0.0..1.0`.
