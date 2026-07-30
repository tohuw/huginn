"""CalVer policy and release-metadata consistency."""
from __future__ import annotations

from datetime import date
import re
import tomllib
import unittest
from pathlib import Path

from huginn import __version__


PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"
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


if __name__ == "__main__":
    unittest.main()
