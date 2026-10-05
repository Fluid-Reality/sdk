"""Hardware-free checks for the Glove Demo's timing and shutdown behavior."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from fluid_reality import ActuatorState
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox
from apps.glove_demo import app as app_module, worker as worker_module
from apps.glove_demo.app import ConnectionDialog, GloveDemo
from apps.glove_demo.hand_view import HandView
from apps.glove_demo.worker import BoardThread, DemoConfig, pattern_values


class FakeBoard:
    actuator_count = 8
    instances: list[FakeBoard] = []

    def __init__(self, endpoint: str, **options: object) -> None:
        self.endpoint = endpoint
        self.events: list[tuple[object, ...]] = []
        self.manual: dict[int, tuple[int, int]] = {}
        self.safety_state = True
        self.detect_state = ActuatorState.READY
        self.discharge_readings = [100, 0]
        FakeBoard.instances.append(self)

    def force_text_mode(self) -> None:
        self.events.append(("text",))

    def power_on(self) -> None:
        self.events.append(("power_on",))

    def power_off(self) -> None:
        self.events.append(("power_off",))

    def voltage(self) -> float:
        return 200.0

    def detect(self, actuator: int) -> ActuatorState:
        self.events.append(("detect", actuator))
        return self.detect_state

    def safety(self, enabled: bool | None = None) -> bool:
        if enabled is not None:
            self.safety_state = enabled
            self.events.append(("safety", enabled))
        return self.safety_state

    def set_manual_output(self, actuator: int, positive: int, negative: int) -> None:
        self.manual[actuator] = (positive, negative)
        self.events.append(("manual", actuator, positive, negative))

    def set_actuator(self, actuator: int, value: int) -> None:
        self.events.append(("act", actuator, value))

    def status(self) -> dict[str, tuple[int, ...]]:
        self.events.append(("status",))
        remaining = self.discharge_readings.pop(0) if self.discharge_readings else 0
        return {"discharge_ms_left": (remaining,) * self.actuator_count}

    def close(self) -> None:
        self.events.append(("close",))


def ready_worker(board: FakeBoard) -> BoardThread:
    worker = BoardThread(board.endpoint, FakeBoard)
    worker._actuator_states = {actuator: ActuatorState.READY for actuator in range(board.actuator_count)}
    return worker


def test_bipolar_cycles_have_zero_signed_area() -> None:
    step = 1 / 2000
    for pattern in ("Pulse", "Snap", "Square Wave"):
        area = sum(pattern_values(pattern, (index + 0.5) * step)[0] * step for index in range(2000))
        assert abs(area) < 0.2, (pattern, area)
    assert pattern_values("Pulse", 0.6)[0] == -255
    assert pattern_values("Pulse", 0.9)[0] == 0


def test_demo_refuses_cached_actuator_that_is_not_ready() -> None:
    board = FakeBoard("fake")
    worker = ready_worker(board)
    worker._actuator_states[0] = ActuatorState.ERROR
    with pytest.raises(RuntimeError, match="not started"):
        worker._run_config(board, DemoConfig("Square Wave", (0, 1, 2, 3, 4)))
    assert not any(event[0] == "detect" for event in board.events)
    assert not any(event[0] in {"manual", "act"} for event in board.events)
    assert board.events[-1] == ("power_off",)


def test_wave_waits_for_discharge_before_finishing() -> None:
    board = FakeBoard("fake")
    worker = ready_worker(board)
    stop = threading.Timer(0.01, worker.stop_demo)
    stop.start()
    try:
        worker._run_config(board, DemoConfig("Fast Wave", (0, 1, 2, 3, 4)))
    finally:
        stop.join()
    assert ("act", 0, 255) in board.events
    assert ("act", 0, 0) in board.events
    assert sum(event == ("status",) for event in board.events) == 2
    assert board.events.index(("act", 0, 0)) < board.events.index(("power_off",))


def test_manual_output_is_balanced_and_previous_safety_restored() -> None:
    board = FakeBoard("fake")
    board.safety_state = False
    worker = ready_worker(board)

    def one_short_pulse(board: FakeBoard, config: DemoConfig, channels) -> None:
        worker._set_manual(board, 0, channels[0], 255)
        time.sleep(0.025)
        worker.stop_demo()

    worker._run_bipolar = one_short_pulse  # type: ignore[method-assign]
    worker._run_config(board, DemoConfig("Pulse", (0, 0, 0, 0, 0)))
    assert board.manual[0] == (0, 0)
    assert ("manual", 0, 0, 255) in board.events
    assert board.safety_state is False
    assert board.events[-1] == ("power_off",)


def test_disconnect_finishes_worker_and_closes_board() -> None:
    FakeBoard.instances.clear()
    worker = BoardThread("fake", FakeBoard)
    worker.start()
    worker.disconnect()
    assert worker.wait(3000)
    assert len(FakeBoard.instances) == 1
    assert FakeBoard.instances[0].events[0] == ("text",)
    assert FakeBoard.instances[0].events[-1] == ("close",)


@pytest.mark.parametrize("firmware", ["Lansing", "Rockford"])
def test_serial_board_profile_is_identified_from_version(
    monkeypatch: pytest.MonkeyPatch, firmware: str
) -> None:
    class Probe:
        instances: list[Probe] = []

        def __init__(self, endpoint: str, **options: object) -> None:
            assert endpoint == "COM9"
            self.transport = object()
            self.closed = False
            self.instances.append(self)

        def force_text_mode(self) -> None:
            pass

        def version(self) -> dict[str, str]:
            return {"FW": firmware}

        def close(self) -> None:
            self.closed = True

    class LansingWrapper:
        def __init__(self, *, transport: object) -> None:
            self.transport = transport

    class RockfordWrapper:
        def __init__(self, *, transport: object) -> None:
            self.transport = transport

    monkeypatch.setattr(worker_module, "Board", Probe)
    monkeypatch.setattr(worker_module, "Lansing", LansingWrapper)
    monkeypatch.setattr(worker_module, "Rockford", RockfordWrapper)
    board = BoardThread("COM9")._open_board()
    expected = LansingWrapper if firmware == "Lansing" else RockfordWrapper
    assert isinstance(board, expected)
    assert board.transport is Probe.instances[-1].transport
    assert not Probe.instances[-1].closed


def test_unknown_firmware_closes_serial_port(monkeypatch: pytest.MonkeyPatch) -> None:
    class Probe:
        instance: Probe | None = None

        def __init__(self, endpoint: str, **options: object) -> None:
            self.closed = False
            Probe.instance = self

        def force_text_mode(self) -> None:
            pass

        def version(self) -> dict[str, str]:
            return {"FW": "Mystery"}

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(worker_module, "Board", Probe)
    with pytest.raises(RuntimeError, match="Unsupported board firmware Mystery"):
        BoardThread("COM9")._open_board()
    assert Probe.instance is not None and Probe.instance.closed


def test_connection_dialog_only_requests_serial_port(monkeypatch: pytest.MonkeyPatch) -> None:
    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_module, "list_ports", lambda: ["COM9"])
    dialog = ConnectionDialog()
    assert len(dialog.findChildren(QComboBox)) == 1
    assert dialog.connection() == "COM9"
    dialog.close()
    application.processEvents()


def test_worker_detects_actuators_once_at_startup() -> None:
    board = FakeBoard("fake")
    worker = BoardThread("fake", FakeBoard)
    worker._detect_actuators_at_startup(board)
    assert worker._actuator_states == {actuator: ActuatorState.READY for actuator in range(8)}
    assert sum(event[0] == "detect" for event in board.events) == 8

    stop = threading.Timer(0.01, worker.stop_demo)
    stop.start()
    try:
        worker._run_config(board, DemoConfig("Fast Wave", (0, 1, 2, 3, 4)))
    finally:
        stop.join()
    assert sum(event[0] == "detect" for event in board.events) == 8


def test_gui_mapping_can_repeat_and_locks_during_demo() -> None:
    application = QApplication.instance() or QApplication([])
    window = GloveDemo()
    assert window.windowTitle() == "Fluid Reality Glove Demo"
    assert not window.windowIcon().isNull()
    assert [box.currentIndex() for box in window.finger_boxes] == list(range(5))
    window.finger_boxes[1].setCurrentIndex(0)
    assert window.hand._actuators == [0, 0, 2, 3, 4]
    window._set_running(True)
    assert all(not box.isEnabled() for box in window.finger_boxes)
    assert not window.pattern_box.isEnabled()
    assert not window.connect_button.isEnabled()
    assert window.run_button.text() == "STOP DEMO"
    window.close()
    application.processEvents()


def test_hand_view_colors_follow_signed_activation() -> None:
    positive_fill, _, _, positive_intensity = HandView._activation_style(128)
    negative_fill, _, _, negative_intensity = HandView._activation_style(-128)
    neutral_fill, _, _, neutral_intensity = HandView._activation_style(0)

    assert positive_intensity == pytest.approx(128 / 255)
    assert negative_intensity == pytest.approx(128 / 255)
    assert neutral_intensity == 0
    assert positive_fill.red() > positive_fill.blue()
    assert negative_fill.blue() > negative_fill.red()
    assert neutral_fill.name() == "#173b53"


def test_bipolar_demo_starts_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    application = QApplication.instance() or QApplication([])
    window = GloveDemo()

    class StartOnlyWorker:
        config: DemoConfig | None = None

        def start_demo(self, config: DemoConfig) -> None:
            self.config = config

    worker = StartOnlyWorker()
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: pytest.fail("Unexpected manual-output dialog"),
    )
    window._worker = worker  # type: ignore[assignment]
    window._connected = True
    window.toggle_demo()
    assert worker.config == DemoConfig("Pulse", (0, 1, 2, 3, 4))
    window._worker = None
    window.close()
    application.processEvents()
