---
name: lead-qualification
description: Research and score inbound leads or prospect companies (BANT/ICP fit) and draft a follow-up. Use for "qualify this lead", "research these prospects" or sales follow-up drafts.
version: 1.0.0
---

# Lead qualification

## Inputs
Company name/domain and, if available, contact role and the inbound message.

## Steps
1. Read the user's ideal customer profile from memory (USER.md). If missing, ask once and save it.
2. Research the company (public sources only): size, industry, funding/stage, tech stack signals, recent news.
3. Score 1-5 on: ICP fit, need/pain evidence, timing triggers, authority of the contact, budget signals.
4. Output: score table, total, 3 bullet rationale, recommended next step (book call / nurture / disqualify).
5. Draft a personalised 80-120 word follow-up referencing one concrete fact you found.
6. For many leads, use `delegate_task` with one task per company, or `hermes batch`.

## Pitfalls
- Do not collect personal data beyond professional public info.
- Mark guesses as guesses.
