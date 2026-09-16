"""End-to-end tests for transparent SDK redirection to the Lansing TCP simulator."""

from __future__ import annotations

import socket
import time
import json

import pytest

from apps.fluidreality_simulator.simulator import (
    FluidRealityDeviceSimulator, FluidRealityTcpServer, SimulatedActuator, SimulatorConfig,
)
from fluid_reality import Lansing, TransportError


def _config() -> SimulatorConfig:
    return SimulatorConfig(
        name="TCP integration board",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={},
    )


def _actuator(
    *, current_ma: float = 4.0, noise_ma: float = 0.0,
    offline_increase: float = 0.0, running_decrease: float = 0.0,
    minimum_ma: float = 0.5,
) -> SimulatedActuator:
    return SimulatedActuator(
        guid="test-actuator",
        name="Test actuator",
        max_starting_current_ma=current_ma,
        offline_current_increase_ma_s=offline_increase,
        running_current_decrease_ma_v_s=running_decrease,
        min_running_current_ma=minimum_ma,
        current_noise_ma=noise_ma,
    )


@pytest.fixture
def tcp_server():
    with FluidRealityTcpServer(_config(), port=0) as server:
        yield server


def _connect(server: FluidRealityTcpServer, *, timeout: float = 0.5) -> socket.socket:
    connection = socket.create_connection(server.address, timeout=timeout)
    connection.settimeout(timeout)
    return connection


def _receive_lines(connection: socket.socket, count: int) -> list[bytes]:
    buffer = bytearray()
    while buffer.count(b"\n") < count:
        buffer.extend(connection.recv(4096))
    lines = bytes(buffer).splitlines(keepends=True)
    return lines[:count]


def test_sdk_lansing_connects_to_simulator_endpoint(tcp_server):

    with Lansing(tcp_server.endpoint, timeout=0.5) as board:
        version = board.firmware_version()
        assert version.firmware == "Lansing"
        assert version.version == "0.1"
        assert board.transport.redirected is True
        assert board.transport.port == tcp_server.endpoint
        assert board.transport.endpoint == tcp_server.endpoint


def test_fragmented_text_command(tcp_server):
    with _connect(tcp_server) as connection:
        for fragment in (b"V", b"E", b"R", b"\n"):
            connection.sendall(fragment)
        assert _receive_lines(connection, 1) == [b"OK:FW>Lansing,VERSION>0.1,PROTO>0.1\r\n"]


def test_multiple_commands_in_one_read_and_multiple_status_response_lines(tcp_server):
    with _connect(tcp_server) as connection:
        connection.sendall(b"VER\nCUR\n")
        lines = _receive_lines(connection, 2)
        assert lines[0].startswith(b"OK:FW>Lansing")
        assert lines[1] == b"OK:0.000\r\n"

        connection.sendall(b"STS\n")
        status_lines = _receive_lines(connection, 7)
        assert len(status_lines) == 7
        assert status_lines[0].startswith(b"OK:PSU>OFF,PSC>OFF")
        assert status_lines[-1].startswith(b"OK:DISCHARGE_MS_LEFT>")
        assert all(line.endswith(b"\r\n") for line in status_lines)


def test_fragmented_binary_stream_packet(tcp_server):
    with _connect(tcp_server) as connection:
        connection.sendall(b"STR\n")
        assert _receive_lines(connection, 1) == [b"OK:STR\r\n"]
        connection.sendall(bytes([7]))
        time.sleep(0.01)
        connection.sendall(bytes([0xE9]))
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if tcp_server.last_engine and tcp_server.last_engine.values[7] == 0xE9:
                break
            time.sleep(0.005)
        assert tcp_server.last_engine is not None
        assert tcp_server.last_engine.values[7] == 0xE9


def test_stream_mode_exit_restores_text_framing(tcp_server):
    with _connect(tcp_server) as connection:
        connection.sendall(b"STR\n")
        assert _receive_lines(connection, 1) == [b"OK:STR\r\n"]
        connection.sendall(bytes([255]))
        connection.sendall(bytes([0]))
        connection.sendall(b"VER\n")
        assert _receive_lines(connection, 1)[0].startswith(b"OK:FW>Lansing")


def test_sdk_read_timeout(tcp_server, monkeypatch):
    with Lansing(tcp_server.endpoint, timeout=0.03) as board:
        # A complete command is deliberately not sent, so the engine has no response.
        board.transport.write_bytes(b"VE")
        with pytest.raises(TransportError, match="Timed out"):
            board.transport.read_line()


def test_client_disconnect_does_not_terminate_server(tcp_server):
    connection = _connect(tcp_server)
    connection.sendall(b"VER\n")
    assert _receive_lines(connection, 1)[0].startswith(b"OK:FW>Lansing")
    connection.close()

    deadline = time.monotonic() + 0.5
    while tcp_server._connection is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert tcp_server._thread is not None and tcp_server._thread.is_alive()


