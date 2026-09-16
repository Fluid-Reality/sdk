from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.fluidreality_simulator.simulator import (
    FluidRealityDeviceSimulator,
    FluidRealityTcpServer,
    SimulatedActuator,
    SimulatorConfig,
)
from fluid_reality import ActuatorState, Rockford


def _config() -> SimulatorConfig:
    return SimulatorConfig(
        name="Rockford test",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=0.2,
        psu_base_current_noise_ma=0.0,
        board_type="rockford",
        actuators={
            0: SimulatedActuator(
                guid="rockford-test-actuator",
                name="Test actuator",
                max_starting_current_ma=1.0,
                offline_current_increase_ma_s=0.0,
                running_current_decrease_ma_v_s=0.0,
                min_running_current_ma=0.3,
                current_noise_ma=0.0,
            )
        },
    )


def test_rockford_profile_exposes_current_protocol(tmp_path) -> None:
    with FluidRealityTcpServer(_config(), port=0) as server:
        with Rockford(server.endpoint, timeout=1.0) as board:
            version = board.firmware_version()
            assert version.firmware == "Rockford"
            assert board.actuator_count == 8

            capabilities = board.capabilities()
            assert capabilities["DET"] == "1"
            assert capabilities["OUC"] == "1"
            assert capabilities["NET"] == "1"
            assert capabilities["BLT"] == "1"
            assert capabilities["FWU"] == "1"
            assert capabilities["VT"] == "1"

            config = board.read_config()
            assert config.vt_limit_vs == 10_000
            assert config.vt_limit_modified is False
            assert board.vt_limit_vs(12_500) == 12_500
            assert board.read_config().vt_limit_modified is True

            status = board.status()
            assert status["config"]["vt_limit_vs"] == 12_500
            assert status["config"]["vt_limit_modified"] is True
            assert status["vt_balance_vms"] == (0,) * 8
            assert status["vt_balance_vs"] == (0.0,) * 8

            assert board.network_interfaces() == ("WIFI",)
            assert board.network_status("WIFI")["IF"] == "WIFI"
            assert board.set_wifi_mode("ap")["MODE"] == "ACCESS_POINT"
            assert board.configure_access_point_ipv4("192.168.24.1", "255.255.255.0")["IP"] == "192.168.24.1"

            assert board.bluetooth_status()["NAME"] == "FR-Rockford-Sim"
            assert board.set_bluetooth_name("Bench-1")["NAME"] == "FR-Bench-1"

            board.power_supply(True)
            board.connect_power(True)
            board.safety(False)
            assert board.manual_output_current(0, 64, 0, 10) > 0

            initial = board.detect_actuator_firmware(0)
            assert initial.state is ActuatorState.PRESENT
            conditioned = board.detect_actuator_firmware_conditioned(0)
            assert conditioned.state is ActuatorState.READY

            image = tmp_path / "firmware.bin"
            image.write_bytes(bytes(range(256)) * 9)
            result = board.update_firmware(image)
            assert result.size == image.stat().st_size


def test_rockford_profile_keeps_absent_actuator_not_connected() -> None:
    with FluidRealityTcpServer(_config(), port=0) as server:
        with Rockford(server.endpoint, timeout=1.0) as board:
            board.power_supply(True)
            board.connect_power(True)
            result = board.detect_actuator_firmware(7)
            assert result.state is ActuatorState.NOT_CONNECTED


def test_included_rockford_configuration_loads() -> None:
    config = SimulatorConfig.load(
        Path("apps/fluidreality_simulator/sample_configs/02_rockford_single_actuator.json")
    )

    assert config.name == "Single Actuator Rockford Board"
    assert tuple(config.actuators) == (0,)
    assert config.psu_voltage_v == 210.0


def test_simulator_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="board_type must be 'lansing' or 'rockford'"):
        FluidRealityDeviceSimulator(lambda _data: None, _config(), board_type="unknown")


def test_rockford_configuration_rejects_additional_groups(tmp_path) -> None:
    source = Path("apps/fluidreality_simulator/sample_configs/02_rockford_single_actuator.json")
    data = json.loads(source.read_text(encoding="utf-8"))
    data["groups"]["1"] = {"actuators": {}}
    invalid = tmp_path / "invalid-rockford.json"
    invalid.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="group key must be 0"):
        SimulatorConfig.load(invalid)
