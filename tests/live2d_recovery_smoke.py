"""Native desktop recovery smoke test in a disposable public-fixture install.

Run with Windows Python and a working desktop/OpenGL driver. Opens only its own
pet window; never launches the normal app entry point or requests a model API.
Logs/screenshots go to work/verification; no personal installation is imported.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def isolated_run():
    work = PROJECT / "work"
    output = work / "verification"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live2d-smoke-", dir=work) as name:
        root = Path(name)
        for directory in ("src", "themes", "assets", "persona_defaults"):
            shutil.copytree(PROJECT / directory, root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "data").mkdir()
        for config in ("config", "thinking", "secrets"):
            filename = config + ".example.json"
            shutil.copyfile(PROJECT / "data" / filename, root / "data" / filename)
        cfg = json.loads((root / "data/config.example.json").read_text(encoding="utf-8-sig"))
        cfg["live2d"]["enabled"] = True
        cfg["live2d"]["model"] = "missing.model3.json"
        (root / "data/config.json").write_text(json.dumps(cfg), encoding="utf-8")
        env = dict(os.environ)
        for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                    "AIPET_HOME", "QT_QPA_PLATFORM"):
            env.pop(key, None)
        env.update(AIPET_HOME=str(root), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                               "--fixture", str(root), str(output)],
                              cwd=root, env=env, timeout=45).returncode


def smoke(root, output):
    sys.path.insert(0, str(root / "src"))
    import live2d_widget as L  # SDK initialization must precede QApplication.
    import pet as P
    from PySide6.QtCore import QPointF, QTimer
    from PySide6.QtGui import QOpenGLContext
    from PySide6.QtWidgets import QApplication

    assert P.ROOT == root and P.M.ROOT == root, "Fixture isolation failed"
    assert L.HAS_LIVE2D, L.LIVE2D_ERROR
    L.make_transparent_gl()
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    pet = P.PetWindow()
    pet.move(80, 100)
    pet.show()
    model_path = PROJECT / "hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json"
    failures, releases, checks = [], [], []
    saved = {}
    original_init = L.live2d.glInit
    original_load = L.Live2DWidget._load_model
    original_destroy = L.Live2DWidget._destroy_renderer
    contexts = {}

    def tracked_load(widget, path):
        model = original_load(widget, path)
        contexts[id(model)] = widget.context()
        return model

    def tracked_destroy(model):
        if model is not None and id(model) in contexts:
            if QOpenGLContext.currentContext() != contexts.pop(id(model)):
                failures.append("Renderer released outside its owning GL context")
            releases.append(id(model))
        original_destroy(model)

    L.Live2DWidget._load_model = tracked_load
    L.Live2DWidget._destroy_renderer = staticmethod(tracked_destroy)

    def check(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    def visible(image):
        return sum(image.pixelColor(x, y).alpha() > 0
                   for x in range(0, image.width(), 8)
                   for y in range(0, image.height(), 8))

    def static_frame(name):
        assert pet.gl is None, "Failed GL child still active"
        image = pet.grab().toImage()
        assert visible(image) > 30, "Static fallback is invisible"
        assert any(pet._body_at(QPointF(x, y)) for x in range(0, pet.width(), 8)
                   for y in range(0, pet.height(), 8)), "Fallback has no clickable body"
        assert image.save(str(output / name))

    def healthy_frame(name=None):
        assert pet.gl and pet.gl._ready and pet.gl._has_frame, "Model not ready"
        frame = pet.gl.grabFramebuffer()
        assert visible(frame) > 30, "Live2D frame is invisible"
        if name:
            assert frame.save(str(output / name))

    def fail_init():
        raise RuntimeError("injected GL initialization failure")

    def initial_static():
        static_frame("live2d-fallback-initial.png")
        check("missing entry -> visible and clickable static pet")
        P.L2D_CFG["model"] = str(model_path)
        L.live2d.glInit = fail_init
        pet._reload_model()

    def failed_init():
        static_frame("live2d-fallback-init.png")
        check("GL initialization exception -> static pet")
        L.live2d.glInit = original_init
        pet._reload_model()

    def recovered():
        healthy_frame("live2d-recovered.png")
        check("retry creates a working native renderer")
        saved["model"] = id(pet.gl.model)
        saved["widget"] = pet.gl
        P.L2D_CFG["model"] = "still-missing.model3.json"
        pet._reload_model()

    def failed_replacement():
        healthy_frame()
        assert pet.gl is saved["widget"] and id(pet.gl.model) == saved["model"]
        check("missing replacement preserves the working native model")
        P.L2D_CFG["model"] = str(model_path)
        def bad_resize(widget, path):
            model = tracked_load(widget, path)
            def fail(*args):
                raise RuntimeError("injected candidate resize failure")
            model.Resize = fail
            return model
        L.Live2DWidget._load_model = bad_resize
        pet._reload_model()

    def failed_resize():
        healthy_frame()
        assert id(pet.gl.model) == saved["model"]
        check("fully loaded candidate resize failure preserves old model and releases candidate")
        L.Live2DWidget._load_model = tracked_load
        pet._reload_model()

    def successful_replacement():
        healthy_frame()
        assert id(pet.gl.model) != saved["model"]
        assert saved["model"] in releases
        check("successful hot reload releases old renderer in its GL context")
        pet._toggle_topmost()

    def after_window_change():
        healthy_frame()
        check("window flag change leaves a visible working renderer")
        def fail():
            raise RuntimeError("injected running Draw failure")
        pet.gl.model.Draw = fail
        pet.gl.update()

    def running_failure():
        static_frame("live2d-fallback-draw.png")
        check("running Draw exception -> visible and clickable static pet")
        pet._reload_model()

    def second_recovery():
        healthy_frame("live2d-recovered-again.png")
        check("retry after a running failure succeeds")
        # A real Qt GL child whose initialization callback never produces a
        # model exercises the watchdog independently of exception handling.
        pet._fallback_live2d("smoke test context reset")
        class NoModel(L.Live2DWidget):
            STARTUP_TIMEOUT_MS = 200
            def initializeGL(self):
                pass
        P.Live2DWidget = NoModel
        pet._reload_model()

    def timed_out():
        static_frame("live2d-fallback-timeout.png")
        assert "未能建立 OpenGL" in pet._live2d_error
        check("no initialized model -> watchdog -> static pet")
        P.Live2DWidget = L.Live2DWidget
        pet._reload_model()

    def final_recovery():
        healthy_frame()
        check("retry after watchdog failure succeeds")

    steps = iter([initial_static, failed_init, recovered, failed_replacement,
                  failed_resize, successful_replacement, after_window_change,
                  running_failure, second_recovery, timed_out, final_recovery])

    def finish():
        pet._stop_live2d()
        pet.companion.timer.stop()
        if contexts:
            failures.append("Native model resources still held after stop")
        for window in app.topLevelWidgets():
            window.hide()
        result = {"checks": checks, "renderer_releases": len(releases), "failures": failures}
        (output / "live2d-recovery-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        app.quit()

    def step():
        try:
            action = next(steps)
            action()
            QTimer.singleShot(650, step)
        except StopIteration:
            finish()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failures.append(str(exc))
            finish()

    QTimer.singleShot(650, step)
    app.exec()
    return int(bool(failures))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--fixture":
        raise SystemExit(smoke(Path(sys.argv[2]), Path(sys.argv[3])))
    raise SystemExit(isolated_run())
