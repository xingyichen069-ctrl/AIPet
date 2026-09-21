"""Small pointer gesture state machines shared by the desktop windows."""

from __future__ import annotations

from PySide6.QtCore import QPoint


class DragGesture:
    """Classify one left-button gesture as a click or a drag.

    A press only records the origin.  Movement starts once the squared
    distance reaches ``threshold``; release then reports the final state and
    resets the recognizer for the next gesture.
    """

    def __init__(self, threshold: int = 8):
        self.threshold = max(1, int(threshold))
        self._origin: QPoint | None = None
        self.dragging = False

    @property
    def active(self) -> bool:
        return self._origin is not None

    def press(self, point: QPoint) -> None:
        self._origin = QPoint(point)
        self.dragging = False

    def move(self, point: QPoint) -> bool:
        if self._origin is None:
            return False
        if not self.dragging:
            delta = QPoint(point) - self._origin
            if delta.x() * delta.x() + delta.y() * delta.y() >= self.threshold ** 2:
                self.dragging = True
        return self.dragging

    def release(self, point: QPoint) -> bool:
        dragging = self.move(point)
        self.reset()
        return dragging

    def reset(self) -> None:
        self._origin = None
        self.dragging = False

