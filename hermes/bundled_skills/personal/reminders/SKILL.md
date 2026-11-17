---
name: reminders
description: Create personal reminders and recurring nudges delivered to the user's chat. Use when the user says "remind me to...", "every Friday tell me...", "in 2 hours ping me".
version: 1.0.0
---

# Reminders

## Steps
1. Extract what and when. Convert to a schedule: "in 2 hours" -> `2h`, "tomorrow at 9" -> `tomorrow at 9am`, "every Friday 5pm" -> `every friday at 5pm`.
2. Create with `cronjob(action="create", schedule=..., prompt="Remind the user: <text>. Reply with just the reminder, friendly and short.", deliver="origin", name="reminder: <text>")`.
   One-off times run once automatically.
3. Confirm back with the exact time and how to cancel (`cronjob remove <id>`).

## Pitfalls
- Ask for the timezone once if ambiguous and save it to memory (target=user).