def test_accepts_new_sdk_connection_after_disconnect(tcp_server, monkeypatch):
    with Lansing(tcp_server.endpoint, timeout=0.5) as first:
        assert first.version()["FW"] == "Lansing"

    with Lansing(tcp_server.endpoint, timeout=0.5) as second:
        assert second.current() == 0.0
        assert second.transport.redirected is True


def test_status_voltage_uses_configured_noise(monkeypatch):
    config = _config()
    config = SimulatorConfig(
        name=config.name,
        psu_voltage_v=200.0,
        psu_voltage_noise_v=2.0,
        psu_base_current_ma=config.psu_base_current_ma,
        psu_base_current_noise_ma=config.psu_base_current_noise_ma,
        actuators=config.actuators,
    )
    with FluidRealityTcpServer(config, port=0) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.psu_on()
            samples = [board.status()["voltage"] for _ in range(4)]

    assert all(sample != 200.0 for sample in samples)
    assert len(set(samples)) == len(samples)


def test_current_is_base_plus_activation_fraction_times_actuator_current(monkeypatch):
    config = _config()
    config = SimulatorConfig(
        name=config.name,
        psu_voltage_v=config.psu_voltage_v,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: _actuator(current_ma=4.0)},
    )
    with FluidRealityTcpServer(config, port=0) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.psu_on()
            board.psc_on()
            board.raw_command("ACT", 0, 128)
            measured_ma = board.current()

    assert measured_ma == pytest.approx(1.0 + (128 / 255.0) * 4.0, abs=0.001)


def test_actuator_current_improves_while_running_and_recovers_while_offline():
    profile = _actuator(
        current_ma=4.0,
        running_decrease=0.5,
        offline_increase=0.25,
        minimum_ma=1.0,
    )
    config = SimulatorConfig(
        name="Evolving current",
        psu_voltage_v=2.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: profile},
    )
    engine = FluidRealityDeviceSimulator(lambda _data: None, config)
    engine.psu = True
    engine.psc = True
    engine.values[0] = 255
    engine.manual[0] = (255, 0)
    engine._current_updated_at = 0.0

    engine._update_actuator_currents(now=1.0)
    assert engine._actuator_current_ma[0] == pytest.approx(3.0)

    engine.values[0] = 0
    engine.manual[0] = (0, 0)
    engine._update_actuator_currents(now=3.0)
    assert engine._actuator_current_ma[0] == pytest.approx(3.5)


def test_diagnosis_has_firmware_like_blocking_latency(monkeypatch):
    delay_s = 0.06
    with FluidRealityTcpServer(_config(), port=0, diagnosis_delay_s=delay_s) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.psu_on()
            board.psc_on()
            started = time.monotonic()
            diagnosis = board.diagnose_actuator(0)
            elapsed = time.monotonic() - started

    assert diagnosis.actuator == 0
    assert elapsed >= delay_s * 0.9


def test_every_text_command_and_binary_packet_is_logged(monkeypatch):
    messages: list[str] = []
    with FluidRealityTcpServer(_config(), port=0, log=messages.append) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.psu_on()
            board.psc_on()
            board.raw_command("ACT", 2, 100)
            board.raw_command("ACT", 2, 100)
            board.enter_stream_mode()
            board.stream_actuator(2, 220)
            board.exit_stream_mode()
            deadline = time.monotonic() + 0.5
            while "RX: [255, 0]" not in messages and time.monotonic() < deadline:
                time.sleep(0.005)

    assert messages.count("RX: ACT 2 100") == 2
    assert "RX: STR" in messages
    assert "RX: [2, 220]" in messages
    assert "RX: [255, 0]" in messages
    assert "TX: OK:ACT" in messages
    assert "TX: OK:STR" in messages


def test_manual_output_command_is_logged(monkeypatch):
    messages: list[str] = []
    with FluidRealityTcpServer(_config(), port=0, log=messages.append) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.safety(False)
            board.set_manual_output(4, 80, 20)

    assert "RX: OUT 4 80 20" in messages
    assert "TX: OK:OUT" in messages


def test_manual_output_contributes_to_current(monkeypatch):
    config = _config()
    config = SimulatorConfig(
        name=config.name,
        psu_voltage_v=config.psu_voltage_v,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: _actuator(current_ma=4.0)},
    )
    with FluidRealityTcpServer(config, port=0) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.psu_on()
            board.psc_on()
            board.safety(False)
            board.set_manual_output(0, 255, 0)
            measured_ma = board.current()
            forward_value = board.get_actuator(0)

    assert measured_ma == pytest.approx(5.0, abs=0.001)
    assert forward_value == 0  # OUT is raw drive; firmware ACT state stays idle.


def test_cur_logs_evolving_current_for_connected_actuators_only(monkeypatch):
    messages: list[str] = []
    config = SimulatorConfig(
        name="Current log",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: _actuator(current_ma=4.0), 7: _actuator(current_ma=2.5)},
    )
    with FluidRealityTcpServer(config, port=0, log=messages.append) as server:
        with Lansing(server.endpoint, timeout=0.5) as board:
            board.psu_on()
            board.current()

    assert "LOG: A0->4.000;A7->2.500" in messages
    assert not any("A1->" in message for message in messages if message.startswith("LOG:"))


