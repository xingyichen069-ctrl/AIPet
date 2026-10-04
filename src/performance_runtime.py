"""Qt clock bridge for the pure controller; emits snapshots, never calls GL."""
import time
from PySide6.QtCore import QObject, QTimer, Signal

from performance import Engine


class PerformanceRuntime(QObject):
    changed = Signal(object, object)  # state snapshot, Profile

    def __init__(self, profile, parent=None, *, clock=time.monotonic, auto_start=True):
        super().__init__(parent)
        self.engine = Engine(profile)
        self.clock, self.started = clock, clock()
        self._last_output = None
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.send("renderer.set", value=False)
        if auto_start:
            self.timer.start()

    def now(self):
        return max(self.engine.now, int((self.clock() - self.started) * 1000))

    def _emit(self):
        state = self.engine.snapshot()
        comparable = {key: value for key, value in state.items() if key != "at_ms"}
        if comparable != self._last_output:
            self._last_output = comparable
            self.changed.emit(state, self.engine.profile)

    def send(self, kind, **fields):
        result = self.engine.dispatch({"kind": kind, **fields}, self.now())
        self._emit()
        return result

    def begin_turn(self):
        turn = self.engine.turn_id + 1
        self.send("turn.begin", turn_id=turn)
        return turn

    def reply_intent(self, turn_id, payload):
        """Future semantic producer entry point; call on the Qt owner thread."""
        results = self.engine.reply_intent(turn_id, payload, self.now())
        self._emit()
        return results

    def tick(self):
        self.engine.advance(self.now())
        self._emit()

    def close(self):
        self.send("close")
        self.timer.stop()
