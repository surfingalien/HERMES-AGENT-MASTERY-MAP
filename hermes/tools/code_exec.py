"""execute_code (#59): run a Python script that can call agent tools over local RPC.

Many tool calls collapse into one turn: the script imports ``hermes_tools``
and calls e.g. ``web_search(query=...)``; only the script's stdout returns to
the model. RPC runs over a localhost socket guarded by a random token.
"""

from __future__ import annotations

import json
import os
import secrets
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from hermes.security.redact import redact
from hermes.tools.environments import sanitized_env
from hermes.tools.registry import ToolContext, ToolError, run_tool, tool

ALLOWED = ["web_search", "web_extract", "read_file", "write_file", "patch", "search_files", "terminal", "vision_analyze", "session_search"]

_STUB = '''
import json, os, socket
_PORT = int(os.environ["HERMES_RPC_PORT"]); _TOKEN = os.environ["HERMES_RPC_TOKEN"]
def _call(name, **kwargs):
    s = socket.create_connection(("127.0.0.1", _PORT))
    s.sendall((json.dumps({"token": _TOKEN, "tool": name, "args": kwargs}) + "\\n").encode())
    buf = b""
    while not buf.endswith(b"\\n"):
        chunk = s.recv(65536)
        if not chunk: break
        buf += chunk
    s.close()
    out = json.loads(buf.decode())["result"]
    try: return json.loads(out)
    except Exception: return out
'''


def _make_stub(names: list[str]) -> str:
    return _STUB + "\n".join(f"def {n}(**kwargs):\n    return _call({n!r}, **kwargs)\n" for n in names)


@tool(
    "execute_code",
    "code_execution",
    "Run a Python script. Inside it, `from hermes_tools import web_search, web_extract, read_file, write_file, patch, "
    "search_files, terminal` and call them like functions (keyword args) to chain many tool calls in one step. "
    "Print what you need; only stdout/stderr come back.",
    {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer", "default": 300}}, "required": ["code"]},
    parallel_safe=False,
)
def execute_code(args: dict, ctx: ToolContext):
    token = secrets.token_hex(16)
    calls = {"n": 0}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                req = json.loads(self.rfile.readline().decode())
                if req.get("token") != token or req.get("tool") not in ALLOWED:
                    out = json.dumps({"error": "rpc denied"})
                elif calls["n"] >= 200:
                    out = json.dumps({"error": "tool call limit (200) reached"})
                else:
                    calls["n"] += 1
                    out, _ = run_tool(req["tool"], req.get("args") or {}, ctx)
            except Exception as exc:
                out = json.dumps({"error": str(exc)})
            self.wfile.write((json.dumps({"result": out}) + "\n").encode())

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-code-") as tmp:
            Path(tmp, "hermes_tools.py").write_text(_make_stub(ALLOWED))
            script = Path(tmp, "script.py")
            script.write_text(args["code"])
            env = sanitized_env()
            env.update({"HERMES_RPC_PORT": str(server.server_address[1]), "HERMES_RPC_TOKEN": token,
                        "PYTHONPATH": tmp + os.pathsep + env.get("PYTHONPATH", "")})
            try:
                proc = subprocess.run([sys.executable, str(script)], cwd=ctx.cwd, env=env, capture_output=True, text=True,
                                      timeout=min(int(args.get("timeout") or 300), 1800))
            except subprocess.TimeoutExpired:
                raise ToolError("script timed out")
    finally:
        server.shutdown()
        server.server_close()
    out = proc.stdout[-30000:]
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr[-8000:]
    return {"exit_code": proc.returncode, "output": redact(out), "tool_calls": calls["n"]}
