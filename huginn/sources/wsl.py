"""Read Claude Code and Codex sessions from WSL distributions.

The reader deliberately executes inside the distribution: opening SQLite over
``\\\\wsl.localhost`` is both slow and prone to inconsistent WAL snapshots.
Only normalized JSON crosses the Windows/WSL boundary.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ..model import Session, SessionState

_HELPER = r'''
import glob,json,os,sqlite3,time
out=[]
for p in glob.glob(os.path.expanduser('~/.claude/sessions/*.json')):
 try:
  x=json.load(open(p)); pid=x.get('pid') or int(os.path.basename(p)[:-5])
  if not x.get('sessionId') or x.get('entrypoint')=='sdk-cli': continue
  try: os.kill(pid,0)
  except OSError: continue
  ms=x.get('statusUpdatedAt') or x.get('updatedAt') or x.get('startedAt') or 0
  out.append({'source':'claude','id':x['sessionId'],'pid':pid,'cwd':x.get('cwd',''),
   'name':x.get('name'),'state':'working' if x.get('status') in ('busy','shell') else 'idle',
   'updated':ms/1000 if ms else os.path.getmtime(p),'entrypoint':x.get('entrypoint'),
   'version':x.get('version')})
 except Exception: pass
db=os.path.expanduser(os.environ.get('CODEX_HOME','~/.codex')+'/state_5.sqlite')
try:
 con=sqlite3.connect('file:'+db+'?mode=ro',uri=True)
 cols={r[1] for r in con.execute('pragma table_info(threads)')}
 wanted=['id','cwd','updated_at_ms','recency_at_ms','created_at','source','model','git_branch','first_user_message','tokens_used','cli_version','archived']
 use=[c for c in wanted if c in cols]
 for row in con.execute('select '+','.join(use)+' from threads'):
  x=dict(zip(use,row))
  if x.get('archived'): continue
  updated=x.get('updated_at_ms') or x.get('recency_at_ms') or x.get('created_at') or 0
  if updated>100000000000: updated/=1000
  if time.time()-updated > 86400: continue
  x.update(source='codex',updated=updated); out.append(x)
 con.close()
except Exception: pass
print(json.dumps(out,separators=(',',':')))
'''


def _namespace(distro: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", distro).strip("-") or "default"


def windows_path(path: str) -> str:
    """Translate WSL's /mnt/c/... convention for native editor launching."""
    match = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", path)
    if not match:
        return path
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{match.group(1).upper()}:\\{rest}" if rest else f"{match.group(1).upper()}:\\"


def _session(raw: dict[str, Any], distro: str) -> Session | None:
    source, sid = raw.get("source"), str(raw.get("id") or "")
    if source not in {"claude", "codex"} or not sid:
        return None
    updated = float(raw.get("updated") or 0)
    state_name = raw.get("state")
    if source == "codex":
        import time
        age = time.time() - updated
        state_name = "working" if age < 15 else "done" if age < 600 else "idle"
    try:
        state = SessionState(state_name or "idle")
    except ValueError:
        state = SessionState.IDLE
    cwd = windows_path(str(raw.get("cwd") or ""))
    ns = _namespace(distro)
    name = raw.get("name") or (Path(cwd).name if cwd else source)
    if source == "codex":
        name = f"{name}-{sid[-4:]}"
    return Session(
        key=f"wsl:{ns}:{source}:{sid}", source=source, session_id=sid,
        cwd=cwd, name=f"{name} · {distro}", pid=None,
        git_branch=raw.get("git_branch"), model=raw.get("model"),
        entrypoint=f"wsl:{raw.get('entrypoint') or raw.get('source') or 'cli'}",
        state=state, state_since=updated, state_origin="poll",
        last_activity=updated,
        last_prompt=(raw.get("first_user_message") or "")[:300] or None,
        tokens=raw.get("tokens_used"), version=raw.get("version") or raw.get("cli_version"),
    )


def scan(distro: str = "", *, timeout: float = 8.0) -> tuple[list[Session], bool]:
    """Return sessions and whether the bounded WSL probe succeeded."""
    cmd = ["wsl.exe"]
    if distro:
        cmd += ["--distribution", distro]
    cmd += ["--exec", "python3", "-c", _HELPER]
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout,
                              "check": False}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, **kwargs)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return [], False
    if proc.returncode != 0:
        return [], False
    try:
        rows = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return [], False
    if not isinstance(rows, list):
        return [], False
    label = distro or "default"
    sessions = [s for row in rows if isinstance(row, dict) if (s := _session(row, label))]
    return sessions, True
