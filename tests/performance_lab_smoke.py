"""Visible lab screenshots plus optional native preview; no private app state."""
from pathlib import Path
import json
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import performance_lab as L
from performance_scenarios import scenario
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def main():
    if L.make_transparent_gl:
        L.make_transparent_gl()
    app = QApplication([])
    output = ROOT / "work/verification"
    output.mkdir(parents=True, exist_ok=True)
    clock = [0.0]
    lab = L.PerformanceLab(clock=lambda: clock[0], auto_start=False)
    checks, failures, sizes = [], [], {}

    def capture(name):
        assert lab.grab().save(str(output / (name + ".png")))
        sizes[name] = [lab.width(), lab.height()]

    def live():
        lab.begin_turn()
        lab.dispatch("mood.set", mood="happy")
        lab.dispatch("action.request", action="drink_tea", source="user")
        clock[0] = 1.4
        lab.tick()
        capture("performance-lab-live")
        lab.recording.save(output / "performance-lab-recording.json")
        assert lab.load_replay(scenario("skin"))
        lab.seek(4200)

    def replay_view():
        capture("performance-lab-replay")
        assert "旧" in lab.table.item(lab.table.rowCount()-1, 3).text()
        lab.reset_recording()
        lab.resize(960, 680)
        lab.profile_box.setCurrentIndex(lab.profile_box.findData("hiyori"))
        assert L.HAS_LIVE2D, "Native preview is unavailable in this environment"
        lab.native_box.setChecked(True)

    def native():
        assert lab.width() <= 960 and lab.height() <= 680, "Small-window layout is forced beyond the viewport"
        assert lab._native is not None and lab._native._ready
        assert lab.recording.engine.available
        frame = lab._native.grabFramebuffer()
        assert sum(frame.pixelColor(x, y).alpha() > 0
                   for x in range(0, frame.width(), 8) for y in range(0, frame.height(), 8)) > 30
        lab.begin_turn()
        lab.phase("replying")
        capture("performance-lab-native")
        lab.native_box.setChecked(False)
        assert lab._native is None and lab.simulation.isVisible()

    steps = iter([(150, live), (150, replay_view), (900, native)])

    def finish():
        lab.close()
        result = {"checks": checks, "failures": failures, "window_sizes": sizes}
        (output / "performance-lab-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        app.quit()

    def next_step():
        try:
            delay, action = next(steps)
        except StopIteration:
            finish()
            return
        def run():
            try:
                action()
                checks.append(action.__name__)
                next_step()
            except Exception as exc:
                traceback.print_exc()
                failures.append(str(exc))
                finish()
        QTimer.singleShot(delay, run)

    lab.show()
    next_step()
    app.exec()
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
