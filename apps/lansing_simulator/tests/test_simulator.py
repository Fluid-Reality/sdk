from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lansing_simulator.clock import ManualClock
from lansing_simulator.config import SimulatorConfig, load_config
from lansing_simulator.engine import LansingSimulator
from lansing_simulator.event_log import EventLogger, NullEventLogger


def test_config() -> SimulatorConfig:
    config = SimulatorConfig()
    config.firmware.persist_state = False
    config.logging.enabled = False
    config.logging.console = False
    config.power.voltage_noise_stddev_v = 0.0
    config.current.board_noise_stddev_ma = 0.0
    config.actuator_defaults.current_noise_stddev_ma = 0.0
    config.actuator_defaults.conditioning.enabled = False
    return config


def simulator(config: SimulatorConfig | None = None) -> tuple[LansingSimulator, ManualClock]:
    clock = ManualClock()
    sim = LansingSimulator(config or test_config(), clock=clock, logger=NullEventLogger())
    return sim, clock


class ProtocolTests(unittest.TestCase):
    def test_startup_and_version(self) -> None:
        sim, _ = simulator()
        self.assertEqual(sim.startup_bytes(), b"OK:READY\n")
        self.assertEqual(
            sim.execute_text("ver"),
            ("OK:FW>Lansing,VERSION>0.1,PROTO>0.1",),
        )

    def test_parser_and_errors(self) -> None:
        sim, _ = simulator()
        self.assertEqual(sim.execute_text("AB"), ("ER:BAD_COMMAND",))
        self.assertEqual(sim.execute_text("NOO"), ("ER:UNKNOWN_COMMAND>NOO",))
        self.assertEqual(sim.execute_text("ACT,0,999"), ("ER:ACT_VALUE",))
        self.assertIn("ER:LINE_TOO_LONG", sim.execute_text("X" * 96))

    def test_power_requirements_and_connection_state(self) -> None:
        sim, _ = simulator()
        self.assertEqual(sim.execute_text("PSC ON"), ("ER:PSC_PSU_OFF",))
        self.assertEqual(sim.execute_text("ACT 0 10"), ("ER:ACT_PSU_OFF",))
        self.assertEqual(sim.execute_text("PSU ON"), ("OK:PSU_ON",))
        self.assertEqual(sim.execute_text("PSC ON"), ("OK:PSC_ON",))
        self.assertEqual(sim.execute_text("PSU OFF"), ("OK:PSU_OFF",))
        self.assertEqual(sim.execute_text("PSC"), ("OK:ON",))

    def test_actuator_runtime_and_discharge(self) -> None:
        sim, clock = simulator()
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        self.assertEqual(sim.execute_text("ACT 3 200"), ("OK:ACT",))
        clock.advance(0.8)
        self.assertEqual(sim.execute_text("ACT 3 0"), ("OK:ACT",))
        self.assertEqual(sim.execute_text("ACT 3 200"), ("ER:ACT_FAILED",))
        self.assertEqual(sim.execute_text("TIM 3"), ("OK:3,800",))
        clock.advance(0.8)
        sim.tick()
        self.assertEqual(sim.execute_text("ACT 3 200"), ("OK:ACT",))

    def test_max_active_time_forces_discharge(self) -> None:
        config = test_config()
        config.firmware.max_active_ms = 100
        config.firmware.discharge_ms = 1000
        sim, clock = simulator(config)
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        sim.execute_text("ACT 0 255")
        clock.advance(0.11)
        sim.tick()
        self.assertEqual(int(sim.board.actuators[0].state), 2)
        self.assertEqual(sim.board.actuators[0].negative, 255)

    def test_manual_output_safety_and_cancel(self) -> None:
        sim, _ = simulator()
        self.assertEqual(sim.execute_text("OUT 0 255 0"), ("ER:OUT_SAFETY_ON",))
        self.assertEqual(sim.execute_text("CFG SAFE OFF"), ("OK:CFG_SAFE",))
        self.assertEqual(sim.execute_text("OUT 0 255 12"), ("OK:OUT",))
        self.assertEqual(sim.execute_text("OUT 0"), ("OK:ACT>0,POS>255,NEG>12",))

    def test_status_has_exactly_seven_result_lines(self) -> None:
        sim, _ = simulator()
        lines = sim.execute_text("STS")
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[0].startswith("OK:PSU>OFF,PSC>OFF,VLT>"))
        self.assertTrue(lines[1].startswith("OK:ACT_VALUES>"))
        self.assertTrue(lines[6].startswith("OK:DISCHARGE_MS_LEFT>"))

    def test_diagnosis_uses_configured_current_delta(self) -> None:
        config = test_config()
        config.current.board_baseline_ma = 1.33
        config.actuator_defaults.forward_delta_ma_at_255 = 5.14
        config.actuator_defaults.discharge_delta_ma_at_255 = 5.10
        sim, _ = simulator(config)
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        line = sim.execute_text("DIA 0")[0]
        fields = dict(item.split(">", 1) for item in line[3:].split(","))
        self.assertAlmostEqual(float(fields["BASE"]), 1.33, delta=0.02)
        self.assertAlmostEqual(float(fields["FWD"]) - float(fields["BASE"]), 5.14, delta=0.02)
        self.assertAlmostEqual(float(fields["DIS"]) - float(fields["BASE"]), 5.10, delta=0.02)

    def test_disconnected_actuator_has_no_delta(self) -> None:
        config = test_config()
        config.actuator_defaults.connected = False
        sim, _ = simulator(config)
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        line = sim.execute_text("DIA 0")[0]
        fields = dict(item.split(">", 1) for item in line[3:].split(","))
        self.assertAlmostEqual(float(fields["BASE"]), float(fields["FWD"]), places=2)

    def test_stuck_output_fault_affects_current_but_not_status_cache(self) -> None:
        config = test_config()
        config.actuator_defaults.faults.stuck_positive_output = 255
        config.actuator_defaults.forward_delta_ma_at_255 = 2.0
        sim, _ = simulator(config)
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        cached = sim.execute_text("OUT 0")[0]
        current = float(sim.execute_text("CUR")[0][3:])
        self.assertEqual(cached, "OK:ACT>0,POS>0,NEG>0")
        self.assertGreater(current, config.current.board_baseline_ma + 1.9)

    def test_intermittent_disconnect_fault_can_force_open_measurement(self) -> None:
        config = test_config()
        config.actuator_defaults.faults.intermittent_disconnect_probability = 1.0
        sim, _ = simulator(config)
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        sim.execute_text("ACT 0 255")
        current = float(sim.execute_text("CUR")[0][3:])
        self.assertAlmostEqual(current, config.current.board_baseline_ma, delta=0.02)

    def test_conditioning_reduces_delta_until_plateau(self) -> None:
        config = test_config()
        condition = config.actuator_defaults.conditioning
        condition.enabled = True
        condition.improvement_ma_per_dose_second = 0.1
        condition.minimum_delta_ma = 1.8
        config.actuator_defaults.forward_delta_ma_at_255 = 5.0
        sim, clock = simulator(config)
        sim.execute_text("CFG SAFE OFF")
        sim.execute_text("OUT 0 255 0")
        clock.advance(10)
        sim.execute_text("OUT 0 0 0")
        self.assertAlmostEqual(sim.board.actuators[0].forward_delta_ma, 4.0, places=3)
        sim.execute_text("OUT 0 0 255")
        clock.advance(100)
        sim.execute_text("OUT 0 0 0")
        self.assertEqual(sim.board.actuators[0].forward_delta_ma, 1.8)

    def test_firmware_initialization_timing_and_runtime(self) -> None:
        sim, clock = simulator()
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        start = clock.monotonic()
        self.assertEqual(sim.execute_text("INI 0"), ("OK:INI",))
        self.assertAlmostEqual(clock.monotonic() - start, 22.5, places=2)
        self.assertEqual(sim.board.actuators[0].total_runtime_ms, 7500)

    def test_binary_mode_and_firmware_exit_edge_case(self) -> None:
        sim, _ = simulator()
        sim.execute_text("PSU ON")
        sim.execute_text("PSC ON")
        self.assertEqual(sim.execute_text("STR"), ("OK:STR",))
        self.assertEqual(sim.feed_bytes(bytes([4, 99])), b"")
        self.assertEqual(sim.board.actuators[4].forward_value, 99)
        self.assertEqual(sim.feed_bytes(bytes([255, 0])), b"")
        self.assertEqual(sim.feed_bytes(b"\n"), b"ER:BAD_COMMAND\n")


