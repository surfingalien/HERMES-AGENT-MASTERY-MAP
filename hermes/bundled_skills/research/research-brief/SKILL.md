---
name: research-brief
description: Produce a sourced research brief on any topic (Research -> Action -> Report). Use when the user asks to research, investigate, compare options or "find out about" something and wants a written summary with sources.
version: 1.0.0
metadata:
  hermes:
    tags: [research, web, report]
---

# Research brief

## When to use
The user wants a concise, sourced overview of a topic, market, tool, person/company (public info only) or decision.

## Steps
1. **Plan** - restate the question in one line and list 3-6 sub-questions with the `todo` tool.
2. **Delegate** (optional) - for broad topics, `delegate_task` with one task per sub-question (toolsets: web) so research runs in parallel.
3. **Search / browse** - `web_search` each sub-question; open the 2-4 most authoritative results with `web_extract`. Prefer primary sources (official docs, filings, papers) over aggregators.
4. **Draft** - write the brief using the template below. Every non-obvious claim gets a source link.
5. **Verify** - re-check numbers, dates and names against the sources; flag anything uncertain.
6. **Deliver** - reply with the brief. If the user wants a file, `write_file` it as Markdown.

## Template
```
# <Topic> - research brief (<date>)
**Bottom line:** 2-3 sentences.
## Key findings
- finding - [source](url)
## Options / comparison (if relevant)
| option | pros | cons | cost |
## Open questions & confidence
## Sources
```

## Pitfalls
- Don't stop at search snippets - open the pages.
- Note publication dates; prefer the last 12 months for fast-moving topics.
- Never present speculation as fact.
