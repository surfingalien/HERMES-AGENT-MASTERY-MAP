"""@-references in user messages (#30 file references).

    @file:src/app.py        inline a file (or @src/app.py when the path exists)
    @file:src/app.py:10-40  inline a line range
    @folder:src             directory listing
    @diff / @staged         git diff of the working tree / index
    @url:https://...        fetch a page's readable text
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

MAX_INLINE = 40000
_REF = re.compile(r"(?<![\w/])@(file:|folder:|url:)?((?:https?://\S+)|(?:[~\w./\\-]+(?::\d+-\d+)?)|diff\b|staged\b)")


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def expand_references(text: str, cwd: str) -> tuple[str, list[str]]:
    """Return (text with attachments appended, list of expanded refs)."""
    attachments, used = [], []
    for m in _REF.finditer(text):
        kind, target = m.group(1) or "", m.group(2)
        if not kind and target in ("diff", "staged"):
            out = _git(cwd, "diff", "--cached") if target == "staged" else _git(cwd, "diff")
            attachments.append(f"<attachment ref=\"@{target}\">\n{out[:MAX_INLINE] or '(no changes)'}\n</attachment>")
            used.append(f"@{target}")
            continue
        if kind == "url:" or (not kind and target.startswith("http")):
            if kind != "url:":
                continue
            from hermes.tools.web import fetch_page

            page = fetch_page(target, MAX_INLINE)
            attachments.append(f"<attachment ref=\"@url:{target}\">\n{page.get('content') or page.get('error')}\n</attachment>")
            used.append(target)
            continue
        rng = None
        rm = re.match(r"(.+):(\d+)-(\d+)$", target)
        if rm:
            target, rng = rm.group(1), (int(rm.group(2)), int(rm.group(3)))
        p = Path(target).expanduser()
        if not p.is_absolute():
            p = Path(cwd) / p
        if kind == "folder:" or (not kind and p.is_dir()):
            if p.is_dir():
                entries = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir() if not x.name.startswith("."))
                attachments.append(f"<attachment ref=\"@folder:{target}\">\n" + "\n".join(entries[:300]) + "\n</attachment>")
                used.append(str(p))
            continue
        if p.is_file() and (kind == "file:" or not kind):
            try:
                content = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if rng:
                lines = content.splitlines()
                content = "\n".join(lines[rng[0] - 1 : rng[1]])
            attachments.append(f"<attachment ref=\"@{target}\" path=\"{p}\">\n{content[:MAX_INLINE]}\n</attachment>")
            used.append(str(p))
    if not attachments:
        return text, []
    return text + "\n\n" + "\n\n".join(attachments), used
