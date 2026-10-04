"""Versioned, data-only skin capabilities. No Qt, configuration or persona imports."""
from copy import deepcopy
import json
import math
from pathlib import Path
import re

MOODS = frozenset({"calm", "happy", "sad", "curious", "worried"})
ACTIVITIES = frozenset({"idle", "thinking", "searching", "replying", "done", "error"})
LEVELS = frozenset({"frugal", "daily", "serious", "deep", "max"})
ACTIONS = frozenset({"greet", "acknowledge", "react", "drink_tea"})
PROFILE_DIR = Path(__file__).resolve().parents[1] / "assets/performance"


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def object_keys(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or value.keys() - set(required) - set(optional)):
        raise ValueError("字段缺失、类型错误或包含未支持的字段。")


def motion(value):
    object_keys(value, {"group", "index"})
    if (not isinstance(value["group"], str) or not 1 <= len(value["group"]) <= 80
            or any(ord(c) < 32 for c in value["group"])
            or not integer(value["index"], 0, 1000)):
        raise ValueError("动作组名或动作编号无效。")


class Profile:
    def __init__(self, data):
        object_keys(data, {"schema_version", "id", "label", "renderer", "parameters",
                           "moods", "activities", "levels", "actions"}, {"idle_motion"})
        if (type(data["schema_version"]) is not int or data["schema_version"] != 1
                or not isinstance(data["id"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", data["id"])
                or not isinstance(data["label"], str) or not 1 <= len(data["label"]) <= 80
                or data["renderer"] not in ("simulation", "live2d")):
            raise ValueError("不支持的皮套映射版本或身份字段。")
        params = data["parameters"]
        if not isinstance(params, dict) or len(params) > 64:
            raise ValueError("参数映射必须是最多 64 项的对象。")
        for key, spec in params.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", key):
                raise ValueError("参数标识无效。")
            object_keys(spec, {"min", "max", "default"})
            if (not all(number(v) for v in spec.values())
                    or not -1e6 <= spec["min"] < spec["max"] <= 1e6
                    or not spec["min"] <= spec["default"] <= spec["max"]):
                raise ValueError("参数范围或默认值无效。")
        for section, allowed, required in (("moods", MOODS, "calm"),
                                            ("activities", ACTIVITIES, "idle"),
                                            ("levels", LEVELS, None)):
            poses = data[section]
            if (not isinstance(poses, dict) or poses.keys() - allowed
                    or (required and required not in poses)):
                raise ValueError("姿态名称不在语义白名单内。")
            for pose in poses.values():
                if not isinstance(pose, dict) or pose.keys() - params.keys():
                    raise ValueError("姿态引用了未声明的参数。")
                for key, value in pose.items():
                    if not number(value) or not params[key]["min"] <= value <= params[key]["max"]:
                        raise ValueError("姿态参数超出映射范围。")
        actions = data["actions"]
        if not isinstance(actions, dict) or actions.keys() - ACTIONS:
            raise ValueError("动作名称不在语义白名单内。")
        for spec in actions.values():
            object_keys(spec, {"duration_ms"}, {"motion"})
            if not integer(spec["duration_ms"], 100, 30000):
                raise ValueError("短动作时长必须在 100–30000 毫秒内。")
            if "motion" in spec:
                motion(spec["motion"])
            elif data["renderer"] == "live2d":
                raise ValueError("Live2D 动作必须声明实际动作组与编号。")
        if "idle_motion" in data:
            motion(data["idle_motion"])
        self._data = deepcopy(data)

    @property
    def id(self):
        return self._data["id"]

    @property
    def label(self):
        return self._data["label"]

    def data(self):
        return deepcopy(self._data)

    def action(self, name):
        return deepcopy(self._data["actions"].get(name))

    def supports_mood(self, name):
        return name in self._data["moods"]

    def pose(self, state):
        if state["layer"] in ("closed", "unavailable", "suspended", "quiet", "drag", "action"):
            return {}
        result = dict(self._data["moods"].get(state["mood"], self._data["moods"]["calm"]))
        result.update(self._data["activities"].get(state["activity"], {}))
        if state["activity"] == "thinking":
            result.update(self._data["levels"].get(state["level"], {}))
        return result


def load_profile(name):
    if name not in ("hiyori", "simulator"):
        raise ValueError("此入口支持 hiyori 或 simulator 映射。")
    return Profile(json.loads((PROFILE_DIR / f"{name}.json").read_text(encoding="utf-8")))
