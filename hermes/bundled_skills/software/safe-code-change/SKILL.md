---
name: safe-code-change
description: Make a code change safely - understand, checkpoint, edit, test, verify, summarise. Use for bug fixes, refactors and features in a local repository.
version: 1.0.0
---

# Safe code change

## Steps
1. **Understand** - read AGENTS.md/README, `search_files` for the relevant code, `read_file` the files you will touch.
2. **Plan** - list the edits with `todo`. Note how you will verify (tests, run command).
3. **Baseline** - run the existing tests first so you know what already fails.
4. **Edit** - prefer `patch` with enough context to be unique; Hermes checkpoints files before the first edit of each turn (`/rollback` restores).
5. **Verify** - rerun the tests / build / linter. Read failures fully; fix root causes, never delete or skip tests to go green.
6. **Review** - `terminal: git diff` and re-read it like a reviewer.
7. **Summarise** - what changed, why, how it was verified, anything left.

## Pitfalls
- Don't run destructive git commands (reset --hard, push --force) without approval.
- Don't reformat unrelated code.
