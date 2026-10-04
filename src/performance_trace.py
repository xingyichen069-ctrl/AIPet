"""Bounded, data-only recording and deterministic replay of presentation events."""
from copy import deepcopy
import json
from pathlib import Path
import re

from performance import Engine, SCHEMA_VERSION, validate_event
from performance_profile import Profile, integer, object_keys

TRACE_VERSION = 1
MAX_EVENTS = 10000
MAX_BYTES = 2 * 1024 * 1024
# Reserve room for final state, queue and the versioned envelope, so every
# accepted input can still be exported without silently dropping old events.
MAX_INPUT_BYTES = MAX_BYTES - 256 * 1024
MAX_TIME_MS = 24 * 60 * 60 * 1000


def _encoded(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


class Recording:
    def __init__(self, profile):
        self.initial_profile = profile.data()
        self.engine = Engine(profile)
        self.events = []
        self._bytes = len(_encoded(self.initial_profile))

    def dispatch(self, event, at_ms):
        event = validate_event(event)
        if not integer(at_ms, self.engine.now, MAX_TIME_MS):
            raise ValueError("记录时间必须递增，且不超过 24 小时。")
        row = {"at_ms": at_ms, "event": event}
        size = len(_encoded(row)) + 1
        if len(self.events) >= MAX_EVENTS or self._bytes + size > MAX_INPUT_BYTES:
            raise ValueError("记录已达到容量限制，请导出并新建记录；没有截断旧记录。")
        result = self.engine.dispatch(event, at_ms)
        self.events.append(row)
        self._bytes += size
        return result

    def advance(self, at_ms):
        if not integer(at_ms, self.engine.now, MAX_TIME_MS):
            raise ValueError("记录时间必须递增，且不超过 24 小时。")
        return self.engine.advance(at_ms)

    def export(self):
        data = {"trace_version": TRACE_VERSION, "controller_version": SCHEMA_VERSION,
                "initial_profile": deepcopy(self.initial_profile), "events": deepcopy(self.events),
                "end_ms": self.engine.now,
                "expected": {"state": self.engine.snapshot(), "audit_sha256": self.engine.audit_sha256}}
        if len(_encoded(data)) + 1 > MAX_BYTES:
            raise ValueError("记录超过文件大小限制，请勿将截断文件作为完整回放。")
        return data

    def save(self, path):
        # Validation precedes writing; a failed serialization never truncates an
        # existing destination. Replace a sibling temporary file atomically.
        import os
        import tempfile
        output = _encoded(self.export()) + b"\n"
        path = Path(path)
        name = None
        try:
            with tempfile.NamedTemporaryFile(prefix=path.name + ".", suffix=".tmp", dir=path.parent,
                                             delete=False) as handle:
                name = handle.name
                handle.write(output)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)


def validate_trace(data):
    object_keys(data, {"trace_version", "controller_version", "initial_profile", "events", "end_ms"}, {"expected"})
    if (type(data["trace_version"]) is not int or data["trace_version"] != TRACE_VERSION
            or type(data["controller_version"]) is not int or data["controller_version"] != SCHEMA_VERSION):
        raise ValueError("回放或控制器版本不匹配。")
    Profile(data["initial_profile"])
    events = data["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("事件列表无效或超过 10000 项。")
    last = 0
    input_bytes = len(_encoded(data["initial_profile"]))
    for row in events:
        object_keys(row, {"at_ms", "event"})
        if not integer(row["at_ms"], last, MAX_TIME_MS):
            raise ValueError("回放时间倒退或超限。")
        last = row["at_ms"]
        validate_event(row["event"])
        input_bytes += len(_encoded(row)) + 1
    if input_bytes > MAX_INPUT_BYTES:
        raise ValueError("事件及映射超过记录容量限制。")
    if not integer(data["end_ms"], last, MAX_TIME_MS):
        raise ValueError("回放结束时间早于事件或超限。")
    if "expected" in data:
        object_keys(data["expected"], {"state", "audit_sha256"})
        if (not isinstance(data["expected"]["state"], dict)
                or not isinstance(data["expected"]["audit_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", data["expected"]["audit_sha256"])):
            raise ValueError("预期结果格式无效。")
    if len(_encoded(data)) > MAX_BYTES:
        raise ValueError("回放超过 2 MiB。")
    return deepcopy(data)


def replay(data, until_ms=None):
    data = validate_trace(data)
    until = data["end_ms"] if until_ms is None else until_ms
    if not integer(until, 0, data["end_ms"]):
        raise ValueError("回放位置超出范围。")
    recording = Recording(Profile(data["initial_profile"]))
    for row in data["events"]:
        if row["at_ms"] > until:
            break
        recording.dispatch(row["event"], row["at_ms"])
    recording.advance(until)
    if until_ms is None and "expected" in data:
        expected = data["expected"]
        if (_encoded(expected["state"]) != _encoded(recording.engine.snapshot())
                or expected["audit_sha256"] != recording.engine.audit_sha256):
            raise ValueError("重新计算的状态或决策摘要与记录不一致。")
    return recording


def read_trace(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 包含重复字段。")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("JSON 包含非有限数。")
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("回放超过 2 MiB。")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
        return validate_trace(data)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("回放编码或嵌套层数无效。") from exc
