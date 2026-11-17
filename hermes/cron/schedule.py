"""Schedule parsing (#51 cronjob, #53 natural-language schedules).

Accepted forms::

    "30m" / "2h" / "1d"                 one-shot, that long from now
    "in 20 minutes"                      one-shot
    "2026-02-03T14:00"                   one-shot at a timestamp
    "every 30m" / "every 2 hours"        recurring interval
    "0 9 * * 1-5"                        standard 5-field cron
    "daily at 9am", "every monday at 8:30", "weekdays at 6pm", "hourly",
    "every day at noon", "twice a day", "every morning", "weekly on friday at 17:00"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass
class Schedule:
    kind: str  # once | interval | cron
    expr: str
    run_at: datetime | None = None
    seconds: int = 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "expr": self.expr, "run_at": self.run_at.isoformat() if self.run_at else None, "seconds": self.seconds}

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        return cls(d["kind"], d["expr"], datetime.fromisoformat(d["run_at"]) if d.get("run_at") else None, int(d.get("seconds") or 0))

    def describe(self) -> str:
        if self.kind == "once":
            return f"once at {self.run_at:%Y-%m-%d %H:%M}"
        if self.kind == "interval":
            return f"every {_fmt_secs(self.seconds)}"
        return f"cron '{self.expr}'"

    def next_run(self, after: datetime) -> datetime | None:
        if self.kind == "once":
            return self.run_at if self.run_at and self.run_at > after else None
        if self.kind == "interval":
            return after + timedelta(seconds=self.seconds)
        return cron_next(self.expr, after)


def _fmt_secs(s: int) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s % n == 0:
            return f"{s // n}{unit}"
    return f"{s}s"


def _duration(text: str) -> int | None:
    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)", text.strip())
    if not m:
        return None
    return int(m.group(1)) * UNIT_SECONDS[m.group(2)[0]]


def _time_of_day(text: str) -> tuple[int, int] | None:
    text = text.strip().lower()
    if text in ("noon", "midday"):
        return 12, 0
    if text == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_schedule(text: str, now: datetime | None = None) -> Schedule:
    now = now or datetime.now()
    raw = text.strip()
    t = raw.lower().strip()
    # 5-field cron
    if re.fullmatch(r"[\d*/,\-a-z]+(\s+[\d*/,\-a-z]+){4}", t) and len(t.split()) == 5 and any(c in t for c in "*0123456789"):
        cron_next(t, now)  # validate
        return Schedule("cron", t)
    # ISO timestamp
    try:
        dt = datetime.fromisoformat(raw)
        return Schedule("once", raw, run_at=dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone().replace(tzinfo=None))
    except ValueError:
        pass
    # one-shot durations
    secs = _duration(t)
    if secs:
        return Schedule("once", raw, run_at=now + timedelta(seconds=secs))
    m = re.fullmatch(r"in\s+(.+)", t)
    if m and _duration(m.group(1).replace("an ", "1 ").replace("a ", "1 ")):
        return Schedule("once", raw, run_at=now + timedelta(seconds=_duration(m.group(1).replace("an ", "1 ").replace("a ", "1 "))))
    m = re.fullmatch(r"(?:tomorrow|today)\s+at\s+(.+)", t)
    if m and _time_of_day(m.group(1)):
        h, mi = _time_of_day(m.group(1))
        day = now + timedelta(days=1 if t.startswith("tomorrow") else 0)
        return Schedule("once", raw, run_at=day.replace(hour=h, minute=mi, second=0, microsecond=0))
    # intervals
    m = re.fullmatch(r"every\s+(.+)", t)
    if m and _duration(m.group(1)):
        return Schedule("interval", raw, seconds=max(60, _duration(m.group(1))))
    simple = {"hourly": "0 * * * *", "every hour": "0 * * * *", "daily": "0 9 * * *", "every day": "0 9 * * *",
              "weekly": "0 9 * * 1", "every week": "0 9 * * 1", "monthly": "0 9 1 * *", "every month": "0 9 1 * *",
              "every morning": "0 8 * * *", "every evening": "0 18 * * *", "every night": "0 22 * * *",
              "twice a day": "0 9,21 * * *", "weekdays": "0 9 * * 1-5", "every weekday": "0 9 * * 1-5",
              "weekends": "0 10 * * 0,6", "every weekend": "0 10 * * 0,6"}
    if t in simple:
        return Schedule("cron", simple[t])
    m = re.fullmatch(r"(?:every\s+)?(day|daily|weekday|weekdays|weekend|weekends|morning|evening|"
                     r"(?:(?:weekly\s+on\s+)|(?:every\s+))?(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:\s*(?:,|and)\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)*)"
                     r"\s+at\s+(.+)", t)
    if not m:
        m = re.fullmatch(r"(?:every\s+)?(day|daily|weekdays?|weekends?|weekly\s+on\s+\w+|\w+day(?:\s*(?:,|and)\s*\w+day)*)\s+at\s+(.+)", t)
    if m:
        tod = _time_of_day(m.group(2))
        if tod:
            h, mi = tod
            what = m.group(1).replace("weekly on ", "").replace("every ", "").strip()
            if what in ("day", "daily", "morning", "evening"):
                dow = "*"
            elif what.startswith("weekday"):
                dow = "1-5"
            elif what.startswith("weekend"):
                dow = "0,6"
            else:
                days = re.findall(r"(mon|tue|wed|thu|fri|sat|sun)", what)
                if not days:
                    raise ValueError(f"cannot parse schedule: {text!r}")
                dow = ",".join(str(DOW[d]) for d in days)
            return Schedule("cron", f"{mi} {h} * * {dow}")
    m = re.fullmatch(r"at\s+(.+)", t)
    if m and _time_of_day(m.group(1)):
        h, mi = _time_of_day(m.group(1))
        run = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return Schedule("once", raw, run_at=run if run > now else run + timedelta(days=1))
    raise ValueError(f"cannot parse schedule: {text!r}. Try '30m', 'every 2h', 'daily at 9am' or a cron expression like '0 9 * * *'.")


# ---------------------------------------------------------------- cron engine

def _parse_field(field: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if names:
            for k, v in names.items():
                part = re.sub(rf"\b{k}[a-z]*\b", str(v), part)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(part)
            if step > 1:
                b = hi
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron value out of range: {part} ({lo}-{hi})")
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    f = expr.split()
    if len(f) != 5:
        raise ValueError("cron expression needs 5 fields: minute hour day month weekday")
    minutes = _parse_field(f[0], 0, 59)
    hours = _parse_field(f[1], 0, 23)
    dom = _parse_field(f[2], 1, 31)
    months = _parse_field(f[3], 1, 12, MONTHS)
    dow = {d % 7 for d in _parse_field(f[4], 0, 7, DOW)}
    return minutes, hours, dom, months, dow, f[2] == "*", f[4] == "*"


def cron_next(expr: str, after: datetime) -> datetime:
    minutes, hours, dom, months, dow, dom_star, dow_star = parse_cron(expr)
    t = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = after + timedelta(days=366 * 4)
    while t < limit:
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        py_dow = (t.weekday() + 1) % 7  # cron: 0=Sunday
        day_ok = (t.day in dom) and (py_dow in dow) if (dom_star or dow_star) else (t.day in dom) or (py_dow in dow)
        if not day_ok:
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hours:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in minutes:
            t += timedelta(minutes=1)
            continue
        return t
    raise ValueError(f"cron expression never fires: {expr}")
