"""Huginn's side of the raven protocol: descriptor lifecycle, menu, actions (#40).

The host that consumes this (the shared status menu bar) is a separate project,
so its parser is not importable here in general. Two things stand in for it:

* ``parse_menu`` below is a faithful replication of the host's own
  ``menu_spec.parse_menu`` -- the same caps, the same coercions, the same
  drop/truncate rules -- so a payload that survives it unchanged is one the host
  renders unchanged. Replicated rather than imported deliberately: the host's
  module layout was mid-rename while this was written, and a test that imports a
  sibling checkout by path is a test that only passes on one machine.
* ``test_payload_survives_the_real_host_parser`` uses the *actual* host parser
  when a checkout is pointed at by ``$APPISTRY_SRC``, and skips otherwise. That is
  the belt-and-braces check that the replication above has not drifted.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from huginn import config, raven
from huginn.config import Config
from huginn.daemon import Daemon
from huginn.model import Session, SessionState


# ── A faithful replication of the host's menu parser ──────────────────────────
#
# Mirrors appistry/roost menu_spec.py at protocol version 1. Kept small but
# behaviourally identical for the properties Huginn's payload must satisfy: what
# is dropped, what is truncated, and what is forced inert.

MAX_SECTIONS = 12
MAX_ITEMS_PER_SECTION = 50
MAX_TOTAL_ITEMS = 200
MAX_LABEL_LENGTH = 120
MAX_DETAIL_LENGTH = 80
MAX_ACTION_ID_LENGTH = 128
STYLES = ("normal", "attention", "muted")

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPOOF_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_WHITESPACE_RE = re.compile(r"[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")


def sanitize_label(value: object, limit: int = MAX_LABEL_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPOOF_RE.sub("", cleaned)
    cleaned = cleaned.replace("\x1b", "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + "\u2026"
    return cleaned


def contains_unsafe_text(value: str) -> bool:
    if not isinstance(value, str):
        return True
    return bool("\x1b" in value or _CONTROL_RE.search(value) or _SPOOF_RE.search(value))


def parse_item(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("separator") is True:
        return {"separator": True, "label": "", "action_id": "", "url": "",
                "detail": "", "enabled": True, "style": "normal"}
    label = sanitize_label(raw.get("label"))
    if not label:
        return None
    action_id = raw.get("id")
    if (not isinstance(action_id, str) or not action_id
            or len(action_id) > MAX_ACTION_ID_LENGTH
            or contains_unsafe_text(action_id) or any(c in action_id for c in "\r\n")):
        action_id = ""
    url = raw.get("url") if isinstance(raw.get("url"), str) else ""
    style = raw.get("style") if raw.get("style") in STYLES else "normal"
    enabled = raw.get("enabled") if isinstance(raw.get("enabled"), bool) else True
    return {
        "separator": False, "label": label, "action_id": action_id, "url": url,
        "detail": sanitize_label(raw.get("detail"), MAX_DETAIL_LENGTH),
        "enabled": enabled and bool(action_id or url), "style": style,
    }


def parse_section(raw: object, budget: int) -> tuple[dict | None, int]:
    if not isinstance(raw, dict) or budget <= 0:
        return None, budget
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        return None, budget
    items: list[dict] = []
    for raw_item in raw_items[:MAX_ITEMS_PER_SECTION]:
        if budget <= 0:
            break
        item = parse_item(raw_item)
        if item is None:
            continue
        if item["separator"] and (not items or items[-1]["separator"]):
            continue
        items.append(item)
        budget -= 1
    while items and items[-1]["separator"]:
        items.pop()
        budget += 1
    if not items:
        return None, budget
    section_id = raw.get("id")
    if not isinstance(section_id, str) or contains_unsafe_text(section_id):
        section_id = ""
    return {"id": section_id, "title": sanitize_label(raw.get("title")), "items": items}, budget


def parse_menu(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {"title": "", "badge": 0, "sections": []}
    raw_sections = payload.get("sections")
    badge = payload.get("badge")
    badge = badge if isinstance(badge, int) and not isinstance(badge, bool) and 0 <= badge <= 9999 else 0
    if not isinstance(raw_sections, list):
        return {"title": sanitize_label(payload.get("title")), "badge": badge, "sections": []}
    sections: list[dict] = []
    budget = MAX_TOTAL_ITEMS
    for raw_section in raw_sections[:MAX_SECTIONS]:
        section, budget = parse_section(raw_section, budget)
        if section is not None:
            sections.append(section)
    return {"title": sanitize_label(payload.get("title")), "badge": badge, "sections": sections}


def session(key: str, name: str, state: SessionState, *, since: float = 0.0, **kw) -> Session:
    return Session(key=key, source=kw.pop("source", "claude"), session_id=key.split(":")[-1],
                   cwd=kw.pop("cwd", "/tmp/project"), name=name, state=state,
                   state_since=since, **kw)


# ── Descriptor location ───────────────────────────────────────────────────────

class DescriptorLocationTests(unittest.TestCase):
    """The resolution rule is the contract: resolve it differently and Huginn
    publishes where the host is not looking, silently."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("RAVENS_STATE_DIR", "XDG_STATE_HOME", "LOCALAPPDATA")}
        for key in self._env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_posix_default(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(raven.state_dir(), Path.home() / ".local" / "state" / "ravens")

    def test_posix_honours_xdg_state_home(self):
        # Not optional even though config.STATE_DIR ignores it: this directory is
        # shared with the host and the other raven.
        os.environ["XDG_STATE_HOME"] = "/tmp/xdg"
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(raven.state_dir(), Path("/tmp/xdg/ravens"))

    def test_diverges_from_huginns_own_state_dir_under_xdg(self):
        # Documented asymmetry, asserted so nobody "fixes" one to match the
        # other by accident. config.STATE_DIR hardcodes ~/.local/state/huginn.
        os.environ["XDG_STATE_HOME"] = "/tmp/xdg"
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(raven.state_dir(), Path("/tmp/xdg/ravens"))
        self.assertEqual(config.STATE_DIR, Path.home() / ".local" / "state" / "huginn")

    def test_windows_uses_localappdata(self):
        os.environ["LOCALAPPDATA"] = r"C:\Users\me\AppData\Local"
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(raven.state_dir(), Path(r"C:\Users\me\AppData\Local") / "Ravens")

    def test_windows_falls_back_without_localappdata(self):
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(raven.state_dir(), Path.home() / "AppData" / "Local" / "Ravens")

    def test_explicit_override_wins_on_every_platform(self):
        os.environ["RAVENS_STATE_DIR"] = "/tmp/override"
        os.environ["XDG_STATE_HOME"] = "/tmp/xdg"
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                self.assertEqual(raven.state_dir(), Path("/tmp/override"))


# ── Descriptor content and lifecycle ──────────────────────────────────────────

class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get("RAVENS_STATE_DIR")
        os.environ["RAVENS_STATE_DIR"] = str(Path(self.tmp.name) / "ravens")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("RAVENS_STATE_DIR", None)
        else:
            os.environ["RAVENS_STATE_DIR"] = self._orig
        self.tmp.cleanup()

    def test_publishes_every_field_the_host_reads(self):
        path = raven.publish(47100)
        payload = json.loads(path.read_text())

        self.assertEqual(path, raven.descriptor_path())
        self.assertEqual(payload["name"], "huginn")
        self.assertEqual(payload["display"], "Huginn")
        self.assertEqual(payload["port"], 47100)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["host_priority"], 100)
        self.assertEqual(payload["token_header"], "X-Huginn-Token")
        self.assertEqual(payload["endpoints"],
                         {"menu": "/api/menu", "action": "/api/menu/action"})
        self.assertIsInstance(payload["started"], float)

    def test_declares_a_range_not_an_equality(self):
        # issue #38: an exact-match comparison silently disabled every
        # participant on a routine bump, with nothing on screen to explain it.
        payload = json.loads(raven.publish(47100).read_text())

        self.assertEqual(payload["min_api"], 1)
        self.assertEqual(payload["max_api"], 1)
        self.assertEqual(payload["api_version"], payload["max_api"])
        self.assertLessEqual(payload["min_api"], payload["max_api"])

    def test_names_huginns_real_token_file(self):
        # The host authenticates by reading this path, so a decorative or
        # invented value would make every fetch 401 with no way to see why.
        payload = json.loads(raven.publish(47100).read_text())

        self.assertEqual(payload["token_path"], str(config.TOKEN_PATH))
        self.assertTrue(Path(payload["token_path"]).is_absolute())

    def test_records_the_process_start_time_not_the_publish_time(self):
        # The host cross-checks this against the OS's record of when the process
        # began, with two seconds of slack, to resist pid reuse. Taking
        # time.time() at publish -- after snapshot restore and plugin startup --
        # would push a healthy daemon outside that window.
        with patch("huginn.platform.platform.process_start_time", return_value=12345.5):
            payload = json.loads(raven.publish(47100).read_text())

        self.assertEqual(payload["started"], 12345.5)

    def test_falls_back_to_the_wall_clock_when_the_os_cannot_answer(self):
        # A missing cross-check must not turn a live raven into a dead one, so an
        # unknown start time still yields a plausible epoch value.
        before = time.time()
        with patch("huginn.platform.platform.process_start_time", return_value=None):
            payload = json.loads(raven.publish(47100).read_text())

        self.assertGreaterEqual(payload["started"], before)

    def test_is_owner_only(self):
        # Same reasoning as daemon.json (issue #41): no secret, but another
        # process reads a port and a token path out of it and acts on them.
        path = raven.publish(47100)
        mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(mode, 0o600, oct(mode))

    def test_directory_is_created_owner_only(self):
        path = raven.publish(47100)
        mode = stat.S_IMODE(path.parent.stat().st_mode)

        self.assertEqual(mode, 0o700, oct(mode))

    def test_republishing_keeps_the_restricted_mode(self):
        raven.publish(47100)
        path = raven.publish(47201)

        self.assertEqual(json.loads(path.read_text())["port"], 47201)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_publish_leaves_no_temp_file_behind(self):
        path = raven.publish(47100)

        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["huginn.json"])

    def test_withdraw_removes_our_own_descriptor(self):
        path = raven.publish(47100)
        raven.withdraw()

        self.assertFalse(path.exists())

    def test_withdraw_leaves_another_processes_descriptor_alone(self):
        # A daemon that lost the port race, or a replacement that already
        # republished, must not have its descriptor deleted by our exit.
        path = raven.publish(47100, pid=os.getpid() + 1)
        raven.withdraw()

        self.assertTrue(path.exists())

    def test_withdraw_is_quiet_when_nothing_was_published(self):
        raven.withdraw()   # must not raise: shutdown runs it unconditionally

        self.assertFalse(raven.descriptor_path().exists())

    def test_withdraw_survives_a_corrupt_descriptor(self):
        path = raven.publish(47100)
        path.write_text("{not json")
        raven.withdraw()

        # Left in place rather than guessed at -- and the host refuses an
        # unparseable descriptor with a visible reason anyway.
        self.assertTrue(path.exists())

    def test_an_existing_shared_directory_is_not_retightened(self):
        # Shared with other ravens; silently changing another project's directory
        # mode is not ours to do.
        directory = raven.state_dir()
        directory.mkdir(parents=True)
        directory.chmod(0o755)
        raven.publish(47100)

        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)