class ConfigurationAndPersistenceTests(unittest.TestCase):
    def test_example_configuration_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config.example.toml")
        self.assertEqual(config.serial.baudrate, 250000)
        self.assertEqual(config.actuator(0).forward_delta_ma_at_255, 5.14)
        self.assertFalse(config.actuator(1).connected)

    def test_runtime_and_conditioned_current_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = test_config()
            config.firmware.persist_state = True
            config.firmware.state_file = str(Path(directory) / "state.json")
            first, clock = simulator(config)
            first.execute_text("PSU ON")
            first.execute_text("PSC ON")
            first.execute_text("ACT 0 255")
            clock.advance(0.25)
            first.execute_text("ACT 0 0")
            first.board.actuators[0].forward_delta_ma = 1.95
            first.board.persist()

            second, _ = simulator(config)
            self.assertEqual(second.board.actuators[0].total_runtime_ms, 250)
            self.assertEqual(second.board.actuators[0].forward_delta_ma, 1.95)

    def test_jsonl_log_is_out_of_band(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = test_config()
            config.logging.enabled = True
            config.logging.file = str(Path(directory) / "events.jsonl")
            logger = EventLogger(config)
            sim = LansingSimulator(config, clock=ManualClock(), logger=logger)
            response = sim.feed_bytes(b"VER\n")
            self.assertEqual(response, b"OK:FW>Lansing,VERSION>0.1,PROTO>0.1\n")
            records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            self.assertIn("protocol.rx", {record["event"] for record in records})
            self.assertIn("protocol.tx", {record["event"] for record in records})


if __name__ == "__main__":
    unittest.main()
