"""The Session/SessionState surface promised in README.md -- issue #42.

huginn had no dedicated model.py test file: the shape was covered indirectly
through the reducer's snapshot round-trip, which is fine for a private internal
and not fine for a declared contract. These pin the specific claims the README
makes, so a future edit that quietly breaks a consumer fails here first.
"""
from __future__ import annotations

import unittest

from corvidae.model import ATTENTION_STATES, STATE_RANK, Session, SessionState


def session(**kw) -> Session:
    defaults = dict(key="claude:100", source="claude", session_id="sid-100",
                    cwd="/tmp/p", name="p-1")
    defaults.update(kw)
    return Session(**defaults)


class SessionStateTests(unittest.TestCase):
    def test_states_are_their_own_wire_values(self):
        # SessionState subclasses str, so a member serializes as its value
        # without an explicit .value at every seam. Consumers rely on this.
        self.assertIsInstance(SessionState.WORKING, str)
        self.assertEqual(SessionState.WAITING_PERMISSION, "waiting_permission")
        self.assertEqual(SessionState("done"), SessionState.DONE)

    def test_every_state_is_ranked(self):
        # A missing rank is a KeyError inside to_dict(), i.e. a broken dashboard
        # sort rather than a clear failure -- pin the total coverage instead.
        self.assertEqual(set(STATE_RANK), set(SessionState))

    def test_attention_states_are_the_urgent_three(self):
        self.assertEqual(ATTENTION_STATES, {
            SessionState.WAITING_INPUT,
            SessionState.WAITING_PERMISSION,
            SessionState.ERROR,
        })

    def test_attention_states_outrank_everything_else(self):
        worst_attention = max(STATE_RANK[s] for s in ATTENTION_STATES)
        best_other = min(STATE_RANK[s] for s in set(SessionState) - ATTENTION_STATES)

        self.assertLess(worst_attention, best_other)


class SessionTests(unittest.TestCase):
    def test_only_the_five_identity_fields_are_required(self):
        s = session()

        self.assertEqual(s.state, SessionState.IDLE)
        self.assertEqual(s.state_origin, "init")
        self.assertIsNone(s.pid)

    def test_attention_tracks_state(self):
        self.assertFalse(session(state=SessionState.WORKING).attention)
        self.assertTrue(session(state=SessionState.WAITING_PERMISSION).attention)

    def test_to_dict_adds_rank_and_attention_and_flattens_state(self):
        d = session(state=SessionState.ERROR).to_dict()

        self.assertEqual(d["state"], "error")
        self.assertEqual(d["rank"], STATE_RANK[SessionState.ERROR])
        self.assertIs(d["attention"], True)

    def test_from_dict_round_trips_to_dict(self):
        original = session(state=SessionState.WAITING_INPUT, blurb="doing a thing",
                           pid=4242, tokens=1234, subagents={"running": 2})

        restored = Session.from_dict(original.to_dict())

        self.assertEqual(restored, original)

    def test_from_dict_ignores_unknown_keys(self):
        # The forward-compatibility claim: a newer producer's snapshot must stay
        # readable by an older consumer instead of raising TypeError.
        payload = session().to_dict() | {"a_field_from_a_future_release": [1, 2, 3]}

        restored = Session.from_dict(payload)

        self.assertEqual(restored.key, "claude:100")
        self.assertFalse(hasattr(restored, "a_field_from_a_future_release"))

    def test_from_dict_rejects_a_payload_missing_identity_fields(self):
        with self.assertRaises(TypeError):
            Session.from_dict({"key": "claude:1", "state": "idle"})

    def test_from_dict_rejects_an_unknown_state(self):
        with self.assertRaises(ValueError):
            Session.from_dict(session().to_dict() | {"state": "transcendent"})


if __name__ == "__main__":
    unittest.main()
