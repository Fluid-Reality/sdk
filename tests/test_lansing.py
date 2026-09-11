from __future__ import annotations

import logging

import pytest

from fluid_reality import ActuatorState, Diagnosis, FirmwareError, Lansing


class FakeTransport:
    def __init__(self, lines=None):
        self.lines = list(lines or [])
        self.writes = []
        self.closed = False

    def write_line(self, line: str) -> None:
        self.writes.append(line)

    def read_line(self) -> str:
        if not self.lines:
            raise AssertionError("No fake response queued")
        return self.lines.pop(0)

    def write_bytes(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


def test_lansing_reads_version_fields():
    transport = FakeTransport(["OK:FW>Lansing,VERSION>0.1,PROTO>0.1"])
    board = Lansing(transport=transport)

    assert board.version() == {"FW": "Lansing", "VERSION": "0.1", "PROTO": "0.1"}
    assert transport.writes == ["VER"]


def test_board_reads_capability_fields():
    transport = FakeTransport(["OK:USB>1,WIFI>1,FWU>0"])
    board = Lansing(transport=transport)

    assert board.capabilities() == {"USB": "1", "WIFI": "1", "FWU": "0"}
    assert transport.writes == ["CAP"]


def test_firmware_batch_detection_streams_each_actuator_result():
    class TwoActuatorLansing(Lansing):
        actuator_count = 2

    transport = FakeTransport(
        [
            "OK:BASE>0.10",
            "OK:ACT>0,BASE>0.10,FWD>0.11,DELTA>0.01,STATE>NOT_CONNECTED",
            "OK:ACT>1,BASE>0.10,FWD>0.45,DELTA>0.35,STATE>PRESENT",
            "OK:ACT>1,BASE>0.10,FWD>0.48,DELTA>0.38,STATE>READY",
        ]
    )
    board = TwoActuatorLansing(transport=transport)
    streamed = []

    detections = board.detect_all_firmware(streamed.append)

    assert transport.writes == ["DT0", "DT1 1"]
    assert [item.actuator for item in streamed] == [0, 1, 1]
    assert streamed[1].state is ActuatorState.PRESENT
    assert streamed[2].state is ActuatorState.READY
    assert detections[0].state is ActuatorState.NOT_CONNECTED
    assert detections[1].state is ActuatorState.READY
    assert board.last_detection(1) == detections[1]


def test_single_firmware_detection_uses_dt0_actuator_command():
    transport = FakeTransport(
        ["OK:ACT>3,BASE>0.10,FWD>11.20,DELTA>11.10,STATE>ERROR"]
    )
    board = Lansing(transport=transport)

    detection = board.detect_actuator_firmware(3)

    assert transport.writes == ["DT0 3"]
    assert detection.state is ActuatorState.ERROR


def test_conditioned_firmware_detection_uses_dt1_command():
    transport = FakeTransport(
        ["OK:ACT>3,BASE>0.10,FWD>0.50,DELTA>0.40,STATE>READY"]
    )
    board = Lansing(transport=transport)

    detection = board.detect_actuator_firmware_conditioned(3)

    assert transport.writes == ["DT1 3"]
    assert detection.state is ActuatorState.READY


def test_platform_diagnostic_lines_do_not_break_protocol_responses():
    diagnostic = "E (30797) wifi:sta is connecting, cannot set config"
    transport = FakeTransport(
        [diagnostic, "OK:FW>Lansing,VERSION>0.1,PROTO>0.1"]
    )
    board = Lansing(transport=transport)

    assert board.version()["FW"] == "Lansing"
    assert board.debug_lines == (diagnostic,)


def test_ready_banner_does_not_replace_first_command_response():
    transport = FakeTransport(
        ["OK:READY", "OK:FW>Lansing,VERSION>0.1,PROTO>0.1"]
    )
    ready_lines = []
    board = Lansing(transport=transport, debug_callback=ready_lines.append)

    assert board.version()["FW"] == "Lansing"
    assert ready_lines == ["OK:READY"]
    assert board.debug_lines == ("OK:READY",)


def test_lansing_default_timeout_allows_slow_board_operations(monkeypatch):
    created = {}

    class FakeSerialTransport(FakeTransport):
        def __init__(self, port, *, baudrate, timeout, **kwargs):
            super().__init__()
            created.update(
                {
                    "port": port,
                    "baudrate": baudrate,
                    "timeout": timeout,
                    "kwargs": kwargs,
                }
            )

    monkeypatch.setattr("fluid_reality.boards.board.SerialTransport", FakeSerialTransport)

    board = Lansing("COM16")

    assert isinstance(board.transport, FakeSerialTransport)
    assert created["port"] == "COM16"
    assert created["baudrate"] == 250000
    assert created["timeout"] == Lansing.default_timeout_s


def test_lansing_skips_debug_lines_before_result():
    transport = FakeTransport(["DBG:COMMAND>ACT,ACTUATOR>1,VALUE>200", "OK:ACT"])
    debug_lines = []
    board = Lansing(transport=transport, debug_callback=debug_lines.append)
    board._actuator_states[1] = ActuatorState.READY

    board.set_actuator(1, 200)

    assert transport.writes == ["ACT 1 200"]
    assert debug_lines == ["DBG:COMMAND>ACT,ACTUATOR>1,VALUE>200"]
    assert board.debug_lines == ("DBG:COMMAND>ACT,ACTUATOR>1,VALUE>200",)


def test_lansing_raises_firmware_errors():
    transport = FakeTransport(["ER:ACT_PSU_OFF"])
    board = Lansing(transport=transport)
    board._actuator_states[1] = ActuatorState.READY

    with pytest.raises(FirmwareError) as error:
        board.set_actuator(1, 200)

    assert error.value.code == "ACT_PSU_OFF"
    assert error.value.info is not None
    assert "PSU" in str(error.value)


def test_lansing_parses_named_error_payload():
    transport = FakeTransport(["ER:UNKNOWN_COMMAND>ABC"])
    board = Lansing(transport=transport)

    with pytest.raises(FirmwareError) as error:
        board.raw_command("ABC")

    assert error.value.code == "UNKNOWN_COMMAND"
    assert error.value.fields == {"UNKNOWN_COMMAND": "ABC"}


def test_lansing_status_reads_all_status_lines():
    transport = FakeTransport(
        [
            "OK:PSU>ON,PSC>ON,VLT>218.45,CUR>12.34,CFG_MAX>5000,CFG_DIS>2000,SAFE>ON,DEBUG>OFF,STREAM>TEXT",
            "OK:ACT_VALUES>0,0,180,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
            "OK:OUT_VALUES>A0P>0,A0N>0,A1P>0,A1N>0,A2P>180,A2N>76,A3P>0,A3N>0,A4P>0,A4N>0,A5P>0,A5N>0,A6P>0,A6N>0,A7P>0,A7N>0,A8P>0,A8N>0,A9P>0,A9N>0,A10P>0,A10N>0,A11P>0,A11N>0,A12P>0,A12N>0,A13P>0,A13N>0,A14P>0,A14N>0,A15P>0,A15N>0,A16P>0,A16N>0,A17P>0,A17N>0,A18P>0,A18N>0,A19P>0,A19N>0,A20P>0,A20N>0,A21P>0,A21N>0,A22P>0,A22N>0,A23P>0,A23N>0",
            "OK:ACT_STATES>0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
            "OK:ACTIVE_MS>0,0,124,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
            "OK:TOTAL_MS>0,5300,1200,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
            "OK:DISCHARGE_MS_LEFT>0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
        ]
    )
    board = Lansing(transport=transport)

    status = board.status()

    assert transport.writes == ["STS"]
    assert status["psu"] == "ON"
    assert status["voltage"] == 218.45
    assert status["actuator_values"][2] == 180
    assert status["manual_outputs"][2] == (180, 76)
    assert status["total_ms"][1] == 5300
    assert status["discharge_ms_left"][2] == 0


def test_lansing_stream_writes_binary_packets():
    transport = FakeTransport(["OK:STR"])
    board = Lansing(transport=transport)

    board.enter_stream_mode()
    board.stream_actuator(23, 255)
    board.exit_stream_mode()

    assert transport.writes == ["STR", bytes([23, 255]), bytes([255, 0])]


def test_lansing_reboot_command():
    transport = FakeTransport(["OK:RBT"])
    board = Lansing(transport=transport)

    board.reboot()

    assert transport.writes == ["RBT"]


def test_lansing_force_text_mode_sends_stream_exit_and_flush_newline():
    transport = FakeTransport()
    board = Lansing(transport=transport)

    assert board.force_text_mode() == ()
    assert transport.writes == [bytes([255, 0]), ""]


def test_lansing_stream_sine_sends_zero_to_trigger_discharge():
    transport = FakeTransport()
    board = Lansing(transport=transport)

    rate = board.stream_sine(0, duration_s=0.001, frequency_hz=1, update_hz=1000)

    assert rate > 0
    assert transport.writes[-1] == bytes([0, 0])


def test_lansing_stream_sine_reports_values_to_callback():
    transport = FakeTransport()
    board = Lansing(transport=transport)
    values = []

    board.stream_sine(
        2,
        duration_s=0.001,
        frequency_hz=1,
        update_hz=1000,
        value_callback=lambda actuator, value, elapsed_s: values.append(
            (actuator, value, elapsed_s)
        ),
    )

    assert values
    assert values[-1][0] == 2
    assert values[-1][1] == 0
    assert values[-1][2] >= 0


def test_lansing_stream_sine_default_minimum_avoids_zero_until_finish():
    transport = FakeTransport()
    board = Lansing(transport=transport)
    values = []

    board.stream_sine(
        0,
        duration_s=0.003,
        frequency_hz=1,
        update_hz=1000,
        value_callback=lambda actuator, value, elapsed_s: values.append(value),
    )

    assert values[-1] == 0
    assert all(value >= 1 for value in values[:-1])


def test_lansing_logs_debug_lines_when_requested(caplog):
    transport = FakeTransport(["DBG:COMMAND>CUR", "OK:12.34"])
    logger = logging.getLogger("fluid_reality.test")
    board = Lansing(transport=transport, debug_logger=logger, log_debug_messages=True)

    with caplog.at_level(logging.DEBUG, logger="fluid_reality.test"):
        assert board.current() == 12.34

    assert "DBG:COMMAND>CUR" in caplog.text


def test_lansing_debug_out_defaults_to_disabled(capsys):
    transport = FakeTransport(["OK:12.34"])
    board = Lansing(transport=transport)

    assert board.current() == 12.34

    assert capsys.readouterr().out == ""


def test_lansing_debug_out_can_write_to_print(capsys):
    transport = FakeTransport(["OK:12.34"])
    board = Lansing(transport=transport)
    board.set_debug_out(print)

    assert board.current() == 12.34

    output = capsys.readouterr().out
    assert "Lansing | current" in output
    assert "protocol | command.write" in output
    assert "CUR" in output


def test_lansing_debug_out_can_write_to_file(tmp_path):
    path = tmp_path / "board-debug.log"
    transport = FakeTransport(["OK:12.34"])
    board = Lansing(transport=transport)
    board.set_debug_out(path)

    assert board.current() == 12.34
    board.close()

    output = path.read_text(encoding="utf-8")
    assert "Lansing | current" in output
    assert "value_ma=12.34" in output


def test_lansing_debug_out_can_write_to_file_handler(tmp_path):
    path = tmp_path / "board-debug.log"
    transport = FakeTransport(["OK:12.34"])
    board = Lansing(transport=transport)

    with path.open("w", encoding="utf-8") as handle:
        board.set_debug_out(handle)
        assert board.current() == 12.34

    output = path.read_text(encoding="utf-8")
    assert "Lansing | current" in output
    assert "value_ma=12.34" in output


def test_lansing_debug_out_rejects_string_paths():
    board = Lansing(transport=FakeTransport())

    with pytest.raises(TypeError, match="Path"):
        board.set_debug_out("board-debug.log")


def test_lansing_debug_out_can_use_callback():
    lines = []
    transport = FakeTransport(["OK:12.34"])
    board = Lansing(transport=transport)
    board.set_debug_out(lines.append)

    assert board.current() == 12.34

    assert any("Lansing | current" in line for line in lines)
    assert any("protocol | response.ok" in line for line in lines)


def test_lansing_debug_out_can_be_disabled_after_enabled(capsys):
    transport = FakeTransport(["OK:12.34", "OK:1.23"])
    board = Lansing(transport=transport)
    board.set_debug_out(print)
    assert board.current() == 12.34
    board.set_debug_out(None)
    assert board.current() == 1.23

    output = capsys.readouterr().out
    assert "value_ma=12.34" in output
    assert "value_ma=1.23" not in output


def test_lansing_typed_config_helpers():
    transport = FakeTransport(
        [
            "OK:5000",
            "OK:CFG_MAX",
            "OK:2000",
            "OK:CFG_DIS",
            "OK:SAFE>ON",
            "OK:CFG_SAFE",
            "OK:DEBUG>OFF",
            "OK:CFG_DEBUG",
        ]
    )
    board = Lansing(transport=transport)

    assert board.max_active_time_ms() == 5000
    assert board.max_active_time_ms(6000) == 6000
    assert board.discharge_time_ms() == 2000
    assert board.discharge_time_ms(1500) == 1500
    assert board.safety() is True
    assert board.safety(False) is False
    assert board.firmware_debug() is False
    assert board.firmware_debug(True) is True

    assert transport.writes == [
        "CFG MAX",
        "CFG MAX 6000",
        "CFG DIS",
        "CFG DIS 1500",
        "CFG SAFE",
        "CFG SAFE OFF",
        "CFG DEBUG",
        "CFG DEBUG ON",
    ]


def test_detection_threshold_config_helpers():
    transport = FakeTransport(
        [
            "OK:DET_MIN>0.20",
            "OK:CFG_DET_MIN",
            "OK:DT0_ERR>10.00",
            "OK:CFG_DT0_ERR",
            "OK:DT1_ERR>3.00",
            "OK:CFG_DT1_ERR",
        ]
    )
    board = Lansing(transport=transport)

    assert board.detection_current_limit_ma() == pytest.approx(0.20)
    assert board.detection_current_limit_ma(0.34) == pytest.approx(0.34)
    assert board.dt0_error_threshold_ma() == pytest.approx(10.0)
    assert board.dt0_error_threshold_ma(12.0) == pytest.approx(12.0)
    assert board.dt1_error_threshold_ma() == pytest.approx(3.0)
    assert board.dt1_error_threshold_ma(4.0) == pytest.approx(4.0)
    assert transport.writes == [
        "CFG DET_MIN",
        "CFG DET_MIN 0.34",
        "CFG DT0_ERR",
        "CFG DT0_ERR 12.0",
        "CFG DT1_ERR",
        "CFG DT1_ERR 4.0",
    ]

def test_lansing_actuators_start_unknown():
    board = Lansing(transport=FakeTransport())

    assert board.actuator_state(0) is ActuatorState.UNKNOWN
    assert board.actuator_states == (ActuatorState.UNKNOWN,) * Lansing.actuator_count


def test_lansing_set_actuator_requires_ready_state():
    transport = FakeTransport()
    board = Lansing(transport=transport)

    with pytest.raises(RuntimeError, match="Unknown"):
        board.set_actuator(0, 255)

    assert transport.writes == []


def detection_responses(
    baseline_ma: float,
    initial_ma: float,
    conditioned_ma: float | None,
) -> list[str]:
    responses = ["OK:SAFE>ON", "OK:CFG_SAFE", *(["OK:OUT"] * Lansing.actuator_count)]
    responses.extend([f"OK:{baseline_ma}", "OK:OUT", f"OK:{initial_ma}"])
    if conditioned_ma is not None:
        responses.append(f"OK:{conditioned_ma}")
    responses.extend(["OK:OUT", "OK:CFG_SAFE"])
    return responses


def test_lansing_detect_marks_actuator_ready(monkeypatch):
    transport = FakeTransport(detection_responses(1.0, 9.0, 2.5))
    board = Lansing(transport=transport)
    sleeps = []
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", sleeps.append)

    assert board.detect(0) is ActuatorState.READY

    detection = board.last_detection(0)
    assert detection is not None
    assert detection.state is ActuatorState.READY
    assert detection.initial_forward_ma == 9.0
    assert detection.initial_delta_ma == 8.0
    assert detection.forward_ma == 2.5
    assert detection.delta_ma == 1.5
    assert detection.discharge_ma == 0.0
    assert board.actuator_state(0) is ActuatorState.READY
    assert transport.writes[:2] == ["CFG SAFE", "CFG SAFE OFF"]
    assert transport.writes[2:26] == [f"OUT {actuator} 0 0" for actuator in range(24)]
    assert transport.writes[26:] == [
        "CUR",
        "OUT 0 255 0",
        "CUR",
        "CUR",
        "OUT 0 0 0",
        "CFG SAFE ON",
    ]
    assert sleeps == [0.25, 2.0]
    assert [write for write in transport.writes if write.startswith("OUT") and write.split()[2:] != ["0", "0"]] == [
        "OUT 0 255 0"
    ]


def test_lansing_detect_marks_actuator_not_connected(monkeypatch):
    transport = FakeTransport(detection_responses(1.0, 1.04, None))
    board = Lansing(transport=transport)
    sleeps = []
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", sleeps.append)

    assert board.detect(3) is ActuatorState.NOT_CONNECTED
    assert board.actuator_state(3) is ActuatorState.NOT_CONNECTED
    assert sleeps == [0.25]
    assert transport.writes.count("CUR") == 2


def test_lansing_detect_never_accepts_current_below_baseline(monkeypatch):
    transport = FakeTransport(detection_responses(1.17, 1.04, None))
    board = Lansing(transport=transport)
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    assert board.detect(6) is ActuatorState.NOT_CONNECTED
    detection = board.last_detection(6)
    assert detection is not None
    assert detection.baseline_ma == 1.17
    assert detection.forward_ma == 1.04
    assert detection.delta_ma == 0.13


def test_conditioned_detection_preserves_presence_when_current_falls_below_baseline(monkeypatch):
    transport = FakeTransport(detection_responses(1.17, 1.80, 1.04))
    board = Lansing(transport=transport)
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    assert board.detect(7) is ActuatorState.READY


def test_diagnosis_below_detection_threshold_preserves_detected_actuator():
    board = Lansing(transport=FakeTransport())
    board._actuator_states[6] = ActuatorState.READY

    detection = board.classify_diagnosis(
        Diagnosis(actuator=6, baseline_ma=1.17, forward_ma=1.04, discharge_ma=1.10)
    )

    assert detection.state is ActuatorState.READY
    assert board.actuator_state(6) is ActuatorState.READY


def test_lansing_detect_preserves_dt0_presence_below_conditioned_connection_limit(monkeypatch):
    transport = FakeTransport(detection_responses(1.0, 1.2, 1.04))
    board = Lansing(transport=transport)
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    assert board.detect(3) is ActuatorState.READY
    assert board.last_detection(3).initial_delta_ma == 0.2
    assert board.last_detection(3).delta_ma == 0.04


def test_reported_detection_limit_does_not_revoke_dt0_presence(monkeypatch):
    transport = FakeTransport(detection_responses(0.96, 1.40, 1.18))
    board = Lansing(transport=transport)
    board.not_connected_delta_ma = 0.34
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    assert board.detect(1) is ActuatorState.READY
    assert board.last_detection(1).delta_ma == pytest.approx(0.22)


def test_lansing_detect_reuses_supplied_baseline_without_measuring_it(monkeypatch):
    responses = ["OK:SAFE>ON", "OK:CFG_SAFE", *(["OK:OUT"] * Lansing.actuator_count)]
    responses.extend(["OK:OUT", "OK:1.20", "OK:1.05", "OK:OUT", "OK:CFG_SAFE"])
    transport = FakeTransport(responses)
    board = Lansing(transport=transport)
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    detection = board.detect_actuator(3, baseline_ma=1.0)

    assert detection.baseline_ma == 1.0
    assert detection.initial_forward_ma == 1.2
    assert detection.forward_ma == 1.05
    assert transport.writes.count("CUR") == 2


def test_lansing_detect_rejects_high_initial_current_without_conditioning(monkeypatch):
    transport = FakeTransport(detection_responses(1.0, 11.01, None))
    board = Lansing(transport=transport)
    sleeps = []
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", sleeps.append)

    assert board.detect(4) is ActuatorState.ERROR
    assert board.actuator_state(4) is ActuatorState.ERROR
    assert board.last_detection(4).initial_delta_ma == 10.01
    assert sleeps == [0.25]
    assert transport.writes.count("CUR") == 2


def test_lansing_detect_marks_conditioned_current_at_three_ma_as_error(monkeypatch):
    transport = FakeTransport(detection_responses(1.0, 5.0, 4.0))
    board = Lansing(transport=transport)
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    assert board.detect(4) is ActuatorState.ERROR
    assert board.last_detection(4).delta_ma == 3.0


def test_lansing_detect_stops_output_and_restores_safety_when_measurement_fails(monkeypatch):
    transport = FakeTransport(
        [
            "OK:SAFE>OFF",
            "OK:CFG_SAFE",
            *(["OK:OUT"] * Lansing.actuator_count),
            "OK:OUT",
            "OK:OUT",
            "OK:CFG_SAFE",
        ]
    )
    board = Lansing(transport=transport)
    measurements = iter([1.0, RuntimeError("current read failed")])

    def current() -> float:
        result = next(measurements)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(board, "current", current)
    monkeypatch.setattr("fluid_reality.boards.board.time.sleep", lambda _duration: None)

    with pytest.raises(RuntimeError, match="current read failed"):
        board.detect(2)

    assert transport.writes[-2:] == ["OUT 2 0 0", "CFG SAFE OFF"]
    assert board.actuator_state(2) is ActuatorState.UNKNOWN


def test_lansing_initialize_requires_detection_first():
    board = Lansing(transport=FakeTransport())

    with pytest.raises(RuntimeError, match="run detect"):
        board.initialize(0)


def test_lansing_initialize_runs_dashboard_sequence_and_returns_state(monkeypatch):
    transport = FakeTransport(
        [
            "OK:217.87",
            "OK:CFG_SAFE",
            "OK:OUT",
            "OK:0.80",
            "OK:OUT",
            "OK:0.82",
            "OK:OUT",
            "OK:CFG_SAFE",
            "OK:ACT>4,BASE>0.88,FWD>1.12,DIS>0.95",
        ]
    )
    board = Lansing(transport=transport)
    board._actuator_states[4] = ActuatorState.ERROR
    board.initialization_stage_duration_s = 0.0
    progress = []

    state = board.initialize(4, progress_callback=progress.append)

    assert state is ActuatorState.READY
    assert board.actuator_state(4) is ActuatorState.READY
    assert progress[-1]["elapsed_s"] == 0.0
    assert transport.writes == [
        "VLT",
        "CFG SAFE OFF",
        "OUT 4 0 0",
        "CUR",
        "OUT 4 0 0",
        "CUR",
        "OUT 4 0 0",
        "CFG SAFE ON",
        "DIA 4",
    ]


def test_initialize_reports_each_voltage_as_it_is_sent(monkeypatch):
    transport = FakeTransport(
        [
            "OK:217.87",
            "OK:CFG_SAFE",
            "OK:OUT",
            "OK:1.00",
            "OK:OUT", "OK:1.10",
            "OK:OUT", "OK:0.90",
            "OK:OUT", "OK:1.20",
            "OK:OUT", "OK:1.00",
            "OK:OUT",
            "OK:CFG_SAFE",
            "OK:ACT>4,BASE>0.88,FWD>1.12,DIS>0.95",
        ]
    )
    board = Lansing(transport=transport)
    board._actuator_states[4] = ActuatorState.ERROR
    board.initialization_stages_v = (25.0,)
    board.initialization_stage_duration_s = 1.1
    board.initialization_phase_interval_s = 0.5
    clock = [0.0]

    monkeypatch.setattr("fluid_reality.boards.board.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "fluid_reality.boards.board.time.sleep",
        lambda duration: clock.__setitem__(0, clock[0] + duration),
    )
    progress = []

    board.initialize(4, progress_callback=progress.append)

    assert [item["sent_voltage"] for item in progress] == [0.0, 25.0, -25.0, 25.0, 0.0]
    assert [item["phase"] for item in progress] == [
        "baseline",
        "positive",
        "negative",
        "positive",
        "off",
    ]
    assert [item["delta_ma"] for item in progress if "delta_ma" in item] == pytest.approx(
        [0.10, 0.20]
    )
    assert all(item["phase_interval_s"] == 0.5 for item in progress)
