---
name: customer-support
description: Answer customer questions using the knowledge base and past conversations, and escalate when unsure. Use for support triage, FAQ answers and drafting replies to customers.
version: 1.0.0
---

# Customer support

## Steps
1. Identify the customer's actual question and urgency (outage, billing, how-to, bug).
2. Look up answers: `search_files` in the knowledge base folder the user configured (check MEMORY.md for its path), then `session_search` for similar past tickets.
3. Answer only from those sources; quote the doc section. If nothing covers it, say so and escalate.
4. Draft the reply: empathetic first line, the answer in steps, a closing next step.
5. Escalate (tell the user / send_message to the support home channel) for: refunds, legal, security, angry customers, anything not in the KB.
6. If the same question appears 3+ times, suggest adding it to the KB.

## Pitfalls
- Never invent policies, prices or timelines.
- Never share another customer's data.
