---
name: skill-authoring
description: How to write and maintain good Hermes skills (focused, reusable procedures). Use before creating or patching a skill with skill_manage, or when the user asks to "turn this into a skill".
version: 1.0.0
---

# Writing good skills

A skill is procedural memory: the steps that worked, so next time is faster.

## When to create one
- A task took many tool calls or several attempts
- The user says "do this again every time" / "remember how to do this"
- You discovered non-obvious pitfalls

## Structure
```
---
name: lowercase-with-dashes
description: WHAT it does + WHEN to use it (this line is all the agent sees until it loads the skill)
version: 1.0.0
---
# Title
## When to use
## Steps        (numbered, imperative, tool names explicit)
## Pitfalls     (what went wrong last time)
## Verification (how to know it worked)
```

## Rules (focused skills)
- One job per skill. If it has "and" in the name, split it.
- Keep SKILL.md under ~400 lines; move long references to `references/`, templates to `templates/`, scripts to `scripts/` (skill_manage write_file).
- No secrets, no user-specific data - that belongs in memory.
- When a skill turns out wrong, patch it immediately (skill_manage patch) - that is how skills improve from experience.
