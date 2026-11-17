---
name: web-research
description: Deep browser-based research for pages that need navigation, forms, pagination or JavaScript (#70 web research). Use when web_extract is not enough - logged-out portals, search forms, multi-page listings, or when you must read charts/images on a page.
version: 1.0.0
---

# Web research with the browser

## When to use
- Content behind a search form or filters
- Paginated listings / multi-step navigation
- Pages where layout or images matter (use `browser_vision`)

## Steps
1. Start with `web_search` to find the right entry URL.
2. `browser_navigate` to it. Read the snapshot: element refs look like `@e12`.
3. Fill forms with `browser_type` (submit=true to send) and follow links with `browser_click`.
4. After every action read the new snapshot before acting again - refs change.
5. For charts, CAPTCHAs-free visual layouts or images use `browser_vision` with a specific question.
6. Record each fact with its URL as you go; `browser_close` when finished.
7. Summarise with sources (see the research-brief skill for the format).

## Pitfalls
- Never enter credentials or payment details unless the user explicitly provided them for this purpose.
- Respect robots/terms of service; don't hammer sites (one page at a time).
- The lite engine has no JavaScript - if a page is empty, say so or ask the user to install Playwright.

See `references/checklist.md` for a source-quality checklist.
