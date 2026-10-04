"""Deterministic presentation scheduler. Time is explicit; no IO, threads or Qt.

Lifecycle/work events are trusted application inputs. reply_intent() is the
restricted boundary for a future language-side producer; it accepts no window,
parameter, path, priority or code instructions.
"""
from collections import deque
from copy import deepcopy
import hashlib
import json

from performance_profile import (ACTIONS, LEVELS, MOODS, Profile, integer, object_keys)

SCHEMA_VERSION = 1
QUEUE_LIMIT = 2
QUEUE_TTL_MS = 2000
DRAG_LEASE_MS = 15000
WORK_LEASE_MS = 120000
FEEDBACK_MS = 1800
PRIORITY = {"user": 60, "reply": 50, "system": 20}


def validate_event(event):
    """Return a detached canonical event; never keep arbitrary payload fields."""
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        raise ValueError("事件必须是带 kind 的对象。")
    kind = event["kind"]
    schema = {
        "turn.begin": ({"turn_id"}, set()),
        "turn.phase": ({"turn_id", "phase"}, set()),
        "turn.end": ({"turn_id", "outcome"}, set()),
        "mood.set": ({"mood"}, {"turn_id"}),
        "action.request": ({"action", "source"}, {"turn_id"}),
        "action.end": ({"epoch", "token", "success"}, set()),
        "quiet.set": ({"value"}, set()),
        "suspend.set": ({"value"}, set()),
        "renderer.set": ({"value"}, set()),
        "skin.change": ({"profile"}, set()),
        "level.set": ({"level"}, set()),
        "drag.begin": (set(), set()), "drag.end": (set(), set()), "close": (set(), set()),
    }
    if kind not in schema:
        raise ValueError("事件种类不在白名单内。")
    required, optional = schema[kind]
    object_keys(event, required | {"kind"}, optional)
    if "turn_id" in event and not integer(event["turn_id"], 1, 2**53 - 1):
        raise ValueError("轮次必须是正整数。")
    if kind == "turn.phase" and event["phase"] not in ("thinking", "searching", "replying"):
        raise ValueError("不支持的工作状态。")
    if kind == "turn.end" and event["outcome"] not in ("complete", "error", "cancelled"):
        raise ValueError("不支持的结束原因。")
    if kind == "mood.set" and (not isinstance(event["mood"], str) or event["mood"] not in MOODS):
        raise ValueError("心情不在白名单内。")
    if kind == "level.set" and (not isinstance(event["level"], str) or event["level"] not in LEVELS):
        raise ValueError("思考档位无效。")
    if kind == "action.request":
        if (not isinstance(event["action"], str) or event["action"] not in ACTIONS
                or not isinstance(event["source"], str) or event["source"] not in PRIORITY):
            raise ValueError("动作或来源不在白名单内。")
        if event["source"] == "reply" and "turn_id" not in event:
            raise ValueError("回复动作必须绑定轮次。")
        if event["source"] == "user" and "turn_id" in event:
            raise ValueError("用户直接动作不绑定回复轮次。")
    if kind == "action.end":
        if not integer(event["epoch"], 0, 2**53 - 1) or not integer(event["token"], 1, 2**53 - 1):
            raise ValueError("动作完成通知缺少有效的皮套代数与令牌。")
    for key in ("value", "success"):
        if key in event and type(event[key]) is not bool:
            raise ValueError("开关必须为布尔值。")
    if kind == "skin.change":
        Profile(event["profile"])
    return deepcopy(event)


