---
name: os-spawn
description: Spawn an Agent Orchestrator worker for a change. Updates issue status to in-progress and runs ao spawn.
license: MIT
compatibility: Requires gh CLI, ao CLI.
metadata:
  author: openspec-generic
  version: "1.1"
---

Spawn an Agent Orchestrator worker for a change.

**Input**: Optionally specify an issue number. If omitted, select from `status:ao-ready` issues.

**Steps**

1. **Detect repo and project name from git remote**
   ```bash
   REPO=$(git remote get-url origin | grep -oP '(?<=github\.com[/:])[^\.]+')
   PROJECT=$(echo "$REPO" | cut -d/ -f2)  # e.g. "lindsay-50"
   OWNER=$(echo "$REPO" | cut -d/ -f1)   # e.g. "mbustosorg"
   ISSUE_NUM={issue_number or detected}
   ```

2. **Select GH issue**
   If issue number provided, use it. Otherwise:
   - Query open issues with `status:ao-ready` label:
     ```bash
     gh issue list --label "status:ao-ready" --json number,title --jq '.[]'
     ```
   - If exactly one, auto-select
   - If multiple, use **AskUserQuestion** to let user pick
   - Announce: "Using issue #{number}"

3. **Confirm before spawning**
   Use **AskUserQuestion** to confirm:
   > "Ready to spawn an AO agent to implement this? The issue will move to status:in-progress."

   If no, stop. If yes, proceed.

4. **Update issue status to in-progress**
   ```bash
   gh issue edit {number} --remove-label status:ao-ready --add-label status:in-progress
   ```

5. **Build execution prompt**
   Construct a prompt that tells the agent to:
   - Read the issue body to find the openspec_change_name
   - Read all spec files and tasks.md
   - Execute tasks in order, cross-referencing specs
   - Mark each task complete in tasks.md
   - Commit when done

6. **Run ao spawn with prompt** (must run from `~/.agent-orchestrator`)
   ```bash
   cd ~/.agent-orchestrator
   # Use "issue-{num}" format — agent derives branch name from issue number alone
   ao spawn "issue-${ISSUE_NUM}" --prompt "<full prompt from step 5>"
   ```

**Output**

```
## AO Worker Spawned

**Issue:** #{number}
**Status:** in-progress

AO agent is now working on this change.
```

**Error: No ao-ready issues found**
```
No issues found with status:ao-ready. Run /os-propose first to generate specs, then confirm check-in to mark as ao-ready.
```

**Prerequisites**
- `gh` CLI must be authenticated
- `ao` CLI must be installed and configured
- At least one issue with `status:ao-ready` label exists
