"""CalVer policy and release-metadata consistency."""
from __future__ import annotations

from datetime import date
import re
import tomllib
import unittest
from pathlib import Path

import corvidae

from huginn import __version__


REPO_ROOT = Path(__file__).parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CORVIDAE_PYPROJECT = REPO_ROOT / "packages" / "corvidae" / "pyproject.toml"
CALVER = re.compile(
    r"^(?P<year>\d{4})\."
    r"(?P<month>0[1-9]|1[0-2])\."
    r"(?P<day>0[1-9]|[12]\d|3[01])"
    r"(?:\.(?P<micro>0|[1-9]\d*))?$"
)


class VersionTests(unittest.TestCase):
    def test_runtime_version_matches_package_metadata(self):
        metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

        self.assertEqual(__version__, metadata["project"]["version"])

    def test_version_uses_calver(self):
        match = CALVER.fullmatch(__version__)

        self.assertIsNotNone(match)
        date(int(match["year"]), int(match["month"]), int(match["day"]))


class SharedPackageVersionTests(unittest.TestCase):
    """corvidae's compatibility promise is keyed to its CalVer *year* -- issue
    #42 -- so a malformed version isn't cosmetic, it makes the promise unreadable.
    """

    def test_runtime_version_matches_package_metadata(self):
        metadata = tomllib.loads(CORVIDAE_PYPROJECT.read_text(encoding="utf-8"))

        self.assertEqual(corvidae.__version__, metadata["project"]["version"])

    def test_version_uses_calver(self):
        match = CALVER.fullmatch(corvidae.__version__)

        self.assertIsNotNone(match)
        date(int(match["year"]), int(match["month"]), int(match["day"]))

    def test_huginn_pins_corvidae_within_one_calver_year(self):
        # A range that crossed a year boundary would silently accept a release
        # allowed to break the surface huginn re-exports.
        requirement = next(
            dep for dep in tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
            if dep.startswith("corvidae")
        )
        year = int(CALVER.fullmatch(corvidae.__version__)["year"])

        self.assertIn(f"<{year + 1}", requirement)


if __name__ == "__main__":
    unittest.main()
