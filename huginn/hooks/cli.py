"""Portable, best-effort hook forwarder used by Claude Code and Codex."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from .. import config


def main() -> int:
    # Hooks must never disrupt the coding-agent process, including when Huginn
    # is stopped or its state files are temporarily unavailable.
    try:
        source, event = sys.argv[1:3]
        port_path = config.STATE_DIR / "port"
        port = port_path.read_text().strip() if port_path.exists() else "47100"
        token_path = config.TOKEN_PATH
        token = token_path.read_text().strip() if token_path.exists() else ""
        payload = sys.stdin.buffer.read() or b"{}"
        json.loads(payload)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/hook/{source}/{event}",
            data=payload,
            headers={"Content-Type": "application/json", "X-Huginn-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=1):
            pass
    except (IndexError, ValueError, OSError, json.JSONDecodeError, urllib.error.URLError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
