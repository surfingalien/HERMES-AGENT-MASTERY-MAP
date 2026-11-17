"""Terminal execution backends (#13, #72 Docker, #73 SSH, #75 Daytona, #76 Modal,
#79 terminal backends, #80 security & isolation, #77 environment variables).

Every backend implements ``execute(command, timeout) -> (exit_code, output)``
and keeps a persistent working directory between calls.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import uuid
from typing import Any

from hermes.config import is_secret_name

_MARK = "__HERMES_CWD__"


def sanitized_env(passthrough: list[str] | None = None) -> dict[str, str]:
    """Child env without secrets unless explicitly passed through."""
    allow = set(passthrough or [])
    env = {k: v for k, v in os.environ.items() if k in allow or not is_secret_name(k)}
    env.setdefault("TERM", "dumb")
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    return env


def _split_cwd(output: str, fallback: str) -> tuple[str, str]:
    idx = output.rfind(_MARK)
    if idx == -1:
        return output, fallback
    cwd = output[idx + len(_MARK) :].strip().splitlines()[0] if output[idx + len(_MARK) :].strip() else fallback
    return output[:idx].rstrip("\n"), cwd or fallback


class BaseEnv:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cwd = cfg.get("cwd") or "."
        self.lock = threading.Lock()

    def execute(self, command: str, timeout: int = 180) -> tuple[int, str]:
        raise NotImplementedError

    def wrap(self, command: str) -> str:
        return f"cd {shlex.quote(self.cwd)} 2>/dev/null || true\n{command}\n__ec=$?\necho\necho {_MARK}$(pwd)\nexit $__ec"

    def cleanup(self) -> None:
        pass


class LocalEnv(BaseEnv):
    name = "local"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.cwd = os.path.abspath(os.path.expanduser(cfg.get("cwd") or "."))

    def execute(self, command, timeout=180):
        with self.lock:
            try:
                proc = subprocess.run(
                    ["bash", "-c", self.wrap(command)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=sanitized_env(self.cfg.get("env_passthrough")),
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
                return 124, out + f"\n[timed out after {timeout}s - use the process tool for long-running commands]"
            out, self.cwd = _split_cwd(proc.stdout + (("\n" + proc.stderr) if proc.stderr else ""), self.cwd)
            return proc.returncode, out


class DockerEnv(BaseEnv):
    """One long-lived hardened container per agent; commands run via docker exec."""

    name = "docker"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.container = f"hermes-{uuid.uuid4().hex[:10]}"
        self.cwd = "/workspace"
        self.started = False
        self.host_dir = os.path.abspath(os.path.expanduser(cfg.get("cwd") or "."))

    def _start(self) -> None:
        if self.started:
            return
        if not shutil.which("docker"):
            raise RuntimeError("docker not found on PATH")
        args = [
            "docker", "run", "-d", "--rm", "--name", self.container,
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "--security-opt", "no-new-privileges", "--pids-limit", "512",
            "--memory", str(self.cfg.get("docker_memory", "2g")), "--cpus", str(self.cfg.get("docker_cpus", "2")),
            "--network", str(self.cfg.get("docker_network", "bridge")),
            "-v", f"{self.host_dir}:/workspace", "-w", "/workspace",
        ]
        for name in self.cfg.get("env_passthrough") or []:
            if os.environ.get(name):
                args += ["-e", name]
        args += list(self.cfg.get("docker_extra_args") or [])
        args += [self.cfg.get("docker_image", "python:3.12-slim"), "sleep", "infinity"]
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=300)
        self.started = True

    def execute(self, command, timeout=180):
        with self.lock:
            self._start()
            try:
                proc = subprocess.run(["docker", "exec", self.container, "bash", "-c", self.wrap(command)],
                                      capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return 124, f"[timed out after {timeout}s]"
            out, self.cwd = _split_cwd(proc.stdout + proc.stderr, self.cwd)
            return proc.returncode, out

    def cleanup(self):
        if self.started:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
            self.started = False


class SSHEnv(BaseEnv):
    name = "ssh"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        if not cfg.get("ssh_host"):
            raise RuntimeError("terminal.ssh_host is not configured")
        self.cwd = cfg.get("remote_cwd") or "~"
        target = cfg["ssh_host"] if not cfg.get("ssh_user") else f"{cfg['ssh_user']}@{cfg['ssh_host']}"
        self.base = ["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=auto", "-o", "ControlPersist=600",
                     "-o", "ControlPath=/tmp/hermes-ssh-%r@%h:%p", "-p", str(cfg.get("ssh_port", 22))]
        if cfg.get("ssh_key"):
            self.base += ["-i", os.path.expanduser(cfg["ssh_key"])]
        self.base.append(target)

    def wrap(self, command: str) -> str:
        cwd = self.cwd if self.cwd.startswith("~") else shlex.quote(self.cwd)
        return f"cd {cwd} 2>/dev/null || true\n{command}\n__ec=$?\necho\necho {_MARK}$(pwd)\nexit $__ec"

    def execute(self, command, timeout=180):
        with self.lock:
            try:
                proc = subprocess.run(self.base + ["bash -s"], input=self.wrap(command), capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return 124, f"[timed out after {timeout}s]"
            out, self.cwd = _split_cwd(proc.stdout + proc.stderr, self.cwd)
            return proc.returncode, out


class DaytonaEnv(BaseEnv):
    """Daytona cloud sandbox (pip install daytona; DAYTONA_API_KEY)."""

    name = "daytona"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        try:
            from daytona import Daytona  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Daytona backend needs `pip install daytona` and DAYTONA_API_KEY") from exc
        self.client = Daytona()
        self.sandbox: Any = None
        self.cwd = "~"

    def execute(self, command, timeout=180):
        with self.lock:
            if self.sandbox is None:
                self.sandbox = self.client.create()
            resp = self.sandbox.process.exec(self.wrap(command), timeout=timeout)
            out, self.cwd = _split_cwd(getattr(resp, "result", "") or "", self.cwd)
            return int(getattr(resp, "exit_code", 0) or 0), out

    def cleanup(self):
        if self.sandbox is not None:
            try:
                self.client.delete(self.sandbox)
            finally:
                self.sandbox = None


class ModalEnv(BaseEnv):
    """Modal serverless sandbox (pip install modal; `modal token new`)."""

    name = "modal"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        try:
            import modal  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Modal backend needs `pip install modal` and `modal token new`") from exc
        self.modal = modal
        self.sb: Any = None
        self.cwd = "/root"

    def execute(self, command, timeout=180):
        with self.lock:
            if self.sb is None:
                app = self.modal.App.lookup("hermes-agent", create_if_missing=True)
                image = self.modal.Image.from_registry(self.cfg.get("modal_image", "python:3.12-slim"))
                self.sb = self.modal.Sandbox.create(app=app, image=image, timeout=3600)
            proc = self.sb.exec("bash", "-c", self.wrap(command), timeout=timeout)
            proc.wait()
            out, self.cwd = _split_cwd(proc.stdout.read() + proc.stderr.read(), self.cwd)
            return proc.returncode, out

    def cleanup(self):
        if self.sb is not None:
            self.sb.terminate()
            self.sb = None


BACKENDS = {"local": LocalEnv, "docker": DockerEnv, "ssh": SSHEnv, "daytona": DaytonaEnv, "modal": ModalEnv}


def create_env(cfg: dict) -> BaseEnv:
    backend = cfg.get("backend", "local")
    if backend not in BACKENDS:
        raise RuntimeError(f"unknown terminal backend '{backend}' (choose from {', '.join(BACKENDS)})")
    return BACKENDS[backend](cfg)
