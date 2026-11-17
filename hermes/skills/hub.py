"""Skills Hub (#38 Skills Hub, #39 agentskills.io).

Install community skills from:
  * a local directory containing SKILL.md
  * a direct URL to a SKILL.md file
  * a GitHub path ``github:owner/repo/path/to/skill`` (or ``owner/repo/path``)
  * a skills index (``taps``) - JSON ``{"skills": [{"name","description","source"}]}``,
    e.g. a site's ``/.well-known/skills/index.json``

Every install is security-scanned (blocked unless ``force``), validated, and
recorded in ``skills/.hub/lock.json`` with its source and content hash.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path

from hermes import config
from hermes.providers import http
from hermes.skills.manager import SkillManager, parse_frontmatter, scan_threats, validate_skill

DEFAULT_TAPS: list[str] = []


def _lock_path() -> Path:
    return config.path("skills", ".hub", "lock.json")


def read_lock() -> dict:
    p = _lock_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _write_lock(data: dict) -> None:
    p = _lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _hash_dir(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _github_fetch(owner: str, repo: str, path: str, dest: Path, ref: str = "") -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}" + (f"?ref={ref}" if ref else "")
    items = http.get_json(url, {"Accept": "application/vnd.github+json"})
    if isinstance(items, dict):
        items = [items]
    for item in items:
        if item["type"] == "file":
            _, _, body = http.request("GET", item["download_url"], timeout=60)
            target = dest / item["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        elif item["type"] == "dir":
            _github_fetch(owner, repo, item["path"], dest / item["name"], ref)


def fetch_to_staging(source: str) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="hermes-skill-"))
    src = source.strip()
    local = Path(src).expanduser()
    if local.exists():
        base = local.parent if local.name == "SKILL.md" else local
        shutil.copytree(base, staging, dirs_exist_ok=True)
    elif src.startswith(("http://", "https://")) and src.endswith(".md"):
        _, text = http.get_text(src)
        (staging / "SKILL.md").write_text(text, encoding="utf-8")
    elif src.startswith("https://github.com/"):
        parts = urllib.parse.urlparse(src).path.strip("/").split("/")
        owner, repo = parts[0], parts[1]
        ref, rest = "", parts[2:]
        if rest[:1] in (["tree"], ["blob"]):
            ref, rest = rest[1], rest[2:]
        _github_fetch(owner, repo, "/".join(rest), staging, ref)
    else:
        spec = src[len("github:"):] if src.startswith("github:") else src
        parts = spec.split("/")
        if len(parts) < 2:
            raise ValueError(f"unrecognised skill source: {source}")
        _github_fetch(parts[0], parts[1], "/".join(parts[2:]), staging)
    if not (staging / "SKILL.md").exists():
        raise ValueError("source does not contain a SKILL.md")
    return staging


def install(source: str, category: str = "community", force: bool = False, cfg: dict | None = None) -> dict:
    staging = fetch_to_staging(source)
    try:
        content = (staging / "SKILL.md").read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(content)
        name = str(meta.get("name") or "")
        errs = validate_skill(content)
        if errs:
            return {"ok": False, "errors": errs}
        threats = []
        for p in staging.rglob("*"):
            if p.is_file() and p.stat().st_size < 2_000_000:
                threats += [f"{p.name}: {t}" for t in scan_threats(p.read_text(encoding="utf-8", errors="ignore"))]
        if threats and not force:
            return {"ok": False, "errors": ["security scan blocked install (use --force to override):"] + threats}
        mgr = SkillManager(cfg)
        existing = mgr.get(name)
        dest = existing.path if existing else config.path("skills", category, name)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(staging, dest)
        lock = read_lock()
        lock[name] = {"source": source, "hash": _hash_dir(dest), "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "path": str(dest), "version": str(meta.get("version", ""))}
        _write_lock(lock)
        return {"ok": True, "name": name, "path": str(dest), "warnings": threats}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall(name: str) -> dict:
    lock = read_lock()
    entry = lock.pop(name, None)
    if not entry:
        return {"ok": False, "errors": [f"'{name}' was not installed from the hub"]}
    shutil.rmtree(entry["path"], ignore_errors=True)
    _write_lock(lock)
    return {"ok": True}


def check_updates() -> list[dict]:
    out = []
    for name, entry in read_lock().items():
        try:
            staging = fetch_to_staging(entry["source"])
            remote = _hash_dir(staging)
            shutil.rmtree(staging, ignore_errors=True)
            local = _hash_dir(Path(entry["path"])) if Path(entry["path"]).exists() else ""
            out.append({"name": name, "update_available": remote != entry["hash"], "locally_modified": local != entry["hash"]})
        except Exception as exc:
            out.append({"name": name, "error": str(exc)})
    return out


def search(query: str, taps: list[str] | None = None) -> list[dict]:
    q = query.lower()
    results = []
    for tap in (taps or []) + DEFAULT_TAPS:
        try:
            idx = http.get_json(tap)
        except Exception as exc:
            results.append({"tap": tap, "error": str(exc)})
            continue
        for s in idx.get("skills", []):
            hay = f"{s.get('name', '')} {s.get('description', '')} {' '.join(s.get('tags', []))}".lower()
            if q in hay:
                results.append({"name": s.get("name"), "description": s.get("description", ""), "source": s.get("source") or s.get("url"), "tap": tap})
    return results
