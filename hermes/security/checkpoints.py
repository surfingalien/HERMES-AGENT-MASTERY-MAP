"""Filesystem checkpoints before edits (#85 checkpoints before edits).

A shadow git repository per working directory lives in
``HERMES_HOME/checkpoints/<hash>``. It never touches the project's own .git.
Before the first file mutation of each agent turn a snapshot is committed;
``/rollback`` restores any snapshot.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import subprocess
from pathlib import Path

from hermes import config

_EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".git/", "*.pyc", ".DS_Store", "dist/", "build/", ".cache/"]
MAX_FILES = 20000


class CheckpointManager:
    def __init__(self, enabled: bool = True, max_snapshots: int = 50):
        self.enabled = enabled
        self.max_snapshots = max_snapshots
        self._snapshotted_this_turn: set[str] = set()

    def new_turn(self) -> None:
        self._snapshotted_this_turn.clear()

    @staticmethod
    def _shadow(workdir: Path) -> Path:
        digest = hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:16]
        return config.path("checkpoints", digest)

    def _git(self, workdir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        shadow = self._shadow(workdir)
        env = dict(os.environ, GIT_DIR=str(shadow), GIT_WORK_TREE=str(workdir),
                   GIT_AUTHOR_NAME="hermes", GIT_AUTHOR_EMAIL="hermes@localhost",
                   GIT_COMMITTER_NAME="hermes", GIT_COMMITTER_EMAIL="hermes@localhost")
        return subprocess.run(["git", *args], cwd=workdir, env=env, capture_output=True, text=True, check=check, timeout=60)

    def _init(self, workdir: Path) -> None:
        shadow = self._shadow(workdir)
        if (shadow / "HEAD").exists():
            return
        shadow.mkdir(parents=True, exist_ok=True)
        self._git(workdir, "init", "-q")
        (shadow / "info").mkdir(exist_ok=True)
        (shadow / "info" / "exclude").write_text("\n".join(_EXCLUDES) + "\n")
        (shadow / "HERMES_WORKDIR").write_text(str(workdir.resolve()))

    @staticmethod
    def workdir_for(file_path: str) -> Path:
        p = Path(file_path).expanduser().resolve()
        start = p.parent if not p.is_dir() else p
        for cand in [start, *start.parents]:
            if (cand / ".git").exists() or (cand / "pyproject.toml").exists() or (cand / "package.json").exists():
                return cand
        return start

    def snapshot_for(self, file_path: str, label: str = "auto") -> str | None:
        """Snapshot the project containing ``file_path`` once per turn."""
        if not self.enabled:
            return None
        workdir = self.workdir_for(file_path)
        if str(workdir) in self._snapshotted_this_turn or workdir in (Path("/"), Path.home()):
            return None
        self._snapshotted_this_turn.add(str(workdir))
        return self.snapshot(workdir, label)

    def snapshot(self, workdir: Path, label: str = "manual") -> str | None:
        try:
            workdir = Path(workdir).resolve()
            count = sum(1 for _ in itertools.islice(workdir.rglob("*"), MAX_FILES + 1))
            if count > MAX_FILES:
                return None
            self._init(workdir)
            self._git(workdir, "add", "-A")
            res = self._git(workdir, "commit", "-q", "--allow-empty", "-m", label, check=False)
            if res.returncode != 0:
                return None
            sha = self._git(workdir, "rev-parse", "--short", "HEAD").stdout.strip()
            self._prune(workdir)
            return sha
        except (OSError, subprocess.SubprocessError):
            return None

    def _prune(self, workdir: Path) -> None:
        log = self.list(workdir)
        if len(log) > self.max_snapshots:
            # Keep it simple: squash history by re-rooting at the oldest kept snapshot.
            keep = log[self.max_snapshots - 1]["sha"]
            self._git(workdir, "replace", "--graft", keep, check=False)

    def list(self, workdir: Path) -> list[dict]:
        workdir = Path(workdir).resolve()
        if not (self._shadow(workdir) / "HEAD").exists():
            return []
        res = self._git(workdir, "log", "--format=%h|%ci|%s", check=False)
        out = []
        for line in res.stdout.splitlines():
            sha, date, msg = (line.split("|", 2) + ["", ""])[:3]
            out.append({"sha": sha, "date": date, "label": msg})
        return out

    def diff(self, workdir: Path, sha: str) -> str:
        workdir = Path(workdir).resolve()
        self._git(workdir, "add", "-A", check=False)
        return self._git(workdir, "diff", "--stat", "--cached", sha, check=False).stdout

    def rollback(self, workdir: Path, sha: str, file: str | None = None) -> str:
        workdir = Path(workdir).resolve()
        if not (self._shadow(workdir) / "HEAD").exists():
            return "no checkpoints for this directory"
        # Safety snapshot of the current state so rollback is itself undoable.
        self.snapshot(workdir, f"pre-rollback to {sha}")
        if file:
            self._git(workdir, "checkout", sha, "--", file)
            return f"restored {file} from {sha}"
        # Remove files created after the snapshot, then restore tracked content.
        created = self._git(workdir, "diff", "--name-only", "--diff-filter=A", sha, "HEAD", check=False).stdout.split()
        for rel in created:
            try:
                (workdir / rel).unlink()
            except OSError:
                pass
        self._git(workdir, "checkout", sha, "--", ".")
        return f"rolled back {workdir} to {sha}"