def test_diagnosis_and_initialization_pulses_evolve_actuator_current():
    profile = _actuator(
        current_ma=4.0,
        running_decrease=0.1,
        offline_increase=0.0,
        minimum_ma=0.5,
    )
    config = SimulatorConfig(
        name="Internal pulse paths",
        psu_voltage_v=2.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: profile},
    )
    engine = FluidRealityDeviceSimulator(lambda _data: None, config, diagnosis_delay_s=0.0)
    engine.psu = True
    engine.psc = True

    assert engine._diagnose(["0"]).startswith("OK:ACT>0")
    assert engine._actuator_current_ma[0] == pytest.approx(3.8)

    assert engine._initialize(["0"]) == "OK:INI"
    assert engine._actuator_current_ma[0] == pytest.approx(2.3)


def test_reverse_output_draws_discharge_current_and_recovers_actuator(monkeypatch):
    config = SimulatorConfig(
        name="Discharge path",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: _actuator(current_ma=4.0, minimum_ma=0.5, offline_increase=0.25)},
    )
    engine = FluidRealityDeviceSimulator(lambda _data: None, config)
    engine.psu = True
    engine.psc = True
    engine._actuator_current_ma[0] = 2.0
    engine.manual[0] = (0, 255)
    engine._current_updated_at = 0.0

    engine._update_actuator_currents(now=2.0)

    assert engine._actuator_current_ma[0] == pytest.approx(2.5)
    assert engine._current_value() == pytest.approx(1.5)


def test_each_response_line_is_logged_as_tx():
    messages: list[str] = []
    with FluidRealityTcpServer(_config(), port=0, log=messages.append) as server:
        with _connect(server) as connection:
            connection.sendall(b"STS\nBAD\n")
            _receive_lines(connection, 8)

    status_responses = [message for message in messages if message.startswith("TX: OK:")]
    assert len(status_responses) == 7
    assert "TX: ER:UNKNOWN_COMMAND>BAD" in messages


def test_simulation_state_persists_across_server_restart(tmp_path, monkeypatch):
    state_path = tmp_path / "board.json"
    state_path.write_text(json.dumps({"name": "Persistent board"}), encoding="utf-8")
    config = SimulatorConfig(
        name="Persistent board",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: _actuator(current_ma=4.0)},
    )
    with FluidRealityTcpServer(config, port=0, state_path=state_path) as first_server:
        with Lansing(first_server.endpoint, timeout=0.5) as board:
            board.psu_on()
            board.psc_on()
            board.raw_command("ACT", 0, 128)

    saved = json.loads(state_path.read_text(encoding="utf-8"))["simulation_state"]
    assert saved["psu"] is True
    assert saved["psc"] is True
    assert saved["actuator_activation"]["0"]["value"] == 128
    assert saved["actuator_current_ma"]["0"] == pytest.approx(4.0)

    with FluidRealityTcpServer(config, port=0, state_path=state_path) as second_server:
        with Lansing(second_server.endpoint, timeout=0.5) as board:
            status = board.status()

    assert status["psu"] == "ON"
    assert status["psc"] == "ON"
    assert status["actuator_values"][0] == 128
    assert status["current"] == pytest.approx(1.0 + 4.0 * 128 / 255, abs=0.001)


def test_config_loader_enforces_schema_and_kind(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps({"schema_version": 2, "kind": "lansing-simulator-design"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version 3"):
        SimulatorConfig.load(path)

    path.write_text(
        json.dumps({"schema_version": 3, "kind": "different-design"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Configuration kind"):
        SimulatorConfig.load(path)


@pytest.mark.parametrize(
    "kind,board_type",
    [
        ("fluidreality-simulator-design", "lansing"),
        ("lansing-simulator-design", "lansing"),
        ("rockford-simulator-design", "rockford"),
    ],
)
def test_config_loader_accepts_unified_and_legacy_kinds(tmp_path, kind, board_type):
    path = tmp_path / "board.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "kind": kind,
                "board_type": board_type,
                "groups": {},
            }
        ),
        encoding="utf-8",
    )

    assert SimulatorConfig.load(path).board_type == board_type


def test_reboot_clears_all_activation_paths():
    config = SimulatorConfig(
        name="Reboot reset",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={0: _actuator()},
    )
    engine = FluidRealityDeviceSimulator(lambda _data: None, config)
    engine.psu = engine.psc = True
    engine.safe = False
    assert engine._output(["0", "255", "0"]) == "OK:OUT"

    assert engine._reboot([]) == "OK:RBT"

    assert engine.psu is False
    assert engine.psc is False
    assert engine.values[0] == 0
    assert engine.manual[0] == (0, 0)
    assert engine._active_since[0] is None
