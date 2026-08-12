"""Thin wrapper around `git` for environment/commit metadata and post-hoc blob reads."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run(args: list[str], cwd: Path) -> str | None:
    if not _git_available():
        return None
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return out.stdout.strip() or None


def head_commit(cwd: Path | str) -> str | None:
    """`git rev-parse HEAD` at cwd, or None if not a repo."""
    p = Path(cwd) if cwd else None
    if not p or not p.exists():
        return None
    return _run(["rev-parse", "HEAD"], p)


def current_branch(cwd: Path | str) -> str | None:
    p = Path(cwd) if cwd else None
    if not p or not p.exists():
        return None
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], p)


def blob_at_commit(cwd: Path | str, commit: str, path: str) -> bytes | None:
    """`git show <commit>:<path>` bytes, or None on miss."""
    p = Path(cwd) if cwd else None
    if not p or not p.exists() or not commit:
        return None
    if not _git_available():
        return None
    try:
        out = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=str(p),
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return out.stdout
