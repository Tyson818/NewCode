---
name: commit
description: Review staged changes and prepare a safe commit proposal.
tools:
- read_file
- find_files
- search_code
- run_command
mode: shared
---

Review the current staged changes. Explain the proposed commit scope and message. Never create a commit unless the user explicitly asks through the normal tool flow.
