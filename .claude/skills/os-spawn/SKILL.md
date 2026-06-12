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

5. **Verify spec is reachable from local main (hard gate)**
   Extract `openspec_change_name:` from the issue body, then verify the spec directory exists on the local `main` branch. If not, refuse to spawn — the worker would land in a worktree that can't see the spec.

   ```bash
   CHANGE_NAME=$(gh issue view {number} --json body --jq -r '.body' | grep -oP '(?<=openspec_change_name: )\S+' | tr -d '[:space:]' || true)

   if [ -n "$CHANGE_NAME" ]; then
     if ! git ls-tree -r main -- openspec/changes/"$CHANGE_NAME"/ 2>/dev/null | grep -q .; then
       echo "ERROR: openspec/changes/$CHANGE_NAME/ is not on local main (HEAD: $(git rev-parse --short main))."
       echo "AO creates the worker worktree from local main; if the spec isn't there, the worker can't read it."
       echo "Run \`/os-propose\` (or commit + push the spec) and try again."
       echo "To override and spawn anyway, re-run with --no-spec-check."
       exit 1
     fi
     echo "Verified: openspec/changes/$CHANGE_NAME/ exists on local main."
   else
     echo "WARN: no openspec_change_name in issue body — skipping spec-reachability check."
   fi
   ```

5b. **Warn if local main is ahead of origin/main**
   If the spec was committed locally but never pushed, AO's worktree will be coherent but the eventual PR will look broken. Surface it and offer to push.

   ```bash
   if [ -n "$(git rev-list --left-right --count origin/main...main 2>/dev/null | awk '{print $1}')" ]; then
     AHEAD=$(git rev-list --left-right --count origin/main...main | awk '{print $1}')
     echo "WARN: local main is $AHEAD commit(s) ahead of origin/main."
     # Use AskUserQuestion:
     #   - "Push local main to origin/main now" (recommended) — runs `git push origin main`
     #   - "Continue without pushing" — proceed, you'll handle it
   fi
   ```

6. **Build execution prompt**
   Construct a prompt that tells the agent to:
   - Read the issue body to find the openspec_change_name
   - Read all spec files and tasks.md
   - Execute tasks in order, cross-referencing specs
   - Mark each task complete in tasks.md
   - Commit when done

7. **Run ao spawn with prompt** (must run from `~/.agent-orchestrator`)
   ```bash
   cd ~/.agent-orchestrator
   # Use "issue-{num}" format — agent derives branch name from issue number alone
   ao spawn "issue-${ISSUE_NUM}" --prompt "<full prompt from step 6>"
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

**Error: Spec not on local main**
```
openspec/changes/<name>/ is not on local main.
AO creates the worker worktree from local main; if the spec isn't there, the worker can't read it.
Run /os-propose (or commit + push the spec) and try again.
To override and spawn anyway, re-run with --no-spec-check.
```

**Prerequisites**
- `gh` CLI must be authenticated
- `ao` CLI must be installed and configured
- At least one issue with `status:ao-ready` label exists
- For spec-driven issues: the referenced openspec change directory must exist on local `main` (use `--no-spec-check` to override)
- Local `main` is preferred to be in sync with `origin/main` (warned if ahead; not blocked)
