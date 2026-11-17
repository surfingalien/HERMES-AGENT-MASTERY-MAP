---
name: file-operations
description: Organise, rename, convert or clean up files and folders safely (dry run first). Use for "organise my downloads", bulk renames, deduplication or format conversion.
version: 1.0.0
---

# File operations

## Steps
1. Inspect: `search_files` (mode=files) / `terminal: ls -la` to see what exists. Report counts and sizes.
2. Propose a plan as a table (from -> to) and **dry-run** it: print the commands without executing.
3. Get explicit approval for anything that deletes or overwrites.
4. Execute in small batches; prefer `mv` into a dated `_archive/` folder over `rm`.
5. Verify the result (counts match, nothing lost) and summarise.

## Pitfalls
- Never touch ~/.ssh, ~/.aws, ~/.hermes/.env.
- Watch for spaces/unicode in names - quote paths.
