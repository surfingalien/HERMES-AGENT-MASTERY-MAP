"""File tools: read_file (#15), write_file, patch (#16), search_files."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from hermes.tools.registry import ToolContext, ToolError, tool

MAX_READ_BYTES = 2_000_000
DEFAULT_LIMIT = 500


def _protected(ctx: ToolContext, path: Path) -> bool:
    protected = (ctx.config.get("security", {}) or {}).get("protected_paths", [])
    rp = str(path.resolve())
    for p in protected:
        base = str(Path(os.path.expanduser(p)).resolve())
        if rp == base or rp.startswith(base.rstrip("/") + "/"):
            return True
    return False


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


@tool(
    "read_file",
    "file",
    "Read a text file with line numbers. Use offset/limit to page through large files.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or relative to cwd)"},
            "offset": {"type": "integer", "description": "1-based line to start from", "default": 1},
            "limit": {"type": "integer", "description": "Max lines to return", "default": DEFAULT_LIMIT},
        },
        "required": ["path"],
    },
)
def read_file(args: dict, ctx: ToolContext):
    path = ctx.resolve_path(args["path"])
    if _protected(ctx, path):
        raise ToolError(f"{path} is a protected path (security.protected_paths)")
    if not path.exists():
        raise ToolError(f"file not found: {path}")
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return {"path": str(path), "directory": True, "entries": entries[:500]}
    if path.stat().st_size > MAX_READ_BYTES * 10:
        raise ToolError("file too large; use terminal tools (head/grep) instead")
    data = path.read_bytes()
    if _is_binary(data):
        return {"path": str(path), "binary": True, "size": len(data)}
    lines = data.decode("utf-8", "replace").splitlines()
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), 2000))
    chunk = lines[offset - 1 : offset - 1 + limit]
    width = len(str(offset + len(chunk)))
    body = "\n".join(f"{str(i).rjust(width)}|{line[:2000]}" for i, line in enumerate(chunk, start=offset))
    more = offset - 1 + limit < len(lines)
    header = f"{path} ({len(lines)} lines)" + (f" - showing {offset}-{offset + len(chunk) - 1}; use offset={offset + limit} for more" if more else "")
    return header + "\n" + body


@tool(
    "write_file",
    "file",
    "Create or overwrite a file with the given content. Parent dirs are created. A checkpoint is taken first.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    parallel_safe=False,
)
def write_file(args: dict, ctx: ToolContext):
    path = ctx.resolve_path(args["path"])
    if _protected(ctx, path):
        raise ToolError(f"{path} is a protected path")
    if ctx.checkpoint:
        ctx.checkpoint(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(args["content"], encoding="utf-8")
    return {"path": str(path), "bytes": len(args["content"].encode()), "created": not existed}


def _fuzzy_find(content: str, old: str) -> tuple[int, int] | None:
    """Whitespace-tolerant search: match lines ignoring indentation/trailing spaces."""
    old_lines = [line.strip() for line in old.strip("\n").splitlines()]
    lines = content.splitlines(keepends=True)
    n = len(old_lines)
    if not n:
        return None
    hits = []
    for i in range(len(lines) - n + 1):
        if [line.strip() for line in lines[i : i + n]] == old_lines:
            hits.append(i)
    if len(hits) != 1:
        return None
    start = sum(len(x) for x in lines[: hits[0]])
    end = start + sum(len(x) for x in lines[hits[0] : hits[0] + n])
    return start, end


@tool(
    "patch",
    "file",
    "Edit a file by replacing old_string with new_string. old_string must be unique unless replace_all=true. "
    "Falls back to whitespace-insensitive matching.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace (include surrounding context)"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_string", "new_string"],
    },
    parallel_safe=False,
)
def patch(args: dict, ctx: ToolContext):
    path = ctx.resolve_path(args["path"])
    if _protected(ctx, path):
        raise ToolError(f"{path} is a protected path")
    if not path.exists():
        raise ToolError(f"file not found: {path}")
    content = path.read_text(encoding="utf-8")
    old, new = args["old_string"], args["new_string"]
    if old == new:
        raise ToolError("old_string and new_string are identical")
    count = content.count(old) if old else 0
    if count == 0:
        span = _fuzzy_find(content, old)
        if not span:
            raise ToolError("old_string not found (also tried whitespace-insensitive match). Re-read the file and retry.")
        if ctx.checkpoint:
            ctx.checkpoint(str(path))
        updated = content[: span[0]] + new.rstrip("\n") + ("\n" if content[: span[1]].endswith("\n") else "") + content[span[1] :]
        path.write_text(updated, encoding="utf-8")
        return {"path": str(path), "replacements": 1, "fuzzy": True}
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_string occurs {count} times; add more context or set replace_all=true")
    if ctx.checkpoint:
        ctx.checkpoint(str(path))
    updated = content.replace(old, new) if args.get("replace_all") else content.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "replacements": count if args.get("replace_all") else 1}


@tool(
    "search_files",
    "file",
    "Search file contents with a regex (mode=content) or find files by glob (mode=files).",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex for content search, or glob like '*.py' for files"},
            "path": {"type": "string", "default": "."},
            "mode": {"type": "string", "enum": ["content", "files"], "default": "content"},
            "file_glob": {"type": "string", "description": "Restrict content search to files matching this glob"},
            "max_results": {"type": "integer", "default": 100},
        },
        "required": ["pattern"],
    },
)
def search_files(args: dict, ctx: ToolContext):
    root = ctx.resolve_path(args.get("path") or ".")
    mode = args.get("mode") or "content"
    limit = int(args.get("max_results") or 100)
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    if mode == "files":
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for f in filenames:
                if fnmatch.fnmatch(f, args["pattern"]):
                    hits.append(os.path.relpath(os.path.join(dirpath, f), root))
                    if len(hits) >= limit:
                        return {"files": hits, "truncated": True}
        return {"files": hits, "truncated": False}
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "--no-heading", "--max-count", "20", "-e", args["pattern"]]
        if args.get("file_glob"):
            cmd += ["-g", args["file_glob"]]
        res = subprocess.run(cmd + [str(root)], capture_output=True, text=True, timeout=60)
        lines = res.stdout.splitlines()
        return {"matches": lines[:limit], "truncated": len(lines) > limit}
    try:
        rx = re.compile(args["pattern"])
    except re.error as exc:
        raise ToolError(f"bad regex: {exc}")
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for f in filenames:
            if args.get("file_glob") and not fnmatch.fnmatch(f, args["file_glob"]):
                continue
            fp = os.path.join(dirpath, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    for no, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{os.path.relpath(fp, root)}:{no}:{line.rstrip()[:300]}")
                            if len(hits) >= limit:
                                return {"matches": hits, "truncated": True}
            except (UnicodeDecodeError, OSError):
                continue
    return {"matches": hits, "truncated": False}
