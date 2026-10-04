"""Real Windows/OpenGL scheduler smoke test. Opens only its own public model.

No app entry point, private configuration or model API is loaded. Results go
under work/verification. Run with Windows Python and an active desktop.
"""
from pathlib import Path
import json
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import live2d_widget as L  # SDK initialization precedes QApplication.
from performance_profile import Profile, load_profile
from performance_runtime import PerformanceRuntime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QOpenGLContext
from PySide6.QtWidgets import QApplication


def main():
    output = ROOT / "work/verification"
    output.mkdir(parents=True, exist_ok=True)
    L.make_transparent_gl()
    app = QApplication([])
    widget = L.Live2DWidget(ROOT / "hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json")
    widget.resize(320, 460)
    runtime = PerformanceRuntime(load_profile("hiyori"), widget)
    runtime.changed.connect(widget.set_presentation)
    widget.set_presentation(runtime.engine.snapshot(), runtime.engine.profile)
    widget.render_ready.connect(lambda: runtime.send("renderer.set", value=True), Qt.QueuedConnection)
    widget.reload_finished.connect(lambda ok, message: runtime.send("renderer.set", value=True) if ok else None,
                                   Qt.QueuedConnection)
    widget.action_ended.connect(lambda epoch, token, success:
                               runtime.send("action.end", epoch=epoch, token=token, success=success), Qt.QueuedConnection)
    checks, failures, notices, starts, stops, parameter_sets, releases = [], [], [], [], [], [], []
    widget.render_failed.connect(failures.append)
    widget.presentation_notice.connect(notices.append)
    saved = {}
    original_destroy = widget._destroy_renderer

    def destroy(model):
        if model is not None:
            assert QOpenGLContext.currentContext() == widget.context()
            releases.append(id(model))
        original_destroy(model)
    widget._destroy_renderer = destroy

    def track_native():
        model = widget.model
        for name, rows in (("StartMotion", starts), ("StopAllMotions", stops), ("SetParameterValue", parameter_sets)):
            original = getattr(model, name)
            def record(*args, _original=original, _rows=rows, **kwargs):
                assert QOpenGLContext.currentContext() == widget.context(), "Native call outside owning GL context"
                _rows.append(args)
                return _original(*args, **kwargs)
            setattr(model, name, record)

    def frame(filename=None):
        assert widget._ready and runtime.engine.available and not failures, failures
        image = widget.grabFramebuffer()
        count = sum(image.pixelColor(x, y).alpha() > 0
                    for x in range(0, image.width(), 8) for y in range(0, image.height(), 8))
        assert count > 30, "Invisible native frame"
        if filename:
            assert image.save(str(output / filename))

    def ready():
        frame("performance-native-idle.png")
        track_native()
        runtime.begin_turn()
        runtime.send("level.set", level="deep")
        runtime.send("mood.set", mood="happy")

    def poses():
        frame("performance-native-thinking.png")
        actual = {widget.model.GetParameter(i).id: widget.model.GetParameter(i).value
                  for i in range(widget.model.GetParameterCount())}
        for name, value in runtime.engine.profile.pose(runtime.engine.snapshot()).items():
            assert abs(actual[name] - value) < 1e-5, (name, actual[name], value)
        runtime.send("action.request", action="greet", source="user")
        saved["token"] = dict(runtime.engine.active)

    def one_start():
        frame("performance-native-greet.png")
        assert runtime.engine.active["name"] == "greet"
        assert sum(row[:2] == ("Tap", 0) for row in starts) == 1
        # These files loop; the application must stop them at its own deadline.
        model_json = json.loads(Path(widget.model_json).read_text(encoding="utf-8"))
        motion = Path(widget.model_json).parent / model_json["FileReferences"]["Motions"]["Tap"][0]["File"]
        assert json.loads(motion.read_text(encoding="utf-8"))["Meta"]["Loop"] is True

    def deadline():
        frame()
        assert runtime.engine.active is None
        assert any(row["reason"] == "action_elapsed" for row in runtime.engine.history)
        assert sum(row[:2] == ("Tap", 0) for row in starts) == 1
        assert starts[-1][:2] == ("Idle", 0)
        runtime.send("action.request", action="react", source="reply", turn_id=1)

    def interrupt():
        frame()
        old = dict(runtime.engine.active)
        runtime.send("drag.begin")
        runtime.send("turn.phase", turn_id=1, phase="replying")
        frame()
        result = runtime.send("action.end", epoch=old["epoch"], token=old["token"], success=True)
        assert result["reason"] == "stale_action"
        assert widget.model.IsMotionFinished()
        runtime.send("drag.end")
        runtime.send("quiet.set", value=True)

    def quiet():
        frame("performance-native-quiet.png")
        assert widget.model.IsMotionFinished()
        assert runtime.engine.active is None
        runtime.send("quiet.set", value=False)
        runtime.send("turn.end", turn_id=1, outcome="cancelled")
        runtime.send("mood.set", mood="calm")
        saved["old_model"] = id(widget.model)
        saved["old_epoch"] = runtime.engine.epoch
        assert widget.request_reload()

    def reloaded():
        frame()
        assert id(widget.model) != saved["old_model"]
        assert runtime.engine.epoch > saved["old_epoch"]
        assert saved["old_model"] in releases
        track_native()
        old = saved["token"]
        runtime.send("action.request", action="greet", source="user")
        token = runtime.engine.active["token"]
        assert runtime.send("action.end", epoch=old["epoch"], token=old["token"], success=True)["reason"] == "stale_action"
        assert runtime.engine.active["token"] == token
        # Narrow mapping and then release: restore SDK default 0, not range min 1.
        data = load_profile("hiyori").data()
        data["parameters"] = {"ParamAngleZ": {"min": 1, "max": 2, "default": 1}}
        data["moods"] = {"calm": {}, "curious": {"ParamAngleZ": 2}}
        data["activities"], data["levels"] = {"idle": {}}, {}
        data["actions"] = {"greet": {"duration_ms": 1000, "motion": {"group": "MissingSmokeMotion", "index": 0}}}
        Profile(data)
        runtime.send("skin.change", profile=data)
        runtime.send("mood.set", mood="curious")
        frame()
        assert parameter_sets[-1] == ("ParamAngleZ", 2, 1.0)
        runtime.send("mood.set", mood="calm")
        frame()
        assert parameter_sets[-1] == ("ParamAngleZ", 0.0, 1.0)
        runtime.send("action.request", action="greet", source="user")

    def missing_motion():
        frame()
        assert runtime.engine.active is None
        assert any("MissingSmokeMotion" in message for message in notices)
        assert any(row["reason"] == "renderer_declined_action" for row in runtime.engine.history)

    steps = iter([(800, ready), (200, poses), (200, one_start), (2100, deadline),
                  (200, interrupt), (200, quiet), (500, reloaded), (200, missing_motion)])

    def finish():
        runtime.close()
        widget.stop()
        assert widget.model is None and not widget._timer.isActive()
        result = {"checks": checks, "failures": failures, "notices": notices,
                  "motion_starts": len(starts), "motion_stops": len(stops), "renderer_releases": len(releases)}
        (output / "performance-native-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        widget.hide()
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
                print("PASS " + action.__name__, flush=True)
                next_step()
            except Exception as exc:
                traceback.print_exc()
                failures.append(str(exc))
                finish()
        QTimer.singleShot(delay, run)

    widget.show()
    next_step()
    app.exec()
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