class DaemonLifecycleTests(unittest.TestCase):
    """The descriptor exists exactly while the daemon is serving."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = config.STATE_DIR
        config.STATE_DIR = Path(self.tmp.name) / "huginn"
        config.STATE_DIR.mkdir(parents=True)
        # TOKEN_PATH is a module constant derived from STATE_DIR at import, so
        # redirecting STATE_DIR alone would let a test rotate the real token of a
        # daemon running on this machine.
        self._orig_token_path = config.TOKEN_PATH
        config.TOKEN_PATH = config.STATE_DIR / "token"
        self._orig_env = os.environ.get("RAVENS_STATE_DIR")
        os.environ["RAVENS_STATE_DIR"] = str(Path(self.tmp.name) / "ravens")

    def tearDown(self):
        config.STATE_DIR = self._orig_state_dir
        config.TOKEN_PATH = self._orig_token_path
        if self._orig_env is None:
            os.environ.pop("RAVENS_STATE_DIR", None)
        else:
            os.environ["RAVENS_STATE_DIR"] = self._orig_env
        self.tmp.cleanup()

    def _serve_once(self, daemon: Daemon, observe) -> None:
        """Run daemon.run() through one serve() call, then let it shut down."""
        import asyncio
        import socket

        async def fake_serve(_server, sockets=None):
            observe(sockets)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            daemon.cfg.update("server", "port", probe.getsockname()[1])

        with patch("uvicorn.Server.serve", new=fake_serve), \
             patch("huginn.daemon.Daemon.claude_watcher", new=_noop), \
             patch("huginn.daemon.Daemon.transcript_watcher", new=_noop), \
             patch("huginn.daemon.Daemon.codex_poller", new=_noop), \
             patch("huginn.daemon.Daemon.codex_rollout_watcher", new=_noop), \
             patch("huginn.daemon.Daemon.ticker", new=_noop), \
             patch("huginn.daemon.Daemon.desktop_poller", new=_noop), \
             patch("huginn.daemon.Daemon.chatgpt_desktop_poller", new=_noop), \
             patch("huginn.daemon.Daemon.wsl_poller", new=_noop), \
             patch("huginn.daemon.Daemon.reducer_loop", new=_noop):
            asyncio.run(daemon.run(open_browser=False))

    def test_serving_publishes_and_a_clean_exit_withdraws(self):
        daemon = Daemon(Config({}))
        seen: dict[str, object] = {}

        def observe(sockets):
            # Mid-flight: this is the window in which the host must be able to
            # find us, and the port must already be bound.
            path = raven.descriptor_path()
            seen["published"] = path.exists()
            seen["payload"] = json.loads(path.read_text())
            seen["bound"] = bool(sockets) and sockets[0].getsockname()[1] > 0

        self._serve_once(daemon, observe)

        self.assertTrue(seen["published"])
        self.assertTrue(seen["bound"])
        self.assertEqual(seen["payload"]["port"], daemon.cfg.get("server", "port"))
        self.assertEqual(seen["payload"]["pid"], os.getpid())
        self.assertFalse(raven.descriptor_path().exists())

    def test_the_descriptor_names_the_token_the_daemon_just_minted(self):
        # The host reads this file to authenticate, and the token rotates on every
        # start -- so the path has to be the live one, not a snapshot of it.
        daemon = Daemon(Config({}))
        seen: dict[str, object] = {}

        def observe(_sockets):
            # Read while serving: the token file is removed on a clean exit, and
            # what matters is that the host could have read it mid-flight.
            payload = json.loads(raven.descriptor_path().read_text())
            seen["token"] = Path(payload["token_path"]).read_text().strip()

        self._serve_once(daemon, observe)

        self.assertEqual(seen["token"], daemon.token)

    def test_an_unwritable_shared_directory_does_not_stop_the_daemon(self):
        # Publishing is best-effort: a shared directory Huginn cannot write is
        # not a reason for Huginn's own console to refuse to serve.
        daemon = Daemon(Config({}))
        seen: dict[str, object] = {}
        with patch("huginn.raven.publish", side_effect=OSError("read-only")):
            self._serve_once(daemon, lambda _s: seen.update(served=True))

        self.assertTrue(seen["served"])
        self.assertEqual(
            daemon.diagnostics.snapshot()["raven_descriptor"]["last_error_class"], "OSError")


async def _noop(self):
    import asyncio
    await asyncio.sleep(3600)


# ── The menu payload ──────────────────────────────────────────────────────────

class MenuShapeTests(unittest.TestCase):
    def test_reports_attention_sessions_with_actions(self):
        now = 1_000.0
        payload = raven.build_menu([
            session("claude:1", "alpha", SessionState.WAITING_PERMISSION,
                    since=now - 90, blurb="Approve deploy"),
            session("codex:t2", "beta", SessionState.WORKING, since=now - 20,
                    source="codex", title="Refactor the parser"),
        ], now=now)
        spec = parse_menu(payload)
        by_id = {s["id"]: s for s in spec["sections"]}

        self.assertEqual(spec["title"], "Huginn")
        self.assertEqual(spec["badge"], 1)
        self.assertEqual(by_id["attention"]["items"][0]["label"], "alpha: Approve deploy")
        self.assertEqual(by_id["attention"]["items"][0]["action_id"], "focus:claude:1")
        self.assertEqual(by_id["attention"]["items"][0]["style"], "attention")
        self.assertEqual(by_id["working"]["items"][0]["action_id"], "focus:codex:t2")

    def test_badge_matches_the_attention_count_the_dashboard_shows(self):
        sessions = [
            session("claude:1", "a", SessionState.WAITING_PERMISSION),
            session("claude:2", "b", SessionState.WAITING_INPUT),
            session("claude:3", "c", SessionState.ERROR),
            session("claude:4", "d", SessionState.WORKING),
        ]
        payload = raven.build_menu(sessions)

        self.assertEqual(payload["badge"], 3)
        self.assertEqual(parse_menu(payload)["badge"], 3)

    def test_permission_prompts_lead_and_the_oldest_is_first(self):
        now = 1_000.0
        payload = raven.build_menu([
            session("claude:1", "recent-perm", SessionState.WAITING_PERMISSION, since=now - 5),
            session("claude:2", "old-input", SessionState.WAITING_INPUT, since=now - 500),
            session("claude:3", "old-perm", SessionState.WAITING_PERMISSION, since=now - 300),
        ], now=now)
        labels = [i["label"] for s in parse_menu(payload)["sections"]
                  if s["id"] == "attention" for i in s["items"]]

        self.assertEqual(labels, ["old-perm", "recent-perm", "old-input"])

    def test_a_quiet_roster_still_says_something(self):
        # An empty payload renders as "Nothing to report", which is
        # indistinguishable from a broken raven. A verdict row is not.
        spec = parse_menu(raven.build_menu([]))
        by_id = {s["id"]: s for s in spec["sections"]}

        self.assertEqual(spec["badge"], 0)
        self.assertEqual(by_id["status"]["items"][0]["label"], "Nothing needs you right now")
        self.assertNotIn("attention", by_id)
        self.assertIn("console", by_id)

    def test_worktree_contention_is_named_and_inert(self):
        shared = str(Path(tempfile.gettempdir(), "shared").resolve())
        sessions = [
            session("claude:1", "alpha", SessionState.WORKING, cwd=shared),
            session("codex:t2", "beta", SessionState.WORKING, cwd=shared, source="codex"),
        ]
        payload = raven.build_menu(sessions, now=1_000.0)
        section = next(s for s in parse_menu(payload)["sections"] if s["id"] == "contention")

        self.assertEqual(section["items"][0]["label"], shared)
        self.assertEqual(section["items"][0]["detail"], "alpha, beta")
        # No session to focus: a row that looks clickable and does nothing is
        # worse than one that admits it.
        self.assertFalse(section["items"][0]["enabled"])

    def test_an_ended_pileup_is_capped_and_says_so(self):
        sessions = [session(f"claude:{i}", f"gone-{i}", SessionState.ENDED, since=float(i))
                    for i in range(12)]
        section = next(s for s in parse_menu(raven.build_menu(sessions, now=1_000.0))["sections"]
                       if s["id"] == "ended")

        self.assertEqual(len(section["items"]), 6)
        self.assertEqual(section["items"][-1]["label"], "+7 more in the console")

    def test_ended_sessions_offer_a_dismiss_action(self):
        payload = raven.build_menu(
            [session("claude:9", "gamma", SessionState.ENDED, since=900.0)], now=1_000.0)
        section = next(s for s in parse_menu(payload)["sections"] if s["id"] == "ended")

        self.assertEqual(section["items"][0]["label"], "Dismiss gamma")
        self.assertEqual(section["items"][0]["action_id"], "dismiss:claude:9")

    def test_every_published_action_id_is_one_huginn_handles(self):
        # The rule that keeps the menu honest: an id we emit but do not accept
        # is a row that fails when clicked, and the host cannot know that.
        sessions = [
            session("claude:1", "a", SessionState.WAITING_PERMISSION),
            session("claude:2", "b", SessionState.WORKING),
            session("claude:3", "c", SessionState.ENDED),
        ]
        payload = raven.build_menu(sessions)
        ids = [i["action_id"] for s in parse_menu(payload)["sections"]
               for i in s["items"] if i["action_id"]]
        handled = {raven.OPEN_CONSOLE, *(f"focus:{s.key}" for s in sessions),
                   *(f"dismiss:{s.key}" for s in sessions)}

        self.assertTrue(ids)
        for action_id in ids:
            with self.subTest(action_id=action_id):
                self.assertIn(action_id, handled)

    def test_nothing_is_truncated_by_the_host(self):
        # A payload the host cuts off mid-section is a payload we should have
        # summarised ourselves. 60 attention sessions is well past our own caps.
        sessions = [session(f"claude:{i}", f"session-{i}", SessionState.WAITING_INPUT,
                            since=float(i)) for i in range(60)]
        payload = raven.build_menu(sessions, now=1_000.0)
        spec = parse_menu(payload)

        self.assertLessEqual(len(payload["sections"]), MAX_SECTIONS)
        self.assertEqual(len(spec["sections"]), len(payload["sections"]))
        total = 0
        for emitted, parsed in zip(payload["sections"], spec["sections"]):
            with self.subTest(section=emitted["id"]):
                self.assertLessEqual(len(emitted["items"]), MAX_ITEMS_PER_SECTION)
                self.assertEqual(len(parsed["items"]), len(emitted["items"]))
            total += len(emitted["items"])
        self.assertLessEqual(total, MAX_TOTAL_ITEMS)

    def test_a_capped_section_says_what_it_left_out(self):
        sessions = [session(f"claude:{i}", f"session-{i}", SessionState.WAITING_INPUT,
                            since=float(i)) for i in range(60)]
        section = next(s for s in parse_menu(raven.build_menu(sessions, now=1_000.0))["sections"]
                       if s["id"] == "attention")

        self.assertEqual(section["items"][-1]["label"], "+45 more in the console")
        self.assertFalse(section["items"][-1]["enabled"])

    def test_the_badge_still_counts_what_the_rows_omit(self):
        sessions = [session(f"claude:{i}", f"s{i}", SessionState.ERROR, since=float(i))
                    for i in range(60)]

        self.assertEqual(parse_menu(raven.build_menu(sessions))["badge"], 60)

    def test_an_unpublishable_session_key_leaves_an_inert_row(self):
        # A plugin source can produce a very long key. The session stays visible
        # with no action rather than disappearing from the menu entirely.
        sessions = [session("plugin:x." + "k" * 200, "huge", SessionState.WAITING_INPUT)]
        section = next(s for s in parse_menu(raven.build_menu(sessions))["sections"]
                       if s["id"] == "attention")

        self.assertEqual(section["items"][0]["label"], "huge")
        self.assertEqual(section["items"][0]["action_id"], "")
        self.assertFalse(section["items"][0]["enabled"])

    @unittest.skipUnless(os.environ.get("APPISTRY_SRC"),
                         "set APPISTRY_SRC to a menu-bar checkout to cross-check")
    def test_payload_survives_the_real_host_parser(self):
        # Guards the replication above against drift. Skipped by default: the
        # host is a separate project and is not a test dependency.
        sys.path.insert(0, os.environ["APPISTRY_SRC"])
        try:
            from roost import menu_spec   # noqa: PLC0415 -- optional cross-check
        finally:
            sys.path.pop(0)
        sessions = [
            session("claude:1", "alpha", SessionState.WAITING_PERMISSION, since=900.0),
            session("codex:t2", "beta", SessionState.WORKING, since=980.0, source="codex"),
            session("claude:3", "gamma", SessionState.ENDED, since=995.0),
        ]
        payload = raven.build_menu(sessions, now=1_000.0)
        real = menu_spec.parse_menu(payload)
        mine = parse_menu(payload)

        self.assertEqual(real.badge, mine["badge"])
        self.assertEqual(real.title, mine["title"])
        self.assertEqual(
            [(s.id, s.title, [(i.label, i.action_id, i.detail, i.style, i.enabled)
                              for i in s.items]) for s in real.sections],
            [(s["id"], s["title"], [(i["label"], i["action_id"], i["detail"], i["style"],
                                     i["enabled"]) for i in s["items"]])
             for s in mine["sections"]],
        )


# ── Sanitisation ──────────────────────────────────────────────────────────────

class SanitisationTests(unittest.TestCase):
    """Session names, titles, and blurbs come from directory names, transcripts,
    and LLM output. Appistry sanitises at its end; that is defence in depth for
    the host, not permission for Huginn to emit control characters."""

    HOSTILE = (
        "\x1b[31mQuit Huginn\x1b[0m",
        "line one\nline two",
        "bell\x07and\x00nul",
        "\u202eabcdef",
        "csi\x9b31m",
        "zero\u200bwidth",
    )

    def test_hostile_session_text_cannot_reach_a_label(self):
        for hostile in self.HOSTILE:
            with self.subTest(hostile=hostile):
                payload = raven.build_menu(
                    [session("claude:1", hostile, SessionState.WAITING_INPUT, blurb=hostile)])
                labels = [i["label"] for s in payload["sections"] for i in s["items"]]
                blob = json.dumps(payload)

                self.assertNotIn("\x1b", blob)
                self.assertNotIn("\x00", blob)
                self.assertNotIn("\u202e", blob)
                for label in labels:
                    self.assertFalse(contains_unsafe_text(label), repr(label))
                    self.assertNotIn("\n", label)

    def test_the_host_parser_finds_nothing_to_repair(self):
        # If our text needed cleaning, the host's cleaned copy would differ from
        # what we sent -- which is how a raven ends up with a label it did not
        # intend on screen.
        payload = raven.build_menu([
            session("claude:1", "\x1b[1mbold\x1b[0m name", SessionState.ERROR,
                    blurb="two\n\nlines\tand\ttabs"),
        ])
        for parsed, emitted in zip(parse_menu(payload)["sections"], payload["sections"]):
            for parsed_item, emitted_item in zip(parsed["items"], emitted["items"]):
                with self.subTest(label=emitted_item["label"]):
                    self.assertEqual(parsed_item["label"], emitted_item["label"])
                    self.assertEqual(parsed_item["detail"], emitted_item.get("detail", ""))

    def test_labels_and_details_stay_inside_the_hosts_caps(self):
        payload = raven.build_menu([
            session("claude:1", "n" * 400, SessionState.WAITING_INPUT, blurb="b" * 400),
        ])
        for section in payload["sections"]:
            for item in section["items"]:
                with self.subTest(label=item["label"][:20]):
                    self.assertLessEqual(len(item["label"]), MAX_LABEL_LENGTH)
                    self.assertLessEqual(len(item.get("detail", "")), MAX_DETAIL_LENGTH)

    def test_credential_shapes_in_a_title_are_redacted(self):
        # A title arrives straight from PUT /api/sessions/{key}/title and a blurb
        # from an LLM; neither passed through the transcript seam that already
        # redacts peek/blurb/Ask text on the way to the dashboard.
        payload = raven.build_menu([
            session("claude:1", "deploy", SessionState.WAITING_PERMISSION,
                    title="use ghp_abcdefghijklmnopqrstuvwxyz012345 to push"),
        ])
        blob = json.dumps(payload)

        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz012345", blob)
        self.assertIn("[REDACTED]", blob)

    def test_redaction_runs_before_the_length_cap(self):
        # Clipping first could cut a credential shape in half so the pattern no
        # longer matches, leaving a partial secret on screen.
        secret = "ghp_" + "a" * 40
        payload = raven.build_menu([
            session("claude:1", "x" * 100, SessionState.ERROR, title="y" * 100 + " " + secret),
        ])

        self.assertNotIn(secret[:20], json.dumps(payload))

    def test_a_session_with_no_usable_name_still_renders(self):
        payload = raven.build_menu(
            [session("claude:1", "\x1b\x00", SessionState.WAITING_INPUT, source="codex")])
        section = next(s for s in parse_menu(payload)["sections"] if s["id"] == "attention")

        # A row dropped for an empty label is a session missing from the menu.
        self.assertEqual(section["items"][0]["label"], "codex")


# ── Actions ───────────────────────────────────────────────────────────────────

class ActionTests(unittest.TestCase):
    def setUp(self):
        self.daemon = Daemon(Config({}))
        self.daemon.token = "test-token"

    def _add(self, key: str, state: SessionState) -> Session:
        s = session(key, key.split(":")[-1], state)
        self.daemon.reducer.sessions[key] = s
        return s

    def test_focus_round_trips_a_published_id(self):
        self._add("claude:1", SessionState.WAITING_PERMISSION)
        payload = raven.build_menu(self.daemon.reducer.sessions.values())
        action_id = next(i["id"] for s in payload["sections"] for i in s["items"]
                         if i.get("id", "").startswith("focus:"))

        with patch("huginn.focus.focus_session", return_value={"ok": True, "target": "iTerm2"}):
            result = raven.perform_action(self.daemon, action_id)

        self.assertEqual(action_id, "focus:claude:1")
        self.assertTrue(result["ok"])

    def test_dismiss_round_trips_and_removes_the_session(self):
        self._add("claude:9", SessionState.ENDED)
        payload = raven.build_menu(self.daemon.reducer.sessions.values())
        action_id = next(i["id"] for s in payload["sections"] for i in s["items"]
                         if i.get("id", "").startswith("dismiss:"))

        result = raven.perform_action(self.daemon, action_id)

        self.assertEqual(result, {"ok": True, "dismissed": "claude:9"})
        self.assertNotIn("claude:9", self.daemon.reducer.sessions)

    def test_open_console_bootstraps_the_dashboard(self):
        with patch("webbrowser.open") as opener:
            result = raven.perform_action(self.daemon, "open-console")

        self.assertTrue(result["ok"])
        # The credential stays inside the daemon that owns it: the fragment is
        # built here rather than published as a url row in the menu payload.
        self.assertIn("#t=test-token", opener.call_args.args[0])

    def test_unknown_action_is_refused_cleanly(self):
        for action_id in ("nope", "focus", "focus:", "restart", "dismiss:claude:1",
                          "focus:../../etc/passwd", "f" * 200, "", None, 7, {"id": "x"}):
            with self.subTest(action_id=action_id):
                result = raven.perform_action(self.daemon, action_id)

                self.assertFalse(result["ok"])
                self.assertIn("error", result)

    def test_a_stale_id_is_refused_rather_than_guessed_at(self):
        # The normal case: a session ended between the menu build and the click.
        self._add("claude:1", SessionState.WAITING_INPUT)
        payload = raven.build_menu(self.daemon.reducer.sessions.values())
        action_id = next(i["id"] for s in payload["sections"] for i in s["items"]
                         if i.get("id", "").startswith("focus:"))
        del self.daemon.reducer.sessions["claude:1"]

        result = raven.perform_action(self.daemon, action_id)

        self.assertEqual(result, {"ok": False, "error": "session is no longer live"})

    def test_dismiss_refuses_a_live_session(self):
        self._add("claude:1", SessionState.WORKING)

        result = raven.perform_action(self.daemon, "dismiss:claude:1")

        self.assertFalse(result["ok"])
        self.assertIn("claude:1", self.daemon.reducer.sessions)

    def test_a_failing_action_reports_instead_of_crashing_the_daemon(self):
        self._add("claude:1", SessionState.WAITING_INPUT)
        with patch("huginn.focus.focus_session", side_effect=RuntimeError("/secret/path")):
            result = raven.perform_action(self.daemon, "focus:claude:1")

        self.assertEqual(result, {"ok": False, "error": "action failed"})
        # Diagnostics stores the class name only, never the message text.
        self.assertNotIn("/secret/path", json.dumps(self.daemon.diagnostics.snapshot()))
        self.assertEqual(
            self.daemon.diagnostics.snapshot()["raven_action"]["last_error_class"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
