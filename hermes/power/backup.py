"""Backups (#90): archive and restore HERMES_HOME (config, memory, skills, sessions, cron)."""

from __future__ import annotations

import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from hermes import config

EXCLUDE = {"cache", "logs", "checkpoints"}


def create_backup(output: str | None = None, include_secrets: bool = False) -> Path:
    home = config.hermes_home()
    out = Path(output or Path.cwd() / f"hermes-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        snap = None
        db = home / "state.db"
        if db.exists():  # consistent copy of a live SQLite db
            snap = Path(tmp) / "state.db"
            src, dst = sqlite3.connect(db), sqlite3.connect(snap)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        with tarfile.open(out, "w:gz") as tar:
            for item in sorted(home.iterdir()):
                if item.name in EXCLUDE or item.name.startswith("state.db"):
                    continue
                if item.name == ".env" and not include_secrets:
                    continue
                tar.add(item, arcname=item.name)
            if snap:
                tar.add(snap, arcname="state.db")
    out.chmod(0o600)
    return out


def restore_backup(archive: str, force: bool = False) -> Path:
    home = config.hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = []
        for m in tar.getmembers():
            target = (home / m.name).resolve()
            if not str(target).startswith(str(home.resolve())) or m.issym() or m.islnk():
                raise ValueError(f"unsafe path in archive: {m.name}")
            if target.exists() and not force and m.isfile():
                raise FileExistsError(f"{target} exists - pass --force to overwrite")
            members.append(m)
        try:
            tar.extractall(home, members=members, filter="data")
        except TypeError:  # Python without tarfile extraction filters; members were validated above
            tar.extractall(home, members=members)
    return home
