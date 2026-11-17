"""Scheduled tasks (#51-#60): job store, ticker, isolated runs, delivery, monitoring.

Jobs live in ``HERMES_HOME/cron/jobs.json``; each run starts a fresh, isolated
agent session (source ``cron``), saves its output under ``cron/output/<job>/``
and delivers the result to the job's target (origin chat, a platform's home
channel, ``telegram:<chat_id>``, or ``local``). A reply containing ``[SILENT]``
is saved but not delivered - useful for "only tell me if something changed".
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from hermes import config
from hermes.cron.schedule import Schedule, parse_schedule

log = logging.getLogger("hermes.cron")


@dataclass
class Job:
    id: str
    name: str
    prompt: str
    schedule: dict
    deliver: str = "origin"
    origin: dict = field(default_factory=dict)  # {platform, chat_id, thread_id}
    skills: list[str] = field(default_factory=list)
    toolsets: list[str] = field(default_factory=list)
    model: str = ""
    enabled: bool = True
    paused_reason: str = ""
    repeat: int | None = None  # remaining runs; None = forever
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    next_run: str | None = None
    last_run: str | None = None
    last_status: str = ""
    last_error: str = ""
    run_count: int = 0
    failure_count: int = 0

    @property
    def sched(self) -> Schedule:
        return Schedule.from_dict(self.schedule)

    def summary(self) -> dict:
        d = asdict(self)
        d["schedule_text"] = self.sched.describe()
        d["prompt"] = self.prompt[:200]
        return d


def _dir() -> Path:
    d = config.path("cron")
    (d / "output").mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _locked(name: str = ".jobs.lock", blocking: bool = True):
    fh = open(_dir() / name, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def load_jobs() -> list[Job]:
    p = _dir() / "jobs.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text() or "[]")
    return [Job(**{k: v for k, v in j.items() if k in Job.__dataclass_fields__}) for j in data]


def save_jobs(jobs: list[Job]) -> None:
    p = _dir() / "jobs.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(j) for j in jobs], indent=2))
    tmp.replace(p)


def _update(fn: Callable[[list[Job]], object]) -> object:
    with _locked():
        jobs = load_jobs()
        result = fn(jobs)
        save_jobs(jobs)
        return result


def create_job(prompt: str, schedule: str, name: str = "", deliver: str = "origin", origin: dict | None = None,
               skills: list[str] | None = None, repeat: int | None = None, model: str = "", toolsets: list[str] | None = None) -> Job:
    sched = parse_schedule(schedule)
    now = datetime.now()
    nxt = sched.next_run(now)
    job = Job(
        id=uuid.uuid4().hex[:8], name=name or prompt[:40], prompt=prompt, schedule=sched.to_dict(), deliver=deliver,
        origin=origin or {}, skills=list(skills or []), repeat=repeat if sched.kind != "once" else 1, model=model,
        toolsets=list(toolsets or []), next_run=nxt.isoformat(timespec="seconds") if nxt else None,
    )
    _update(lambda jobs: jobs.append(job))
    return job


def find(job_id: str) -> Job | None:
    return next((j for j in load_jobs() if j.id == job_id or j.name == job_id), None)


def update_job(job_id: str, **changes) -> Job | None:
    def fn(jobs):
        for j in jobs:
            if j.id == job_id or j.name == job_id:
                if "schedule" in changes and isinstance(changes["schedule"], str):
                    sched = parse_schedule(changes.pop("schedule"))
                    j.schedule = sched.to_dict()
                    nxt = sched.next_run(datetime.now())
                    j.next_run = nxt.isoformat(timespec="seconds") if nxt else None
                for k, v in changes.items():
                    if hasattr(j, k) and v is not None:
                        setattr(j, k, v)
                return j
        return None
    return _update(fn)  # type: ignore[return-value]


def pause(job_id: str, reason: str = "paused by user") -> Job | None:
    return update_job(job_id, enabled=False, paused_reason=reason)


def resume(job_id: str) -> Job | None:
    j = find(job_id)
    if not j:
        return None
    nxt = j.sched.next_run(datetime.now())
    return update_job(job_id, enabled=True, paused_reason="", next_run=nxt.isoformat(timespec="seconds") if nxt else j.next_run)


def remove(job_id: str) -> bool:
    def fn(jobs):
        before = len(jobs)
        jobs[:] = [j for j in jobs if j.id != job_id and j.name != job_id]
        return len(jobs) < before
    return bool(_update(fn))


def due_jobs(now: datetime | None = None) -> list[Job]:
    now = now or datetime.now()
    return [j for j in load_jobs() if j.enabled and j.next_run and datetime.fromisoformat(j.next_run) <= now]


def outputs(job_id: str, limit: int = 5) -> list[Path]:
    d = _dir() / "output" / job_id
    return sorted(d.glob("*.md"), reverse=True)[:limit] if d.exists() else []


def build_prompt(job: Job, cfg: dict) -> str:
    parts = []
    if job.skills:
        from hermes.skills.manager import SkillManager

        mgr = SkillManager(cfg)
        for s in job.skills:
            sk = mgr.get(s)
            if sk:
                parts.append(f"[Skill: {s}]\n{sk.body()}")
    parts.append(
        "You are running as an unattended scheduled task. Nobody can answer questions, so do not ask any. "
        "Your final response is delivered to the user as the task's result. If there is nothing worth reporting "
        "(for example, nothing changed since last time), reply with exactly [SILENT].\n\n"
        f"Task: {job.prompt}"
    )
    return "\n\n".join(parts)


def run_job(job: Job, cfg: dict, deliver_fn: Callable[[str, str, dict], None] | None = None, agent_factory=None) -> dict:
    """Run one job in an isolated session, persist output, deliver, reschedule."""
    started = datetime.now()
    status, error, text = "ok", "", ""
    try:
        if agent_factory is None:
            from hermes.agent import AIAgent

            def agent_factory(**kw):
                return AIAgent(**kw)
        toolsets = job.toolsets or (cfg.get("platform_toolsets", {}) or {}).get("cron", ["hermes-cron"])
        agent = agent_factory(config=cfg, platform="cron", toolsets=toolsets, model=job.model or None, approval_mode="deny-dangerous")
        result = agent.run_conversation(build_prompt(job, cfg))
        text = result.get("final_response") or ""
    except Exception as exc:  # job failures must never kill the ticker
        status, error = "error", f"{type(exc).__name__}: {exc}"
        log.exception("cron job %s failed", job.id)
    out_dir = _dir() / "output" / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{started:%Y%m%d-%H%M%S}.md"
    out_file.write_text(f"# {job.name}\n\n- run: {started.isoformat(timespec='seconds')}\n- status: {status}\n\n{text or error}\n")
    silent = "[SILENT]" in text
    delivered = False
    if deliver_fn and not silent:
        msg = text if status == "ok" else f"⚠️ Scheduled task '{job.name}' failed: {error}"
        try:
            deliver_fn(job.deliver, f"⏰ {job.name}\n\n{msg}", job.origin)
            delivered = True
        except Exception as exc:
            log.warning("delivery failed for job %s: %s", job.id, exc)

    def fn(jobs):
        for j in jobs:
            if j.id != job.id:
                continue
            j.last_run = started.isoformat(timespec="seconds")
            j.last_status = status
            j.last_error = error
            j.run_count += 1
            j.failure_count = j.failure_count + 1 if status != "ok" else 0
            if j.repeat is not None:
                j.repeat -= 1
            nxt = j.sched.next_run(datetime.now())
            if (j.repeat is not None and j.repeat <= 0) or nxt is None:
                j.enabled, j.next_run, j.paused_reason = False, None, "completed"
            else:
                j.next_run = nxt.isoformat(timespec="seconds")
            if j.failure_count >= 5:
                j.enabled, j.paused_reason = False, "auto-paused after 5 consecutive failures"
    _update(fn)
    return {"job": job.id, "status": status, "error": error, "output_file": str(out_file), "silent": silent, "delivered": delivered}


def tick(cfg: dict, deliver_fn=None, agent_factory=None) -> list[dict]:
    """Run all due jobs once. A file lock prevents overlapping tickers."""
    with _locked(".tick.lock", blocking=False) as got:
        if not got:
            return []
        results = []
        for job in due_jobs():
            results.append(run_job(job, cfg, deliver_fn, agent_factory))
        return results


class CronTicker(threading.Thread):
    """Background ticker used by the gateway (#52 scheduled tasks)."""

    def __init__(self, cfg: dict, deliver_fn=None, interval: int = 60):
        super().__init__(daemon=True, name="cron-ticker")
        self.cfg, self.deliver_fn, self.interval = cfg, deliver_fn, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                tick(self.cfg, self.deliver_fn)
            except Exception:
                log.exception("cron tick failed")
            self.stop_event.wait(self.interval)

    def stop(self) -> None:
        self.stop_event.set()


def status_report() -> dict:
    jobs = load_jobs()
    return {
        "total": len(jobs),
        "enabled": sum(j.enabled for j in jobs),
        "failing": [j.id for j in jobs if j.last_status == "error"],
        "next": sorted(((j.next_run, j.id, j.name) for j in jobs if j.enabled and j.next_run))[:5],
        "ticker_hint": "jobs run while `hermes gateway run` is active, or via `hermes cron tick` from system cron",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
