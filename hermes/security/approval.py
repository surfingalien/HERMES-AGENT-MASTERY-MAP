"""Approval of risky actions (#82 approve risky actions, #84 review tool permissions).

Dangerous terminal commands are detected by pattern. Depending on
``approvals.mode`` the user is always asked (manual), a model-based judge
pre-screens and only escalates commands it considers risky (smart), or
everything runs (off / --yolo). Decisions can be remembered for
the session or permanently (written to ``approvals.allowlist``).
"""

from __future__ import annotations

import fnmatch
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\*|\$HOME|\.\s*$|\.\.)", "recursive/forced delete of a broad path"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r", "rm -rf"),
    (r"\bmkfs(\.\w+)?\b", "format a filesystem"),
    (r"\bdd\b[^|]*\bof=/dev/", "raw write to a device"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "overwrite a block device"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bchmod\s+(-R\s+)?0?777\b", "world-writable permissions"),
    (r"\bchown\s+-R\b.*\s/(\s|$)", "recursive chown of /"),
    (r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "pipe remote script into a shell"),
    (r"\bsudo\b", "privilege escalation (sudo)"),
    (r"\bgit\s+push\b.*(--force|-f\b)", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "discard git changes"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "delete untracked files"),
    (r"(?i)\bdrop\s+(table|database|schema)\b", "drop database objects"),
    (r"(?i)\btruncate\s+table\b", "truncate table"),
    (r"(?i)\bdelete\s+from\s+\w+\s*(;|$)", "delete all rows"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "power state change"),
    (r"\bkill(all)?\s+-9\s+(-1|1)\b", "kill all processes"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "stop system services"),
    (r">\s*/etc/|\btee\s+/etc/", "write to /etc"),
    (r"\b(crontab\s+-r)\b", "remove crontab"),
    (r"\biptables\s+-F\b", "flush firewall"),
    (r"\bdocker\s+(system\s+prune|rm\s+-f)", "destructive docker cleanup"),
    (r"\bnpm\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b", "publish a package"),
    (r"(^|\s)history\s+-c\b", "clear shell history"),
    (r"\b(cat|less|head|tail)\s+[^|;]*(\.ssh/id_|\.env\b|credentials)", "read credentials"),
]

_compiled = [(re.compile(p), why) for p, why in DANGEROUS_PATTERNS]


def detect_dangerous(command: str) -> str:
    """Return a human reason if the command looks dangerous, else ''."""
    for pat, why in _compiled:
        if pat.search(command):
            return why
    return ""


@dataclass
class ApprovalManager:
    mode: str = "manual"  # manual | smart | off
    allowlist: list[str] = field(default_factory=list)
    prompt: Callable[[str, str], str] | None = None  # returns once|session|always|deny
    judge: Callable[[str, str], bool] | None = None  # smart mode: True = clearly safe in context
    on_always: Callable[[str], None] | None = None
    session_allowed: set[str] = field(default_factory=set)
    log: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _allowlisted(self, command: str) -> bool:
        return any(fnmatch.fnmatch(command, pat) or command.startswith(pat) for pat in self.allowlist)

    def check(self, command: str, reason: str = "") -> bool:
        """Return True if the action may proceed."""
        reason = reason or detect_dangerous(command)
        if not reason:
            return True
        if self.mode == "off":
            self._record(command, reason, "auto-yolo")
            return True
        key = reason
        if key in self.session_allowed or self._allowlisted(command):
            self._record(command, reason, "allowlisted")
            return True
        if self.mode == "smart" and self.judge is not None:
            try:
                if self.judge(command, reason):
                    self._record(command, reason, "approved-smart")
                    return True
            except Exception:
                pass  # judge failure -> fall through to the human
        if self.prompt is None:
            self._record(command, reason, "denied-no-approver")
            return False
        with self._lock:
            choice = (self.prompt(command, reason) or "deny").strip().lower()
        if choice in ("y", "yes", "once", "o"):
            self._record(command, reason, "approved-once")
            return True
        if choice in ("s", "session"):
            self.session_allowed.add(key)
            self._record(command, reason, "approved-session")
            return True
        if choice in ("a", "always"):
            self.allowlist.append(command)
            if self.on_always:
                self.on_always(command)
            self._record(command, reason, "approved-always")
            return True
        self._record(command, reason, "denied")
        return False

    def _record(self, command: str, reason: str, outcome: str) -> None:
        self.log.append({"command": command, "reason": reason, "outcome": outcome})


def cli_prompt(command: str, reason: str) -> str:
    print(f"\n⚠  Approval needed: {reason}\n   $ {command}")
    try:
        ans = input("   [o]nce / [s]ession / [a]lways / [d]eny > ")
    except EOFError:
        return "deny"
    return {"o": "once", "s": "session", "a": "always"}.get(ans.strip().lower()[:1], "deny")
