"""Deterministic scheduler and renderer contracts, without Qt/native models."""
from copy import deepcopy
import random
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from performance import Engine, QUEUE_LIMIT, WORK_LEASE_MS, validate_event
from performance_profile import Profile, load_profile
from performance_live2d import Live2DAdapter


class Control(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(load_profile("simulator"))

    def send(self, kind, at=0, **fields):
        return self.engine.dispatch({"kind": kind, **fields}, at)

    def action(self, name="drink_tea", at=0, source="user", **fields):
        return self.send("action.request", at, action=name, source=source, **fields)

    def test_work_mood_and_short_action_are_separate_and_restore_latest_underlay(self):
        self.send("mood.set", mood="happy")
        self.send("turn.begin", turn_id=1)
        self.action()
        self.send("turn.phase", 500, turn_id=1, phase="replying")
        self.assertEqual(self.engine.snapshot()["layer"], "action")
        state = self.engine.advance(9800)
        self.assertEqual((state["mood"], state["activity"], state["layer"]), ("happy", "replying", "activity"))

    def test_mood_change_cancels_active_and_queued_actions(self):
        self.action()
        self.action("greet", 10)
        self.send("mood.set", 20, mood="sad")
        self.assertIsNone(self.engine.active)
        self.assertEqual(self.engine.queue, [])
        self.assertEqual(self.engine.advance(10000)["mood"], "sad")

    def test_drag_cancels_actions_and_restores_new_work_without_resuming_half_action(self):
        self.send("turn.begin", turn_id=1)
        self.action()
        self.send("drag.begin", 100)
        self.assertEqual(self.action("greet", 110)["reason"], "blocked")
        self.send("turn.phase", 150, turn_id=1, phase="replying")
        self.send("drag.end", 200)
        self.assertEqual(self.engine.snapshot()["layer"], "activity")
        self.assertIsNone(self.engine.active)
        self.send("drag.begin", 300)
        self.assertEqual(self.engine.advance(15300)["layer"], "activity")

    def test_duplicate_drag_begin_does_not_extend_a_stuck_drag_forever(self):
        self.send("drag.begin")
        self.send("drag.begin", 14999)
        self.assertEqual(self.engine.advance(15000)["layer"], "idle")

    def test_quiet_suspension_and_renderer_failure_suppress_without_replaying_old_actions(self):
        for kind in ("quiet.set", "suspend.set", "renderer.set"):
            with self.subTest(kind=kind):
                self.engine = Engine(load_profile("simulator"))
                self.send("mood.set", mood="curious")
                self.action()
                blocked = kind != "renderer.set"
                self.send(kind, 100, value=blocked)
                self.assertIsNone(self.engine.active)
                self.assertFalse(self.action("greet", 200)["accepted"])
                self.send(kind, 300, value=not blocked)
                self.assertEqual(self.engine.snapshot()["layer"], "mood")
                self.assertIsNone(self.engine.active)

    def test_priority_preemption_queue_fifo_capacity_duplicates_and_expiry(self):
        self.send("turn.begin", turn_id=1)
        self.action("react", source="reply", turn_id=1)
        self.assertEqual(self.action("drink_tea")["reason"], "action_preempted")
        self.action("greet", 10)
        self.action("acknowledge", 20)
        self.assertEqual(len(self.engine.queue), QUEUE_LIMIT)
        self.assertEqual(self.action("react", 30)["reason"], "queue_full")
        self.assertEqual(self.action("drink_tea", 40)["reason"], "duplicate_action")
        self.engine.advance(2100)
        self.assertEqual(self.engine.queue, [])
        self.assertEqual(self.engine.active["name"], "drink_tea")

    def test_waiting_action_starts_in_fifo_order_and_expires_before_tied_completion(self):
        self.action("acknowledge")
        self.action("greet", 100)
        self.action("react", 100)
        self.engine.advance(1200)
        self.assertEqual(self.engine.active["name"], "greet")
        self.engine.advance(2800)
        self.assertIsNone(self.engine.active)  # react expired at 2100, never runs late

    def test_large_time_jump_is_equivalent_to_fine_ticks(self):
        self.action("acknowledge")
        self.action("greet", 100)
        other = Engine(load_profile("simulator"))
        other.dispatch({"kind": "action.request", "action": "acknowledge", "source": "user"}, 0)
        other.dispatch({"kind": "action.request", "action": "greet", "source": "user"}, 100)
        for at in range(200, 5001, 100):
            other.advance(at)
        self.assertEqual(self.engine.advance(5000), other.snapshot())
        self.assertEqual(list(self.engine.history), list(other.history))

    def test_cancel_new_round_and_late_chunks_or_intents_cannot_override_current_turn(self):
        self.send("turn.begin", turn_id=1)
        self.engine.reply_intent(1, {"mood": "happy", "action": "greet"}, 10)
        self.send("turn.end", 20, turn_id=1, outcome="cancelled")
        self.assertIsNone(self.engine.active)
        self.send("turn.begin", 30, turn_id=2)
        for kind, payload in (("turn.phase", {"phase": "replying"}),
                              ("turn.end", {"outcome": "complete"}),
                              ("mood.set", {"mood": "sad"}),
                              ("action.request", {"action": "react", "source": "reply"})):
            result = self.send(kind, 40, turn_id=1, **payload)
            self.assertEqual(result["reason"], "stale_turn")
        self.assertEqual(self.engine.activity, "thinking")
        self.assertEqual(self.engine.mood, "happy")
        self.assertFalse(self.send("turn.begin", 50, turn_id=1)["accepted"])

    def test_end_is_idempotent_and_old_completion_timer_does_not_reset_new_turn(self):
        self.send("turn.begin", turn_id=1)
        self.send("turn.end", 100, turn_id=1, outcome="complete")
        self.assertFalse(self.send("turn.end", 101, turn_id=1, outcome="complete")["accepted"])
        self.send("turn.begin", 500, turn_id=2)
        self.assertEqual(self.engine.advance(1900)["activity"], "thinking")

    def test_work_lease_refresh_and_expiry_do_not_revive_a_dead_turn(self):
        self.send("turn.begin", turn_id=1)
        self.send("turn.phase", 119000, turn_id=1, phase="thinking")
        self.assertTrue(self.engine.advance(120000)["turn_open"])
        state = self.engine.advance(119000 + WORK_LEASE_MS)
        self.assertFalse(state["turn_open"])
        self.assertEqual(state["activity"], "error")
        self.assertFalse(self.send("turn.phase", self.engine.now, turn_id=1, phase="replying")["accepted"])

    def test_reply_budget_is_per_turn_and_user_requests_do_not_consume_it(self):
        self.send("turn.begin", turn_id=1)
        for at in (0, 2000, 4000, 6000):
            self.assertTrue(self.action("greet", at, source="reply", turn_id=1)["accepted"])
        self.assertEqual(self.action("greet", 8000, source="reply", turn_id=1)["reason"], "reply_action_budget")
        self.assertTrue(self.action("greet", 8000)["accepted"])
        self.send("turn.begin", 10000, turn_id=2)
        self.assertTrue(self.action("greet", 10000, source="reply", turn_id=2)["accepted"])

    def test_new_turn_preserves_explicit_user_action_but_clears_old_automatic_queue(self):
        self.send("turn.begin", turn_id=1)
        self.action("drink_tea")
        self.action("greet", 10, source="reply", turn_id=1)
        self.action("react", 20, source="system")
        self.send("turn.begin", 30, turn_id=2)
        self.assertEqual(self.engine.active["name"], "drink_tea")
        self.assertEqual(self.engine.queue, [])

    def test_queue_expiry_wins_when_tied_with_active_action_deadline(self):
        data = load_profile("simulator").data()
        data["actions"]["greet"]["duration_ms"] = 2000
        self.engine = Engine(Profile(data))
        self.action("greet")
        self.action("react")
        self.engine.advance(2000)
        self.assertIsNone(self.engine.active)
        self.assertEqual(self.engine.queue, [])
        self.assertEqual([r["reason"] for r in list(self.engine.history)[-2:]], ["queue_expired", "action_elapsed"])

    def test_skin_epoch_and_action_tokens_reject_old_native_callbacks(self):
        self.action()
        old = deepcopy(self.engine.active)
        self.send("renderer.set", 100, value=True)
        self.action("greet", 200)
        result = self.send("action.end", 300, epoch=old["epoch"], token=old["token"], success=True)
        self.assertEqual(result["reason"], "stale_action")
        self.send("skin.change", 400, profile=load_profile("hiyori").data())
        self.assertIsNone(self.engine.active)
        self.assertEqual(self.action("drink_tea", 500)["reason"], "unsupported_action")

    def test_missing_mood_is_reported_while_preserving_semantic_mood(self):
        data = load_profile("simulator").data()
        del data["moods"]["sad"]
        self.send("skin.change", profile=data)
        result = self.send("mood.set", mood="sad")
        self.assertEqual(result["reason"], "mood_unavailable")
        self.assertEqual(result["state"]["mood"], "sad")
        self.assertEqual(result["state"]["visible_mood"], "calm")

    def test_untrusted_reply_cannot_inject_controls_and_is_validated_atomically(self):
        self.send("turn.begin", turn_id=1)
        for payload in ({"mood": "happy", "action": "shell"}, {"priority": 999},
                        {"kind": "close"}, {"mood": []}, {"action": "../file"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.engine.reply_intent(1, payload, 10)
            self.assertEqual(self.engine.mood, "calm")
            self.assertEqual(self.engine.now, 0)

    def test_malformed_events_and_reversed_clock_do_not_mutate_state(self):
        for event in ({"kind": "close", "text": "private"},
                      {"kind": "quiet.set", "value": 1},
                      {"kind": "turn.begin", "turn_id": True},
                      {"kind": "action.request", "action": [], "source": "user"},
                      {"kind": "action.request", "action": "greet", "source": "reply"}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.engine.dispatch(event, 10)
        self.engine.advance(100)
        with self.assertRaises(ValueError):
            self.send("mood.set", 99, mood="happy")
        self.assertEqual(self.engine.mood, "calm")

    def test_close_is_terminal_and_history_is_bounded(self):
        self.action()
        self.send("close", 100)
        for at in range(101, 701):
            self.send("drag.begin", at)
        self.assertEqual(self.engine.snapshot()["layer"], "closed")
        self.assertIsNone(self.engine.active)
        self.assertEqual(len(self.engine.history), 512)
        self.assertGreater(self.engine.history_dropped, 0)

    def test_seeded_event_sequences_preserve_queue_and_blocking_invariants(self):
        rng = random.Random(20261004)
        for at in range(0, 100000, 100):
            kind = rng.choice(["action.request", "mood.set", "quiet.set", "drag.begin", "drag.end"])
            fields = ({"action": rng.choice(["drink_tea", "greet", "react", "acknowledge"]), "source": "user"}
                      if kind == "action.request" else {"mood": rng.choice(["calm", "happy", "sad"])}
                      if kind == "mood.set" else {"value": rng.choice([True, False])} if kind == "quiet.set" else {})
            self.send(kind, at, **fields)
            state = self.engine.snapshot()
            self.assertLessEqual(len(state["queue"]), QUEUE_LIMIT)
            if state["layer"] in ("quiet", "drag"):
                self.assertIsNone(state["action"])
                self.assertEqual(state["queue"], [])


class ProfilesAndRenderer(unittest.TestCase):
    def test_history_bound_cannot_disable_or_unbound_audit_storage(self):
        for value in (0, -1, True, None, 10001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Engine(load_profile("simulator"), history_limit=value)

    def test_invalid_mapping_ranges_ids_durations_and_extra_fields_are_rejected(self):
        original = load_profile("hiyori").data()
        mutations = [lambda p: p.update(schema_version=2), lambda p: p.update(code="exec"),
                     lambda p: p["parameters"]["ParamAngleZ"].update(default=float("nan")),
                     lambda p: p["actions"]["greet"].update(duration_ms=0),
                     lambda p: p["moods"]["calm"].update(UnknownParam=0),
                     lambda p: p["moods"]["calm"].update(ParamAngleZ=999)]
        for mutate in mutations:
            data = deepcopy(original)
            mutate(data)
            with self.assertRaises(ValueError):
                Profile(data)

    def native(self):
        model = Mock()
        params = [SimpleNamespace(id="ParamAngleZ", min=-2, max=2, default=0),
                  SimpleNamespace(id="ParamMouthForm", min=-1, max=1, default=0)]
        model.GetParameterCount.return_value = len(params)
        model.GetParameter.side_effect = lambda index: params[index]
        model.GetMotionGroups.return_value = {"Idle": 1, "Tap": 2, "Tap@Body": 1, "Flick": 1}
        return model

    def test_native_limits_missing_parameters_and_restoration_are_respected(self):
        profile = load_profile("hiyori")
        engine = Engine(profile)
        model, ended, notice = self.native(), Mock(), Mock()
        adapter = Live2DAdapter(ended, notice)
        engine.dispatch({"kind": "mood.set", "mood": "curious"}, 0)
        state = engine.snapshot()
        adapter.before_update(model, state, profile)
        adapter.after_update(model, state, profile)
        model.SetParameterValue.assert_called_with("ParamAngleZ", 2, 1.0)
        self.assertEqual(notice.call_count, 2)  # missing eyes are never invented
        engine.dispatch({"kind": "mood.set", "mood": "calm"}, 1)
        adapter.after_update(model, engine.snapshot(), profile)
        model.SetParameterValue.assert_called_with("ParamAngleZ", 0, 1.0)
        calls = model.SetParameterValue.call_count
        adapter.after_update(model, engine.snapshot(), profile)
        self.assertEqual(model.SetParameterValue.call_count, calls)  # idle allows blink/motion

    def test_native_action_starts_once_and_late_callback_keeps_its_original_token(self):
        profile = load_profile("hiyori")
        engine, model, ended = Engine(profile), self.native(), Mock()
        adapter = Live2DAdapter(ended, Mock())
        adapter.before_update(model, engine.snapshot(), profile)
        engine.dispatch({"kind": "action.request", "action": "greet", "source": "user"}, 10)
        state = engine.snapshot()
        adapter.before_update(model, state, profile)
        callback = model.StartMotion.call_args.kwargs["onFinishMotionHandler"]
        adapter.before_update(model, state, profile)
        self.assertEqual(model.StartMotion.call_count, 2)  # one idle, one action (token 1)
        engine.dispatch({"kind": "drag.begin"}, 20)
        adapter.before_update(model, engine.snapshot(), profile)
        callback()
        self.assertEqual(ended.call_args.args, (0, 1, True))
        result = engine.dispatch({"kind": "action.end", "epoch": 0, "token": 1, "success": True}, 30)
        self.assertEqual(result["reason"], "stale_action")

    def test_missing_native_motion_is_declined_once_without_inventing_a_replacement(self):
        profile = load_profile("hiyori")
        engine, model, ended = Engine(profile), self.native(), Mock()
        model.GetMotionGroups.return_value = {"Idle": 1}
        adapter = Live2DAdapter(ended, Mock())
        engine.dispatch({"kind": "action.request", "action": "greet", "source": "user"}, 0)
        adapter.before_update(model, engine.snapshot(), profile)
        adapter.before_update(model, engine.snapshot(), profile)
        ended.assert_called_once_with(0, 1, False)
        model.StartMotion.assert_not_called()

    def test_same_model_rebinding_restores_native_default_outside_narrow_mapping(self):
        data = load_profile("hiyori").data()
        data["parameters"] = {"ParamAngleZ": {"min": 1, "max": 2, "default": 1}}
        data["moods"] = {"calm": {}, "curious": {"ParamAngleZ": 2}}
        data["activities"], data["levels"] = {"idle": {}}, {}
        profile = Profile(data)
        engine, model = Engine(profile), self.native()
        adapter = Live2DAdapter(Mock(), Mock())
        engine.dispatch({"kind": "mood.set", "mood": "curious"}, 0)
        adapter.before_update(model, engine.snapshot(), profile)
        adapter.after_update(model, engine.snapshot(), profile)
        model.SetParameterValue.assert_called_with("ParamAngleZ", 2, 1.0)
        replacement = load_profile("hiyori")
        engine.dispatch({"kind": "skin.change", "profile": replacement.data()}, 1)
        adapter.before_update(model, engine.snapshot(), replacement)
        model.SetParameterValue.assert_called_with("ParamAngleZ", 0, 1.0)
