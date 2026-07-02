from __future__ import annotations

import logging

import pytest

from fluid_reality import ActuatorState, FirmwareError, Lansing


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

    monkeypatch.setattr("fluid_reality.boards.lansing.SerialTransport", FakeSerialTransport)

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


def test_lansing_detect_marks_actuator_ready():
    transport = FakeTransport(["OK:ACT"] * 8 + ["OK:ACT>0,BASE>0.86,FWD>1.18,DIS>0.91"])
    board = Lansing(transport=transport)

    assert board.detect(0) is ActuatorState.READY

    detection = board.last_detection(0)
    assert detection is not None
    assert detection.state is ActuatorState.READY
    assert detection.delta_ma == 0.32
    assert board.actuator_state(0) is ActuatorState.READY
    assert transport.writes == [
        "ACT 0 0",
        "ACT 1 0",
        "ACT 2 0",
        "ACT 3 0",
        "ACT 4 0",
        "ACT 5 0",
        "ACT 6 0",
        "ACT 7 0",
        "DIA 0",
    ]


def test_lansing_detect_marks_actuator_not_connected():
    transport = FakeTransport(["OK:ACT"] * 8 + ["OK:ACT>3,BASE>0.86,FWD>0.91,DIS>0.90"])
    board = Lansing(transport=transport)

    assert board.detect(3) is ActuatorState.NOT_CONNECTED
    assert board.actuator_state(3) is ActuatorState.NOT_CONNECTED


def test_lansing_detect_marks_actuator_error():
    transport = FakeTransport(["OK:ACT"] * 8 + ["OK:ACT>4,BASE>0.88,FWD>4.42,DIS>1.16"])
    board = Lansing(transport=transport)

    assert board.detect(4) is ActuatorState.ERROR
    assert board.actuator_state(4) is ActuatorState.ERROR


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
        "OUT 4 0 0",
        "CFG SAFE ON",
        "DIA 4",
    ]
