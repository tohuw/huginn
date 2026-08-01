"""Backward compatibility of the paths corvidae was extracted from -- issue #42.

Plugins import ``huginn.model`` and ``huginn.sources.transcript`` directly, and
out-of-tree plugin distributions (e.g. huginn-cisco's Bedrock and Neo-Cortex)
import from upstream by module path. The extraction is only acceptable if it is
invisible to them, so these pin the old import paths and pin that they resolve to
the *same objects* -- not lookalike copies, which would break isinstance checks
and enum identity across the seam.

The second round of extraction moved the ``LoginAgent`` seam (issue #39) and the
raven descriptor/label machinery (issue #40) as well, so those are pinned here
too: ``huginn install-agent`` and the descriptor Huginn publishes are both things
a user's installed state and a separate host project depend on, and neither may
change shape because the implementation moved.
"""
from __future__ import annotations

import unittest
import unittest.mock

import corvidae
from huginn import agent_install as huginn_agent_install
from huginn import model as huginn_model
from huginn import raven as huginn_raven
from huginn.agent_install import (
    LaunchdAgent,
    SystemdUserAgent,
    WindowsStartupAgent,
    get_login_agent,
    install,
    uninstall,
)
from huginn.llm import context as huginn_context
from huginn.model import ATTENTION_STATES, STATE_RANK, Event, Session, SessionState
from huginn.sources import transcript as huginn_transcript
from huginn.sources.transcript import ATTACH_WINDOW, MAX_READ, ClaudeAnalyzer, CodexAnalyzer, Tail


class OriginalImportPathsTests(unittest.TestCase):
    def test_model_names_are_the_shared_objects_not_copies(self):
        self.assertIs(Session, corvidae.Session)
        self.assertIs(SessionState, corvidae.SessionState)
        self.assertIs(STATE_RANK, corvidae.STATE_RANK)
        self.assertIs(ATTENTION_STATES, corvidae.ATTENTION_STATES)

    def test_transcript_names_are_the_shared_objects_not_copies(self):
        self.assertIs(Tail, corvidae.Tail)
        self.assertIs(ClaudeAnalyzer, corvidae.ClaudeAnalyzer)
        self.assertIs(CodexAnalyzer, corvidae.CodexAnalyzer)
        self.assertEqual(ATTACH_WINDOW, corvidae.ATTACH_WINDOW)
        self.assertEqual(MAX_READ, corvidae.MAX_READ)

    def test_redact_secrets_is_the_shared_object_not_a_copy(self):
        self.assertIs(huginn_context.redact_secrets, corvidae.redact_secrets)

    def test_event_stayed_in_huginn(self):
        # Deliberately not shared: it is this daemon's bus envelope. Kept as a
        # test so a later "tidy the re-exports" pass doesn't move it by reflex.
        self.assertFalse(hasattr(corvidae, "Event"))
        self.assertIs(huginn_model.Event, Event)

    def test_private_helpers_context_relies_on_still_resolve(self):
        # huginn.llm.context imports these from the old path; they carry no
        # corvidae stability promise, but huginn is inside the version boundary.
        for name in ("_items", "_user_text", "_parse_lines", "_strip_meta", "_iso_ts"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(huginn_transcript, name)))


