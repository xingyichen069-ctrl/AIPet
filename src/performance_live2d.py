"""Apply presentation snapshots inside a widget's active GL callback only.

Holds metadata, not a native model reference. Owner retains full control of
resource destruction and reports exceptions through its existing fallback.
"""
from performance_profile import Profile, number


class Live2DAdapter:
    def __init__(self, finished, notice):
        self.finished, self.notice = finished, notice
        self._binding = None
        self._motion_key = None
        self._controlled = set()
        self._limits = {}
        self._motions = {}
        self._profile = None

    def _bind(self, model, profile, epoch):
        self._profile = Profile(profile.data())
        self._binding = (id(model), profile.id, epoch)
        self._motion_key = None
        self._controlled.clear()
        self._limits.clear()
        native = {}
        for index in range(model.GetParameterCount()):
            param = model.GetParameter(index)
            native[param.id] = param
        for name, spec in profile.data()["parameters"].items():
            param = native.get(name)
            if (param is None or not all(number(v) for v in (param.min, param.max, param.default))
                    or param.min > param.max):
                self.notice(f"未找到可用参数：{name}")
                continue
            low, high = max(spec["min"], param.min), min(spec["max"], param.max)
            if low > high:
                self.notice(f"参数范围不兼容：{name}")
                continue
            # A released parameter belongs to the model again. Its native
            # default need not lie inside this profile's narrower pose range.
            self._limits[name] = (low, high, max(param.min, min(param.max, param.default)))
        self._motions = model.GetMotionGroups()

    def _restore(self, model, names):
        for name in names:
            if name in self._limits:
                model.SetParameterValue(name, self._limits[name][2], 1.0)

    def before_update(self, model, state, profile):
        binding = (id(model), profile.id, state["epoch"])
        if self._binding != binding:
            # On a profile/generation change within the same native model,
            # restore its previously controlled parameters before rebinding.
            if self._binding and self._binding[0] == id(model):
                self._restore(model, self._controlled)
            self._bind(model, profile, state["epoch"])
        action = state["action"] if state["layer"] == "action" else None
        # Use tagged keys: Python True equals 1, which is also a valid token.
        key = (state["epoch"], "action", action["token"]) if action else (
            state["epoch"], "base" if state["layer"] in ("idle", "mood", "activity") else "stopped")
        if key == self._motion_key:
            return
        self._motion_key = key
        model.StopAllMotions()
        data = profile.data()
        spec = profile.action(action["name"]) if action else None
        target = spec.get("motion") if spec else data.get("idle_motion") if key[1] == "base" else None
        if not target:
            if action:
                self.notice("当前原生模型没有此动作映射。")
                self.finished(action["epoch"], action["token"], False)
            return
        count = self._motions.get(target["group"], 0)
        if type(count) is not int or target["index"] >= count:
            self.notice(f"当前模型不支持动作组/编号：{target['group']} / {target['index']}")
            if action:
                self.finished(action["epoch"], action["token"], False)
            return
        callback = None
        if action:
            epoch, token = action["epoch"], action["token"]
            callback = lambda *unused: self.finished(epoch, token, True)
        model.StartMotion(target["group"], target["index"], 3, onFinishMotionHandler=callback)

    def after_update(self, model, state, profile):
        targets = {name: value for name, value in profile.pose(state).items() if name in self._limits}
        self._restore(model, self._controlled - targets.keys())
        for name, value in targets.items():
            low, high, _ = self._limits[name]
            # Blending with an out-of-range value left by a native motion can
            # escape the declared range even when the target is clamped.
            model.SetParameterValue(name, max(low, min(high, value)), 1.0)
        self._controlled = set(targets)

    def reset(self):
        self._binding = self._motion_key = None
        self._controlled.clear()
        self._limits.clear()
        self._motions.clear()