class Engine:
    def __init__(self, profile, *, history_limit=512):
        if not integer(history_limit, 1, 10000):
            raise ValueError("可见决策历史上限必须在 1–10000 项之间。")
        self.profile = Profile(profile.data())
        self.now = 0
        self.turn_id = 0
        self.turn_open = False
        self.activity = "idle"
        self.mood = "calm"
        self.level = "daily"
        self.quiet = self.suspended = self.closed = False
        self.available = True
        self.epoch = self._serial = 0
        self.active = None
        self.queue = []
        self.drag_until = self.work_until = self.feedback_until = None
        self.history = deque(maxlen=history_limit)
        self.history_dropped = 0
        self._reply_actions = 0
        self._audit = hashlib.sha256()

    @property
    def audit_sha256(self):
        return self._audit.hexdigest()

    def snapshot(self):
        layer = ("closed" if self.closed else "unavailable" if not self.available else
                 "suspended" if self.suspended else "quiet" if self.quiet else
                 "drag" if self.drag_until is not None else "action" if self.active else
                 "activity" if self.activity != "idle" else "mood" if self.mood != "calm" else "idle")
        return {"at_ms": self.now, "skin": self.profile.id, "epoch": self.epoch,
                "turn_id": self.turn_id, "turn_open": self.turn_open,
                "activity": self.activity, "mood": self.mood,
                "visible_mood": self.mood if self.profile.supports_mood(self.mood) else "calm",
                "level": self.level, "quiet": self.quiet, "suspended": self.suspended,
                "available": self.available, "closed": self.closed, "layer": layer,
                "action": deepcopy(self.active), "queue": deepcopy(self.queue)}

    def _record(self, kind, accepted, reason):
        row = {"at_ms": self.now, "kind": kind, "accepted": accepted,
               "reason": reason, "state": self.snapshot()}
        if len(self.history) == self.history.maxlen:
            self.history_dropped += 1
        self.history.append(row)
        self._audit.update((json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                       separators=(",", ":")) + "\n").encode("utf-8"))
        return deepcopy(row)

    def _blocked(self):
        return self.closed or not self.available or self.suspended or self.quiet or self.drag_until is not None

    def _clear_actions(self):
        self.active = None
        self.queue.clear()

    def _clear_reply_actions(self):
        if self.active and self.active["source"] != "user":
            self.active = None
        self.queue = [item for item in self.queue if item["source"] == "user"]

    def _start(self, item):
        item = dict(item)
        item.pop("expires_ms", None)
        item["started_ms"] = self.now
        item["ends_ms"] = self.now + self.profile.action(item["name"])["duration_ms"]
        self.active = item

    def _start_waiting(self):
        if not self.active and self.queue and not self._blocked():
            self.queue.sort(key=lambda item: (-item["priority"], item["token"]))
            self._start(self.queue.pop(0))
            self._record("timer", True, "queued_action_started")

    def _request(self, name, source, turn_id=None):
        if not self.profile.action(name):
            return False, "unsupported_action"
        if self._blocked():
            return False, "blocked"
        if ((self.active and self.active["name"] == name)
                or any(item["name"] == name for item in self.queue)):
            return False, "duplicate_action"
        if source == "reply" and self._reply_actions >= 4:
            return False, "reply_action_budget"
        priority = PRIORITY[source]
        if self.active and priority <= self.active["priority"] and len(self.queue) >= QUEUE_LIMIT:
            return False, "queue_full"
        self._serial += 1
        item = {"name": name, "source": source, "turn_id": turn_id,
                "priority": priority, "token": self._serial, "epoch": self.epoch}
        if source == "reply":
            self._reply_actions += 1
        if not self.active or priority > self.active["priority"]:
            reason = "action_preempted" if self.active else "action_started"
            self._start(item)
        else:
            item["expires_ms"] = self.now + QUEUE_TTL_MS
            self.queue.append(item)
            reason = "action_queued"
        return True, reason

    def advance(self, at_ms):
        if not integer(at_ms, self.now, 2**53 - 1):
            raise ValueError("时间必须是单调递增的整数毫秒。")
        # Resolve scheduled boundaries chronologically, including on a large
        # clock jump. Replay does not depend on the UI's timer frequency.
        while True:
            deadlines = [v for v in (self.drag_until, self.work_until, self.feedback_until,
                         self.active["ends_ms"] if self.active else None) if v is not None]
            deadlines.extend(item["expires_ms"] for item in self.queue)
            if not deadlines or min(deadlines) > at_ms:
                break
            self.now = min(deadlines)
            before = len(self.queue)
            self.queue = [item for item in self.queue if item["expires_ms"] > self.now]
            if before != len(self.queue):
                self._record("timer", True, "queue_expired")
            if self.work_until is not None and self.work_until <= self.now:
                self.work_until = None
                self.turn_open = False
                self.activity = "error"
                self.feedback_until = self.now + FEEDBACK_MS
                self._clear_reply_actions()
                self._record("timer", True, "turn_timed_out")
            if self.feedback_until is not None and self.feedback_until <= self.now:
                self.feedback_until = None
                self.activity = "idle"
                self._record("timer", True, "feedback_elapsed")
            if self.drag_until is not None and self.drag_until <= self.now:
                self.drag_until = None
                self._record("timer", True, "drag_lease_elapsed")
            if self.active and self.active["ends_ms"] <= self.now:
                self.active = None
                self._record("timer", True, "action_elapsed")
            self._start_waiting()
        self.now = at_ms
        return self.snapshot()

    def dispatch(self, raw, at_ms):
        event = validate_event(raw)
        self.advance(at_ms)
        kind = event["kind"]
        if self.closed:
            return self._record(kind, False, "closed")
        turn = event.get("turn_id")
        if kind == "turn.begin":
            if turn <= self.turn_id:
                return self._record(kind, False, "stale_turn")
        elif turn is not None and (turn != self.turn_id or not self.turn_open):
            return self._record(kind, False, "stale_turn")
        accepted, reason = True, "applied"
        if kind == "turn.begin":
            self._clear_reply_actions()
            self.turn_id, self.turn_open, self.activity = turn, True, "thinking"
            self._reply_actions = 0
            self.work_until = self.now + WORK_LEASE_MS
            self.feedback_until = None
        elif kind == "turn.phase":
            self.activity = event["phase"]
            self.work_until = self.now + WORK_LEASE_MS
        elif kind == "turn.end":
            self.turn_open = False
            self.work_until = None
            outcome = event["outcome"]
            self.activity = {"complete": "done", "error": "error", "cancelled": "idle"}[outcome]
            self.feedback_until = None if outcome == "cancelled" else self.now + FEEDBACK_MS
            if outcome == "cancelled":
                self._clear_reply_actions()
            else:
                _, feedback = self._request("acknowledge" if outcome == "complete" else "react", "system", turn)
                reason = "turn_ended:" + feedback
        elif kind == "mood.set":
            if self.mood != event["mood"]:
                self._clear_actions()
            self.mood = event["mood"]
            if not self.profile.supports_mood(self.mood):
                reason = "mood_unavailable"
        elif kind == "action.request":
            accepted, reason = self._request(event["action"], event["source"], turn)
        elif kind == "action.end":
            if not self.active or (event["epoch"], event["token"]) != (self.active["epoch"], self.active["token"]):
                return self._record(kind, False, "stale_action")
            self.active = None
            reason = "action_finished" if event["success"] else "renderer_declined_action"
        elif kind in ("quiet.set", "suspend.set"):
            setattr(self, "quiet" if kind == "quiet.set" else "suspended", event["value"])
            if event["value"]:
                self._clear_actions()
                self.drag_until = None
        elif kind == "renderer.set":
            # A ready signal can refer to a new model in the same Qt widget.
            self.available = event["value"]
            self.epoch += 1
            self._clear_actions()
            self.drag_until = None
        elif kind == "skin.change":
            self.profile = Profile(event["profile"])
            self.epoch += 1
            self._clear_actions()
            self.drag_until = None
        elif kind == "level.set":
            self.level = event["level"]
        elif kind == "drag.begin":
            if self.drag_until is None:
                self._clear_actions()
                self.drag_until = self.now + DRAG_LEASE_MS
        elif kind == "drag.end":
            self.drag_until = None
        elif kind == "close":
            self.closed = True
            self.turn_open = False
            self._clear_actions()
            self.drag_until = self.work_until = self.feedback_until = None
            self.activity = "idle"
        self._start_waiting()
        return self._record(kind, accepted, reason)

    def reply_intent(self, turn_id, payload, at_ms):
        """Whitelist-only future language bridge; callers cannot supply source.

        Validate the entire payload before applying anything. Deliberately does
        not classify sentiment from text or call an external language model.
        """
        object_keys(payload, set(), {"mood", "action"})
        if not payload:
            raise ValueError("意图至少包含 mood 或 action。")
        events = []
        if "mood" in payload:
            events.append({"kind": "mood.set", "mood": payload["mood"], "turn_id": turn_id})
        if "action" in payload:
            events.append({"kind": "action.request", "action": payload["action"],
                           "source": "reply", "turn_id": turn_id})
        events = [validate_event(event) for event in events]
        return [self.dispatch(event, at_ms) for event in events]
