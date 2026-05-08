---
name: os-spawn
description: Spawn an Agent Orchestrator worker for a change. Updates issue status to in-progress and runs ao spawn.
license: MIT
compatibility: Requires gh CLI, ao CLI.
metadata:
  author: openspec-generic
  version: "1.0"
---

Spawn an Agent Orchestrator worker for a change.

**Input**: Optionally specify an issue number. If omitted, select from status:specifying issues.

**Steps**

1. **Detect repo from git remote**
   ```bash
   git remote get-url origin | grep -oP '(?<=github\.com[/:])[^\.]+'
   ```

2. **Select GH issue**
   If issue number provided, use it. Otherwise:
   - Query open issues with `status:specifying` label:
     ```bash
     gh issue list --label "status:specifying" --json number,title --jq '.[]'
     ```
   - If exactly one, auto-select
   - If multiple, use **AskUserQuestion** to let user pick
   - Announce: "Using issue #{number}"

3. **Update issue status to in-progress**
   ```bash
   # Remove status:specifying, add status:in-progress
   gh issue edit {number} --remove-label status:specifying --add-label status:in-progress
   ```

4. **Build execution prompt**
   Construct the prompt that tells the agent to read the issue body, find the change name, then read and execute the OpenSpec artifacts.

5. **Run ao spawn with prompt**
   ```bash
   ao spawn {number} --prompt "Read the issue body to find the openspec_change_name. Then read openspec/changes/{openspec_change_name}/design.md, openspec/changes/{openspec_change_name}/specs/*/spec.md, and openspec/changes/{openspec_change_name}/tasks.md to understand the full scope. Execute tasks in order, cross-referencing specs to ensure compliance. Mark each task complete in tasks.md when done."
   ```

**Output**

```
## AO Worker Spawned

**Issue:** #{number}
**Status:** in-progress

AO agent is now working on this change.
```

**Error: No specifying issues found**
```
No issues found with status:specifying. Run /os-propose first to start spec work.
```

**Prerequisites**
- `gh` CLI must be authenticated
- `ao` CLI must be installed and configured
- At least one issue with `status:specifying` label exists
