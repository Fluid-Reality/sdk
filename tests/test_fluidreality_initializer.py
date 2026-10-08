from __future__ import annotations

import json
import os
import queue
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from apps.fluidreality_initializer.demo import DEMO_BOARDS, SimulatedRockfordTransport
from apps.fluidreality_initializer.engine import BoardSession, Clock
from apps.fluidreality_initializer.reporting import ReportWriter
from apps.fluidreality_initializer.sequence import (
    InitializationConfig,
    build_plan,
    load_presets,
    parse_number_list,
)
from fluid_reality import Rockford

ROOT = Path(__file__).resolve().parents[1]
QUICK = InitializationConfig(
    name="Test",
    voltages_v=(50.0, 100.0),
    high_times_s=(1.0,),
    repeats=1,
    num_cycles=2,
    pause_time_s=1.0,
)


class RecordingTransport(SimulatedRockfordTransport):
    """Simulated board that records every command and can inject failures."""

    def __init__(self, *args, fail=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.writes: list[tuple[float, str]] = []
        self.fail = fail

    def write_line(self, line: str) -> None:
        self.writes.append((time.monotonic(), line))
        if self.fail is not None:
            outcome = self.fail(line)
            if outcome == "transport":
                from fluid_reality import TransportError

                raise TransportError("cable unplugged")
            if outcome:
                self._lines.append(outcome)
                return
        super().write_line(line)


def make_session(tmp_path: Path, *, speed: float = 50.0, fail=None, board: str = "Demo bench A"):
    events: queue.Queue = queue.Queue()
    holder: dict[str, RecordingTransport] = {}

    def factory() -> Rockford:
        transport = RecordingTransport(DEMO_BOARDS[board], speed=speed, name=board, fail=fail)
        holder["transport"] = transport
        return Rockford(transport=transport)

    session = BoardSession("b1", "Bench", "sim://test", factory, events, output_dir=tmp_path,
                           clock=Clock(speed), reporter_factory=ReportWriter)
    session.start()
    session.power(True)
    return session, events, holder


def wait_until(predicate, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def finished(session: BoardSession, channels) -> bool:
    """Process ended and its report (written on the report thread) is ready."""

    return all(
        ch in session.processes
        and session.processes[ch].status in {"completed", "stopped", "failed"}
        and session.processes[ch].report_paths
        for ch in channels
    )


# --------------------------------------------------------------- sequence
def test_standard_preset_matches_infinidaq_sequence() -> None:
    presets = load_presets()
    standard = presets["Standard initialization"]
    plan = build_plan(standard)
    assert len(plan.runs) == 4 * 7 * 3
    # Same total drive time as infinidaq/amplifier_sequence_run.py: 6.13 h.
    assert plan.drive_duration_s == pytest.approx(22080.0)
    forward, reverse = plan.volt_seconds()
    assert forward == pytest.approx(reverse)


def test_imports_infinidaq_amplifier_sequence_config(tmp_path: Path) -> None:
    source = {
        "voltages": [50, 100], "high_times": [1, 5], "repeats": 2, "pause_time": 5,
        "num_cycles": 5, "low_mode": "negative", "pre_hold": True, "actuator_name": "M4.2",
    }
    path = tmp_path / "amp.json"
    path.write_text(json.dumps(source))
    config = InitializationConfig.load(path)
    assert config.voltages_v == (50.0, 100.0)
    assert config.pre_hold is True
    plan = build_plan(config)
    first = plan.phases[0]
    assert first.kind == "pre_hold" and first.target_v == -50.0 and first.duration_s == 1.0
    assert InitializationConfig.from_dict(config.to_dict()) == config


def test_invalid_sequences_are_rejected() -> None:
    assert InitializationConfig(voltages_v=(300.0,)).validate()
    assert InitializationConfig(high_times_s=()).validate()
    with pytest.raises(ValueError):
        build_plan(InitializationConfig(repeats=0))
    assert parse_number_list("50, 100 150;200") == (50.0, 100.0, 150.0, 200.0)


# ------------------------------------------------------------------ engine
def test_independent_processes_complete_with_reports(tmp_path: Path) -> None:
    # Modest speed-up so the drive-time precision check is not dominated by OS jitter.
    session, events, holder = make_session(tmp_path, speed=5.0)
    try:
        session.start_process(0, "ACT-0", QUICK)
        session.start_process(1, "ACT-1", QUICK.with_changes(voltages_v=(80.0,)))
        wait_until(lambda: finished(session, (0, 1)))
        for channel in (0, 1):
            process = session.processes[channel]
            assert process.status == "completed", process.status_detail
            assert process.measurements, "expected start/end readings"
            # Each high/low phase got its configured drive time.
            for record in process.phase_records:
                if record.kind in ("high", "low"):
                    assert record.drive_s == pytest.approx(1.0, abs=0.15)
            assert process.vt_forward_vs == pytest.approx(process.vt_reverse_vs, rel=0.05)
            report = json.loads((Path(process.report_paths["folder"]) / "report.json").read_text())
            assert report["schema"] == "fluid-reality/actuator-initialization-report"
            assert report["summary"]["actuator_id"] == f"ACT-{channel}"
            assert report["summary"]["status"] == "completed"
            assert report["checks"]["pre"] and report["checks"]["post"]
            assert (Path(process.report_paths["folder"]) / "report.html").read_text().startswith("<!doctype html>")
            rows = (Path(process.report_paths["folder"]) / "measurements.csv").read_text().splitlines()
            assert len(rows) == len(process.measurements) + 1
    finally:
        session.shutdown()
        session.join(5)


def test_measurement_pauses_other_actuators_and_maps_polarity(tmp_path: Path) -> None:
    session, events, holder = make_session(tmp_path)
    try:
        every = QUICK.with_changes(pre_check=False, post_check=False, measurement_mode="every_phase")
        session.start_process(0, "A", every)
        session.start_process(1, "B", every)
        wait_until(lambda: finished(session, (0, 1)))
        lines = [line for _, line in holder["transport"].writes]
        # Every current reading on one channel is surrounded by the other channel being zeroed and restored.
        for index, line in enumerate(lines):
            parts = line.split()
            if parts[0] != "OUC" or parts[2:4] == ["0", "0"]:
                continue
            channel = int(parts[1])
            other = 1 - channel
            before = [l for l in lines[:index] if l.startswith(f"OUT {other} ")]
            if before and before[-1] != f"OUT {other} 0 0":
                pytest.fail(f"channel {other} was still driven during {line!r}")
        # Negative drive: OUC uses TOP=255-value with BOTTOM on; OUT uses the negative electrode.
        expected = [round(255 * volts / session.supply_v) for volts in QUICK.voltages_v]
        negative_tops = [int(l.split()[2]) for l in lines if l.startswith("OUC 0 ") and l.split()[3] == "1"]
        assert negative_tops
        assert all(min(abs((255 - top) - value) for value in expected) <= 2 for top in negative_tops)
        assert any(l.startswith("OUT 1 0 ") and l != "OUT 1 0 0" for l in lines)
        assert "CFG SAFE OFF" in lines and lines.index("CFG SAFE OFF") < lines.index(next(l for l in lines if l.startswith("OUC")))
        assert lines[-1] == "CFG SAFE ON" or "CFG SAFE ON" in lines
    finally:
        session.shutdown()
        session.join(5)


def test_firmware_error_fails_only_that_actuator(tmp_path: Path) -> None:
    calls = {"n": 0}

    def fail(line: str):
        if line.startswith("OUC 1 ") and not line.startswith("OUC 1 0 0"):
            calls["n"] += 1
            if calls["n"] == 3:
                return "ER:OUC_FAILED"
        return None

    session, events, holder = make_session(tmp_path, fail=fail)
    try:
        config = QUICK.with_changes(pre_check=False, post_check=False)
        session.start_process(0, "A", config)
        session.start_process(1, "B", config)
        wait_until(lambda: finished(session, (0, 1)))
        assert session.processes[1].status == "failed"
        assert "OUC_FAILED" in session.processes[1].status_detail
        assert session.processes[0].status == "completed"
        assert session.processes[1].report_paths, "partial report expected"
    finally:
        session.shutdown()
        session.join(5)


def test_lost_connection_fails_every_process_on_the_board(tmp_path: Path) -> None:
    state = {"armed": False}

    def fail(line: str):
        return "transport" if state["armed"] else None

    session, events, holder = make_session(tmp_path, fail=fail)
    try:
        config = QUICK.with_changes(pre_check=False, post_check=False, pause_time_s=20.0)
        session.start_process(0, "A", config)
        session.start_process(2, "C", config)
        wait_until(lambda: all(ch in session.processes and session.processes[ch].status == "running" for ch in (0, 2)))
        state["armed"] = True
        wait_until(lambda: finished(session, (0, 2)), timeout=20)
        for channel in (0, 2):
            process = session.processes[channel]
            assert process.status == "failed"
            assert "Board fault" in process.status_detail
            assert process.report_paths
        assert session.faulted
    finally:
        session.shutdown()
        session.join(5)


def test_keepalive_traffic_during_long_phase(tmp_path: Path) -> None:
    session, events, holder = make_session(tmp_path, speed=1.0)
    try:
        config = QUICK.with_changes(pre_check=False, post_check=False, pause_time_s=4.0)
        session.start_process(0, "A", config)
        wait_until(lambda: 0 in session.processes and session.processes[0].status == "running", timeout=10)
        start = time.monotonic()
        time.sleep(3.5)  # inside the 4 s leading pause: no phase traffic
        session.stop_process(0)
        wait_until(lambda: finished(session, (0,)), timeout=10)
        stamps = [t for t, _ in holder["transport"].writes if t >= start]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert stamps and max(gaps) < 1.6, "firmware watchdog needs traffic well within 10 s"
    finally:
        session.shutdown()
        session.join(5)


def test_pause_resume_and_stop(tmp_path: Path) -> None:
    session, events, holder = make_session(tmp_path, speed=5.0)
    try:
        config = QUICK.with_changes(pre_check=False, post_check=False, pause_time_s=1.0, high_times_s=(4.0,))
        session.start_process(0, "A", config)
        wait_until(lambda: 0 in session.processes and session.processes[0].phase_pos >= 1, timeout=10)
        session.pause_process(0)
        wait_until(lambda: session.processes[0].status == "paused", timeout=5)
        frozen = session.processes[0].phase_elapsed_s
        time.sleep(0.5)
        assert session.processes[0].phase_elapsed_s == pytest.approx(frozen)
        assert session.processes[0].output_value == 0
        session.resume_process(0)
        wait_until(lambda: session.processes[0].status == "running", timeout=5)
        session.stop_process(0)
        wait_until(lambda: finished(session, (0,)), timeout=5)
        report = json.loads((Path(session.processes[0].report_paths["folder"]) / "report.json").read_text())
        assert report["status"] == "stopped"
    finally:
        session.shutdown()
        session.join(5)


def test_not_connected_actuator_fails_pre_check(tmp_path: Path) -> None:
    session, events, holder = make_session(tmp_path)
    try:
        session.start_process(3, "EMPTY", QUICK)  # Demo bench A has nothing on channel 3
        wait_until(lambda: finished(session, (3,)))
        assert session.processes[3].status == "failed"
        assert "not connected" in session.processes[3].status_detail
    finally:
        session.shutdown()
        session.join(5)


# ---------------------------------------------------------------------- UI
def test_main_window_runs_demo_offscreen(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from apps.fluidreality_initializer.app import MainWindow, tile_view

    app = QApplication.instance() or QApplication([])
    window = MainWindow(demo=True, demo_speed=60)
    try:
        window.output_dir = tmp_path
        for board in window.boards.values():
            board.session.output_dir = tmp_path
        board = next(iter(window.boards.values()))
        deadline = time.monotonic() + 10
        while not board.connected and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert board.connected
        board.session.power(True)
        while not board.psu_on and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        model = board.channels[0]
        model.actuator_id = "UI-0"
        model.config = QUICK
        window._start(board, model)
        window.select(board.key, 0)
        deadline = time.monotonic() + 60
        while model.process_status != "completed" and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert model.process_status == "completed"
        assert model.measurements and model.voltage_steps
        view = tile_view(model, board)
        assert view["state"] == "completed"
        window._refresh_selected()
    finally:
        for board in window.boards.values():
            board.session.shutdown()
        window.close()


# ------------------------------------------------- safety regressions (review)
def test_neighbour_that_cannot_be_zeroed_faults_the_board_and_powers_off(tmp_path: Path) -> None:
    state = {"armed": False}

    def fail(line: str):
        if state["armed"] and line == "OUT 1 0 0":
            return "ER:OUT_FAILED"
        return None

    session, events, holder = make_session(tmp_path, speed=10.0, fail=fail)
    try:
        config = QUICK.with_changes(pre_check=False, post_check=False, high_times_s=(3.0,))
        session.start_process(0, "A", config)
        session.start_process(1, "B", config)
        wait_until(lambda: all(ch in session.processes and session.processes[ch].output_value != 0 for ch in (0, 1)), timeout=20)
        state["armed"] = True
        wait_until(lambda: finished(session, (0, 1)), timeout=30)
        assert session.faulted
        assert all(session.processes[ch].status == "failed" for ch in (0, 1))
        engine = holder["transport"].engine
        assert engine.psu is False, "supply must be powered off when a channel cannot be confirmed at 0 V"
    finally:
        session.shutdown()
        session.join(10)


def test_failed_restore_fails_only_the_neighbour(tmp_path: Path) -> None:
    state = {"armed": False, "hits": 0}

    def fail(line: str):
        parts = line.split()
        if state["armed"] and parts[:2] == ["OUT", "1"] and line != "OUT 1 0 0" and state["hits"] == 0:
            state["hits"] += 1
            return "ER:OUT_FAILED"
        return None

    session, events, holder = make_session(tmp_path, speed=10.0, fail=fail)
    try:
        # Every-phase mode: phase changes go through OUC, so the injected OUT failure hits the restore.
        config = QUICK.with_changes(pre_check=False, post_check=False, high_times_s=(3.0,),
                                    measurement_mode="every_phase")
        session.start_process(0, "A", config)
        session.start_process(1, "B", config.with_changes(pause_time_s=0.5))
        wait_until(lambda: 1 in session.processes and session.processes[1].output_value != 0, timeout=20)
        state["armed"] = True
        wait_until(lambda: finished(session, (0, 1)), timeout=60)
        assert session.processes[1].status == "failed"
        assert "restore" in session.processes[1].status_detail
        assert session.processes[0].status == "completed"
        assert not session.faulted
        assert holder["transport"].engine.manual[1] == (0, 0)
    finally:
        session.shutdown()
        session.join(10)


def test_shutdown_leaves_outputs_zero_and_safety_on(tmp_path: Path) -> None:
    session, events, holder = make_session(tmp_path, speed=10.0)
    config = QUICK.with_changes(pre_check=False, post_check=False, high_times_s=(5.0,))
    session.start_process(0, "A", config)
    session.start_process(2, "C", config)
    wait_until(lambda: all(ch in session.processes and session.processes[ch].output_value != 0 for ch in (0, 2)), timeout=20)
    session.shutdown()
    session.join(15)
    engine = holder["transport"].engine
    assert all(output == (0, 0) for output in engine.manual[:5])
    assert engine.safe is True
    assert all(session.processes[ch].status == "stopped" for ch in (0, 2))


def test_supply_glitch_cannot_raise_drive_voltage(tmp_path: Path) -> None:
    session = BoardSession("b", "B", "x", lambda: None, queue.Queue(), output_dir=tmp_path)
    session._reset_supply(210.0)
    nominal = session._dac_value(100.0)
    session._update_supply(120.0)  # isolated low glitch is ignored
    assert session._dac_value(100.0) == nominal
    session._update_supply(205.0)
    assert session._dac_value(100.0) <= nominal + 1
    session._io.shutdown()


def test_second_start_on_running_channel_is_rejected_not_fatal(tmp_path: Path) -> None:
    session, events, holder = make_session(tmp_path, speed=10.0)
    try:
        config = QUICK.with_changes(pre_check=False, post_check=False)
        session.start_process(0, "A", config)
        wait_until(lambda: 0 in session.processes and session.processes[0].status == "running", timeout=10)
        session.start_process(0, "A-again", config)
        time.sleep(0.5)
        assert session.processes[0].status == "running"
        assert session.processes[0].actuator_id == "A"
        wait_until(lambda: finished(session, (0,)), timeout=60)
        assert session.processes[0].status == "completed"
    finally:
        session.shutdown()
        session.join(10)


def test_supply_rise_is_adopted_immediately(tmp_path: Path) -> None:
    session = BoardSession("b", "B", "x", lambda: None, queue.Queue(), output_dir=tmp_path)
    session._reset_supply(150.0)
    session._update_supply(212.0)  # supply rose: drive must drop now
    assert session._scale_supply_v() == pytest.approx(212.0)
    session._observe_supply(150.0)  # a low reading at start never raises drive
    assert session._scale_supply_v() == pytest.approx(212.0)
    session._io.shutdown()


def test_default_reads_after_first_and_last_cycle_only(tmp_path: Path) -> None:
    config = QUICK.with_changes(num_cycles=4, pre_check=False, post_check=False)
    plan = build_plan(config)
    assert plan.measurement_count() == 2 * len(plan.runs)
    session, events, holder = make_session(tmp_path)
    try:
        session.start_process(0, "A", config)
        wait_until(lambda: finished(session, (0,)))
        process = session.processes[0]
        assert process.status == "completed"
        readings = [(m.run_id, m.phase_kind, m.cycle, m.edge) for m in process.measurements]
        expected = [(run.run_id, "high", cycle, "end") for run in plan.runs for cycle in (1, 4)]
        assert readings == expected
        assert all(m.target_v > 0 for m in process.measurements)
        # Only two board-pausing reading events per run, plus the initial baseline.
        oucs = [l for _, l in holder["transport"].writes if l.startswith("OUC 0 ") and not l.startswith("OUC 0 0 0")]
        assert len(oucs) == 2 * len(plan.runs)
        for record in process.phase_records:
            if record.kind in ("high", "low"):
                assert record.drive_s == pytest.approx(1.0, abs=0.15)
    finally:
        session.shutdown()
        session.join(10)
