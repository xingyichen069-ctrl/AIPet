"""Public synthetic demonstrations; no chat text, model API or private data."""
from performance_profile import load_profile
from performance_trace import Recording

SCENARIOS = {
    "reply": "一轮正常回复",
    "interrupt": "喝茶被拖拽打断",
    "late_reply": "旧回复迟到",
    "queue": "连续动作与等待过期",
    "skin": "换皮套与旧动作回调",
    "quiet": "安静、暂停与恢复",
}


def scenario(name):
    if name not in SCENARIOS:
        raise ValueError("不存在的示例场景。")
    recording = Recording(load_profile("simulator"))
    def send(at, kind, **fields):
        recording.dispatch({"kind": kind, **fields}, at)
    send(0, "mood.set", mood="happy")
    if name == "reply":
        send(300, "turn.begin", turn_id=1)
        send(1300, "turn.phase", turn_id=1, phase="searching")
        send(2500, "turn.phase", turn_id=1, phase="replying")
        send(4200, "turn.end", turn_id=1, outcome="complete")
        end = 6500
    elif name == "interrupt":
        send(200, "action.request", action="drink_tea", source="user")
        send(2000, "drag.begin")
        send(2500, "turn.begin", turn_id=1)
        send(3300, "drag.end")
        send(4200, "turn.phase", turn_id=1, phase="replying")
        send(5000, "turn.end", turn_id=1, outcome="complete")
        end = 7200
    elif name == "late_reply":
        send(200, "turn.begin", turn_id=1)
        send(1000, "turn.end", turn_id=1, outcome="cancelled")
        send(1600, "turn.begin", turn_id=2)
        send(2400, "mood.set", mood="sad", turn_id=1)
        send(2800, "turn.end", turn_id=1, outcome="complete")
        send(3500, "turn.phase", turn_id=2, phase="replying")
        send(4700, "turn.end", turn_id=2, outcome="complete")
        end = 6800
    elif name == "queue":
        send(200, "action.request", action="drink_tea", source="user")
        send(600, "action.request", action="greet", source="user")
        send(900, "action.request", action="acknowledge", source="user")
        send(1100, "action.request", action="react", source="user")
        send(1400, "action.request", action="drink_tea", source="user")
        end = 10800
    elif name == "skin":
        send(200, "action.request", action="drink_tea", source="user")
        send(2100, "skin.change", profile=load_profile("hiyori").data())
        send(2900, "action.request", action="drink_tea", source="user")
        send(3600, "action.request", action="greet", source="user")
        send(4200, "action.end", epoch=0, token=1, success=True)
        end = 6200
    else:
        send(200, "action.request", action="drink_tea", source="user")
        send(1800, "quiet.set", value=True)
        send(2400, "action.request", action="greet", source="user")
        send(3100, "quiet.set", value=False)
        send(3800, "action.request", action="greet", source="user")
        send(4100, "suspend.set", value=True)
        send(5100, "suspend.set", value=False)
        end = 6200
    recording.advance(end)
    return recording.export()
