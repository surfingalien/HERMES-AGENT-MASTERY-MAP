---
name: multi-agent-project
description: Run a larger project with several agents - decompose into kanban cards for specialist profiles or parallel subagents, then integrate results. Use for multi-part research, content pipelines or projects with independent workstreams.
version: 1.0.0
---

# Multi-agent project

## Choose the mechanism
- **delegate_task** (in-session, minutes): up to 5 parallel isolated subagents; results come straight back.
- **kanban** (long-running, hours/days): cards on a shared board; workers (`hermes -p <profile> kanban work`) pick them up; survives restarts.

## Steps
1. Decompose the goal into 3-8 independent cards with crisp deliverables.
2. Mark dependencies (`depends_on`) - workers only start a card when its dependencies are done and receive their results.
3. Assign specialists where useful (`assignee: researcher`, `writer`, `coder` - profiles created with `hermes profile create`).
4. Monitor with `kanban(action="list")`; unblock or re-scope blocked cards.
5. Integrate: read all results, resolve conflicts, produce the final deliverable, and verify it.

## Pitfalls
- Cards must be self-contained - workers do not see this conversation.
- Avoid two cards editing the same files concurrently.
