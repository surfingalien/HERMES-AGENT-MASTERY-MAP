---
name: scheduled-report
description: Set up a recurring report (daily digest, weekly metrics, news summary) delivered to a chat via cron. Use when the user says "every morning send me...", "weekly report on...", or wants monitoring that only alerts on change.
version: 1.0.0
---

# Scheduled report

## Steps
1. Clarify: what to report, sources, schedule, delivery target (this chat = origin, or `telegram`, `slack:<channel>`, `email:<addr>`).
2. Write a **self-contained** job prompt - the job runs in a fresh session with no memory of this chat. Include URLs, file paths, format and length.
3. For "only tell me if something changed": instruct the job to compare with the previous output file in `~/.hermes/cron/output/<job_id>/` (read_file) and reply `[SILENT]` if nothing changed.
4. Create it: `cronjob(action="create", schedule="every weekday at 8am", prompt=..., deliver="origin", skills=[...])`.
5. Offer to run it once now: `cronjob(action="run", job_id=...)` and show the result.
6. Tell the user how to pause/resume/remove it.

## Report template
```
📊 <Report name> - <date>
• headline metric / change
• 3-5 bullets
Next check: <time>
```

## Pitfalls
- Jobs can't ask questions - put every assumption in the prompt.
- Keep messages short for mobile.
