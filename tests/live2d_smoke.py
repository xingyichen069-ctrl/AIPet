"""Native OpenGL smoke test; no private data or model API requests."""
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from live2d_widget import Live2DWidget, make_transparent_gl
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

make_transparent_gl()
app = QApplication([])
widget = Live2DWidget(ROOT / 'hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json')
widget.resize(260, 380)
widget.show()
states = iter(['thinking', 'searching', 'replying', 'done', 'error', 'idle', 'quiet', 'resume'])
failures = []

def step():
    try:
        assert widget._ready, 'Model did not initialize'
        state = next(states)
        widget.set_quiet(state == 'quiet')
        widget.set_activity(state if state not in ('quiet', 'resume') else 'idle')
        widget._apply_activity()  # Direct call exposes API mismatch rather than hiding it in paintGL.
        widget.apply_thinking_pose('deep')
        frame = widget.grabFramebuffer()
        visible = sum(frame.pixelColor(x, y).alpha() > 0
                      for x in range(0, frame.width(), 8)
                      for y in range(0, frame.height(), 8))
        assert visible > 30, f'Empty frame for {state}'
        print(f'PASS {state}: {visible} visible samples', flush=True)
        QTimer.singleShot(450, step)
    except StopIteration:
        widget.stop()
        app.quit()
    except Exception as error:
        failures.append(str(error))
        traceback.print_exc()
        widget.stop()
        app.quit()

QTimer.singleShot(1200, step)
app.exec()
sys.exit(bool(failures))
