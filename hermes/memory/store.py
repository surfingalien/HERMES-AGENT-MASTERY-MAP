"""Persistent curated memory (#21 MEMORY.md, #22 USER.md, #23 persistent memory,
#29 privacy boundaries).

Two bounded files under ``HERMES_HOME/memories``:

* ``MEMORY.md`` - the agent's notes: environment facts, conventions, lessons
* ``USER.md``   - who the user is: preferences, communication style, context

Entries are separated by ``\\n§\\n``. A *frozen snapshot* is injected into the
system prompt at session start (keeps the prompt cache stable); writes go to
disk immediately and become visible in the next session.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from hermes import config
from hermes.security.redact import contains_secret

DELIM = "\n§\n"

_INJECTION = re.compile(
    r"(?i)(ignore (all )?(previous|prior|above) instructions|disregard (the )?system prompt|you are now|"
    r"<\s*/?\s*system\s*>|new instructions:|exfiltrate|send (your|the) (api )?keys?)"
)


@dataclass
class MemoryFile:
    path: Path
    limit: int

    def entries(self) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8").strip()
        return [e.strip() for e in text.split(DELIM) if e.strip()] if text else []

    def write(self, entries: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(DELIM.join(entries) + ("\n" if entries else ""), encoding="utf-8")

    def size(self, entries: list[str] | None = None) -> int:
        return len(DELIM.join(self.entries() if entries is None else entries))


class MemoryStore:
    def __init__(self, cfg: dict | None = None, home: Path | None = None):
        mcfg = (cfg or {}).get("memory", {}) if cfg else {}
        base = (home or config.hermes_home()) / "memories"
        self.memory = MemoryFile(base / "MEMORY.md", int(mcfg.get("memory_char_limit", 2200)))
        self.user = MemoryFile(base / "USER.md", int(mcfg.get("user_char_limit", 1375)))
        self.lock = threading.Lock()

    def _file(self, target: str) -> MemoryFile:
        if target in ("memory", "agent", "notes"):
            return self.memory
        if target in ("user", "profile"):
            return self.user
        raise ValueError("target must be 'memory' or 'user'")

    @staticmethod
    def validate(content: str) -> str:
        if not content or not content.strip():
            return "content is empty"
        if contains_secret(content):
            return "refusing to store something that looks like a secret/credential"
        if _INJECTION.search(content):
            return "content looks like a prompt-injection attempt; not stored"
        if "§" in content:
            return "content may not contain the § delimiter"
        return ""

    def add(self, target: str, content: str) -> dict:
        err = self.validate(content)
        if err:
            return {"ok": False, "error": err}
        with self.lock:
            f = self._file(target)
            entries = f.entries()
            content = content.strip()
            if any(content == e for e in entries):
                return {"ok": True, "note": "already stored", "usage": self.usage(target)}
            new = entries + [content]
            if f.size(new) > f.limit:
                return {"ok": False, "error": f"{target} is full ({f.size(entries)}/{f.limit} chars). "
                        "Consolidate: use replace/remove to merge or drop older entries first.",
                        "entries": entries}
            f.write(new)
            return {"ok": True, "usage": self.usage(target)}

    def replace(self, target: str, old_text: str, content: str) -> dict:
        err = self.validate(content)
        if err:
            return {"ok": False, "error": err}
        with self.lock:
            f = self._file(target)
            entries = f.entries()
            hits = [i for i, e in enumerate(entries) if old_text in e]
            if not hits:
                return {"ok": False, "error": "no entry contains old_text"}
            if len(hits) > 1:
                return {"ok": False, "error": "old_text matches several entries; be more specific"}
            entries[hits[0]] = content.strip()
            if f.size(entries) > f.limit:
                return {"ok": False, "error": f"replacement would exceed the {f.limit}-char limit"}
            f.write(entries)
            return {"ok": True, "usage": self.usage(target)}

    def remove(self, target: str, old_text: str) -> dict:
        with self.lock:
            f = self._file(target)
            entries = f.entries()
            keep = [e for e in entries if old_text not in e]
            if len(keep) == len(entries):
                return {"ok": False, "error": "no entry contains old_text"}
            if len(entries) - len(keep) > 1:
                return {"ok": False, "error": "old_text matches several entries; be more specific"}
            f.write(keep)
            return {"ok": True, "usage": self.usage(target)}

    def usage(self, target: str) -> str:
        f = self._file(target)
        return f"{f.size()}/{f.limit} chars, {len(f.entries())} entries"

    def snapshot(self, include_user: bool = True, include_memory: bool = True) -> str:
        parts = []
        if include_memory:
            m = self.memory.entries()
            if m:
                parts.append(f"## Agent memory (MEMORY.md - {self.usage('memory')})\n" + "\n".join(f"- {e}" for e in m))
        if include_user:
            u = self.user.entries()
            if u:
                parts.append(f"## User profile (USER.md - {self.usage('user')})\n" + "\n".join(f"- {e}" for e in u))
        return "\n\n".join(parts)


class MemoryProvider:
    """Plugin interface for external memory backends (#28 memory providers).

    Built-in memory always runs; a provider *adds* recall. Register one from a
    plugin with ``ctx.register_memory_provider(MyProvider())`` and select it via
    ``memory.provider`` in config.yaml.
    """

    name = "base"

    def prefetch(self, query: str, session_id: str = "") -> str:
        """Return context to prepend to the user turn (e.g. semantic recall)."""
        return ""

    def sync_turn(self, user: str, assistant: str, session_id: str = "") -> None:
        """Called after each completed turn."""

    def on_session_end(self, session_id: str, messages: list[dict]) -> None:
        """Called when a session ends (extract facts, summarise, index...)."""


class SessionRecallProvider(MemoryProvider):
    """Built-in provider: prefetches relevant snippets from past sessions via FTS."""

    name = "session_recall"

    def __init__(self, db=None, limit: int = 3):
        self.db = db
        self.limit = limit

    def prefetch(self, query: str, session_id: str = "") -> str:
        if not self.db or len(query) < 12:
            return ""
        words = [w for w in re.findall(r"[A-Za-z0-9]{4,}", query)][:6]
        if not words:
            return ""
        hits = self.db.search(" OR ".join(words), limit=self.limit, exclude_session=session_id)
        if not hits:
            return ""
        lines = [f"- [{h['started_at'][:10]} {h['title'] or h['session_id']}] {h['snippet']}" for h in hits]
        return "Possibly relevant past conversations:\n" + "\n".join(lines)


MEMORY_PROVIDERS: dict[str, type[MemoryProvider] | MemoryProvider] = {"session_recall": SessionRecallProvider}


def register_memory_provider(provider: MemoryProvider) -> None:
    MEMORY_PROVIDERS[provider.name] = provider