class LoginAgentCompatTests(unittest.TestCase):
    """``huginn.agent_install``'s surface after the #42 extraction.

    Unlike the model/transcript re-exports, the three backends are *subclasses*
    rather than the same objects: corvidae's take a ``LoginAgentSpec`` in their
    constructor, while Huginn's callers write ``LaunchdAgent()`` and expect
    Huginn's own spec. ``isinstance`` against corvidae's classes is the property a
    consumer could depend on, and it holds.
    """

    def test_each_backend_is_a_corvidae_backend(self):
        for cls, shared in ((LaunchdAgent, corvidae.LaunchdAgent),
                            (SystemdUserAgent, corvidae.SystemdUserAgent),
                            (WindowsStartupAgent, corvidae.WindowsStartupAgent)):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, shared))
                self.assertTrue(issubclass(cls, corvidae.LoginAgent))
                self.assertIsInstance(cls(), shared)

    def test_get_login_agent_keeps_its_one_argument_signature(self):
        # corvidae's takes (spec, name); huginn's callers and its CLI pass a
        # platform name alone, and that must not have changed.
        self.assertIsInstance(get_login_agent("darwin"), LaunchdAgent)
        self.assertIsInstance(get_login_agent("linux"), SystemdUserAgent)
        self.assertIsInstance(get_login_agent("win32"), WindowsStartupAgent)
        self.assertIsNone(get_login_agent("freebsd14"))
        self.assertIsNotNone(get_login_agent())

    def test_the_module_constants_the_docs_and_tray_name_still_exist(self):
        # WINDOWS.md and README.md name these, and the Windows tray depends on
        # DAEMON_RUN_VALUE being distinct from TRAY_RUN_VALUE.
        self.assertEqual(huginn_agent_install.LABEL, "is.tohuw.huginn")
        self.assertEqual(huginn_agent_install.UNIT_NAME, "huginn.service")
        self.assertEqual(huginn_agent_install.DAEMON_RUN_VALUE, "HuginnDaemon")
        self.assertEqual(huginn_agent_install.TRAY_RUN_VALUE, "Huginn")
        self.assertNotEqual(huginn_agent_install.DAEMON_RUN_VALUE,
                            huginn_agent_install.TRAY_RUN_VALUE)
        self.assertTrue(str(huginn_agent_install.PLIST_PATH).endswith(
            "Library/LaunchAgents/is.tohuw.huginn.plist"))
        self.assertTrue(str(huginn_agent_install.UNIT_PATH).endswith(
            "systemd/user/huginn.service"))
        self.assertTrue(callable(install))
        self.assertTrue(callable(uninstall))

    def test_the_spec_reads_the_module_constants_at_call_time(self):
        # Built per call, not frozen at import: a fork with a relocated checkout
        # patches REPO_ROOT, and a snapshot would ignore it -- the failure that
        # once hardcoded one developer's path (issue #37).
        with unittest.mock.patch.object(huginn_agent_install, "REPO_ROOT", "/tmp/elsewhere"):
            self.assertEqual(huginn_agent_install.spec().working_dir, "/tmp/elsewhere")

    def test_the_argv_still_starts_the_same_daemon(self):
        argv = list(huginn_agent_install.spec().argv)
        self.assertEqual(argv[1:], ["-m", "huginn.cli", "serve", "--no-open"])


class RavenCompatTests(unittest.TestCase):
    """``huginn.raven``'s descriptor and label surface after the #42 extraction."""

    def test_state_dir_and_the_env_name_are_the_shared_objects(self):
        # Not lookalikes: a raven resolving this directory differently from the
        # host publishes where nothing is looking, and that failure is silent.
        self.assertIs(huginn_raven.state_dir, corvidae.state_dir)
        self.assertEqual(huginn_raven.STATE_DIR_ENV, corvidae.STATE_DIR_ENV)

    def test_the_host_caps_are_the_shared_values(self):
        self.assertEqual(huginn_raven.MAX_LABEL, corvidae.MAX_LABEL)
        self.assertEqual(huginn_raven.MAX_DETAIL, corvidae.MAX_DETAIL)

    def test_descriptor_path_still_names_huginns_own_file(self):
        self.assertEqual(huginn_raven.descriptor_path(),
                         corvidae.descriptor_path("huginn"))

    def test_safe_text_still_redacts_as_well_as_sanitises(self):
        # The composition is Huginn's decision, not corvidae's: sanitize_label
        # alone would leave the credential on screen, and Muninn deliberately does
        # not redact its labels.
        secret = "ghp_" + "a" * 36
        self.assertNotIn(secret, huginn_raven.safe_text(f"use {secret} to push"))
        self.assertIn(secret, corvidae.sanitize_label(f"use {secret} to push"))
        self.assertEqual(huginn_raven.safe_text("\x1b[31mQuit\x1b[0m"), "Quit")


if __name__ == "__main__":
    unittest.main()
