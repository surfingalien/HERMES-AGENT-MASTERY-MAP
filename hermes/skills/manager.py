"""Skills system (#31-#40): SKILL.md procedures with progressive disclosure.

Layout (agentskills.io compatible)::

    ~/.hermes/skills/<category>/<name>/SKILL.md
                                       references/  templates/  scripts/  assets/

Progressive disclosure:
  level 0 - name + description of every skill in the system prompt (cheap)
  level 1 - ``skill_view(name)`` loads the full SKILL.md
  level 2 - ``skill_view(name, file_path=...)`` loads a supporting file
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hermes import config

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_SKILL_CHARS = 100_000
BUNDLED_DIR = Path(__file__).resolve().parent.parent / "bundled_skills"

THREAT_PATTERNS = [
    (r"curl[^\n|]*\|\s*(ba)?sh", "pipes a remote script into a shell"),
    (r"base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh", "decodes and executes hidden payload"),
    (r"rm\s+-rf\s+(/|~|\$HOME)(\s|$)", "deletes the filesystem/home"),
    (r"(env|printenv|\$\{?[A-Z_]*(KEY|TOKEN|SECRET))[^\n]*(curl|wget|nc)\s", "may exfiltrate secrets"),
    (r"(?i)ignore (all )?(previous|prior) instructions", "prompt injection"),
    (r"\.ssh/(id_|authorized_keys)", "touches SSH keys"),
    (r"(?i)crypto ?miner|xmrig", "crypto mining"),
]


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    category: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def skill_md(self) -> Path:
        return self.path / "SKILL.md"

    def body(self) -> str:
        return self.skill_md.read_text(encoding="utf-8")

    def files(self) -> list[str]:
        out = []
        for p in sorted(self.path.rglob("*")):
            if p.is_file() and p.name != "SKILL.md" and ".hub" not in p.parts:
                out.append(str(p.relative_to(self.path)))
        return out[:200]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        meta = {}
    body = text[end + 4 :].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


def validate_skill(content: str, expected_name: str | None = None) -> list[str]:
    errs = []
    meta, body = parse_frontmatter(content)
    if not meta:
        errs.append("missing YAML frontmatter (--- name: ... description: ... ---)")
        return errs
    name = str(meta.get("name") or "")
    if not NAME_RE.match(name):
        errs.append("frontmatter 'name' must be lowercase letters, digits, '-', '_' or '.' (max 64)")
    if expected_name and name and name != expected_name:
        errs.append(f"frontmatter name '{name}' does not match '{expected_name}'")
    desc = str(meta.get("description") or "")
    if not desc:
        errs.append("frontmatter 'description' is required (say WHAT it does and WHEN to use it)")
    elif len(desc) > 1024:
        errs.append("description is longer than 1024 characters")
    if not body.strip():
        errs.append("skill body is empty")
    if len(content) > MAX_SKILL_CHARS:
        errs.append(f"SKILL.md exceeds {MAX_SKILL_CHARS} characters - move detail into references/")
    return errs


def scan_threats(text: str) -> list[str]:
    return [why for pat, why in THREAT_PATTERNS if re.search(pat, text)]


def _platform_ok(meta: dict) -> bool:
    plats = meta.get("platforms")
    if not plats:
        return True
    current = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), platform.system().lower())
    return current in [str(p).lower() for p in plats]


class SkillManager:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.root = config.path("skills")
        scfg = self.cfg.get("skills", {}) or {}
        self.external = [Path(p).expanduser() for p in scfg.get("external_dirs", [])]
        self.disabled = set(scfg.get("disabled", []))

    # ---------------------------------------------------------------- discovery
    def sync_bundled(self) -> list[str]:
        """Copy bundled skills into HERMES_HOME/skills without clobbering user edits."""
        copied = []
        if not BUNDLED_DIR.exists():
            return copied
        manifest_path = self.root / ".bundled_manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        for skill_md in BUNDLED_DIR.rglob("SKILL.md"):
            src = skill_md.parent
            rel = src.relative_to(BUNDLED_DIR)
            dst = self.root / rel
            digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
            if dst.exists():
                current = hashlib.sha256((dst / "SKILL.md").read_bytes()).hexdigest() if (dst / "SKILL.md").exists() else ""
                if manifest.get(str(rel)) != current or current == digest:
                    continue  # user modified it, or unchanged
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            manifest[str(rel)] = digest
            copied.append(str(rel))
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return copied

    def _roots(self) -> list[Path]:
        return [self.root] + [p for p in self.external if p.exists()]

    def discover(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for root in self._roots():
            if not root.exists():
                continue
            for skill_md in sorted(root.rglob("SKILL.md")):
                if any(part.startswith(".") for part in skill_md.relative_to(root).parts[:-1]):
                    continue
                try:
                    meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
                except OSError:
                    continue
                name = str(meta.get("name") or skill_md.parent.name)
                if name in seen or name in self.disabled or not _platform_ok(meta):
                    continue
                rel = skill_md.parent.relative_to(root)
                category = rel.parts[0] if len(rel.parts) > 1 else ""
                seen[name] = Skill(name, str(meta.get("description") or ""), skill_md.parent, category, meta)
        return sorted(seen.values(), key=lambda s: (s.category, s.name))

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.discover() if s.name == name), None)

    def index_for_prompt(self, max_chars: int = 6000) -> str:
        skills = self.discover()
        if not skills:
            return ""
        lines, cat = [], None
        for s in skills:
            if s.category != cat:
                cat = s.category
                lines.append(f"[{cat or 'general'}]")
            lines.append(f"- {s.name}: {s.description[:200]}")
        text = "\n".join(lines)
        return text[:max_chars] + ("\n... (use skills_list for more)" if len(text) > max_chars else "")

    # ---------------------------------------------------------------- level 1/2
    def view(self, name: str, file_path: str | None = None) -> dict:
        s = self.get(name)
        if not s:
            return {"error": f"skill '{name}' not found"}
        if file_path:
            target = (s.path / file_path).resolve()
            if not str(target).startswith(str(s.path.resolve()) + "/") or not target.is_file():
                return {"error": f"file '{file_path}' not found in skill '{name}'"}
            return {"name": name, "file": file_path, "content": target.read_text(encoding="utf-8", errors="replace")[:60000]}
        return {"name": name, "path": str(s.path), "content": s.body(), "files": s.files()}

    # ---------------------------------------------------------------- authoring (#36, #40)
    def _owned_dir(self, name: str) -> Path | None:
        s = self.get(name)
        if s and str(s.path).startswith(str(self.root)):
            return s.path
        return None

    def create(self, name: str, content: str, category: str = "") -> dict:
        errs = validate_skill(content, name)
        if errs:
            return {"ok": False, "errors": errs}
        if self.get(name):
            return {"ok": False, "errors": [f"skill '{name}' already exists - use patch or edit"]}
        if category and not NAME_RE.match(category):
            return {"ok": False, "errors": ["invalid category name"]}
        d = self.root / category / name if category else self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(d / "SKILL.md"), "warnings": scan_threats(content)}

    def edit(self, name: str, content: str) -> dict:
        d = self._owned_dir(name)
        if not d:
            return {"ok": False, "errors": [f"skill '{name}' not found in {self.root}"]}
        errs = validate_skill(content, name)
        if errs:
            return {"ok": False, "errors": errs}
        (d / "SKILL.md").write_text(content, encoding="utf-8")
        return {"ok": True, "warnings": scan_threats(content)}

    def patch(self, name: str, old: str, new: str, file_path: str | None = None) -> dict:
        d = self._owned_dir(name)
        if not d:
            return {"ok": False, "errors": [f"skill '{name}' not found in {self.root}"]}
        target = (d / (file_path or "SKILL.md")).resolve()
        if not str(target).startswith(str(d.resolve())) or not target.exists():
            return {"ok": False, "errors": ["file not found in skill"]}
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            return {"ok": False, "errors": [f"old_string must occur exactly once (found {count})"]}
        updated = text.replace(old, new, 1)
        if target.name == "SKILL.md":
            errs = validate_skill(updated, name)
            if errs:
                return {"ok": False, "errors": errs}
        target.write_text(updated, encoding="utf-8")
        return {"ok": True}

    def delete(self, name: str) -> dict:
        d = self._owned_dir(name)
        if not d:
            return {"ok": False, "errors": [f"skill '{name}' not found in {self.root}"]}
        shutil.rmtree(d)
        return {"ok": True}

    def write_file(self, name: str, file_path: str, content: str) -> dict:
        d = self._owned_dir(name)
        if not d:
            return {"ok": False, "errors": [f"skill '{name}' not found"]}
        rel = Path(file_path)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] not in ("references", "templates", "scripts", "assets"):
            return {"ok": False, "errors": ["file_path must be inside references/, templates/, scripts/ or assets/"]}
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(target), "warnings": scan_threats(content)}

    def remove_file(self, name: str, file_path: str) -> dict:
        d = self._owned_dir(name)
        if not d:
            return {"ok": False, "errors": [f"skill '{name}' not found"]}
        target = (d / file_path).resolve()
        if not str(target).startswith(str(d.resolve()) + "/") or target.name == "SKILL.md" or not target.exists():
            return {"ok": False, "errors": ["cannot remove that file"]}
        target.unlink()
        return {"ok": True}

    # ---------------------------------------------------------------- lint (#89 focused skills)
    def check(self) -> list[dict]:
        report = []
        for s in self.discover():
            content = s.body()
            issues = validate_skill(content)
            _, body = parse_frontmatter(content)
            if len(body) > 15000:
                issues.append("body is long; split detail into references/ to keep the skill focused")
            if len(s.description) < 30:
                issues.append("description is very short; say what it does AND when to use it")
            issues += [f"security: {w}" for w in scan_threats(content)]
            report.append({"name": s.name, "path": str(s.path), "issues": issues})
        return report

    def slash_prompt(self, name: str, args: str = "") -> str | None:
        """Turn ``/skill-name args`` into a user message that loads the skill."""
        s = self.get(name)
        if not s:
            return None
        return (f"[Skill invoked: {s.name}]\n\n{s.body()}\n\n---\nFollow the skill above."
                + (f"\n\nUser request: {args}" if args else ""))
