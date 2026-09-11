from __future__ import annotations

from apps.lansing_simulator.simulator import LansingTcpServer, SimulatedActuator, SimulatorConfig
from fluid_reality import ActuatorState, Rockford


def _config() -> SimulatorConfig:
    return SimulatorConfig(
        name="Rockford test",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=0.2,
        psu_base_current_noise_ma=0.0,
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
    with LansingTcpServer(_config(), port=0, board_type="rockford") as server:
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
    with LansingTcpServer(_config(), port=0, board_type="rockford") as server:
        with Rockford(server.endpoint, timeout=1.0) as board:
            board.power_supply(True)
            board.connect_power(True)
            result = board.detect_actuator_firmware(7)
            assert result.state is ActuatorState.NOT_CONNECTED
