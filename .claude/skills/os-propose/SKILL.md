---
name: os-propose
description: Start spec work on a GitHub issue. Updates status to specifying, runs openspec propose, links GH issue to openspec change.
license: MIT
compatibility: Requires gh CLI, openspec CLI.
metadata:
  author: openspec-generic
  version: "1.0"
---

Start spec work on a GitHub issue.

**Input**: Optionally specify an issue number. If omitted, select from status=todo issues.

**Steps**

1. **Detect repo from git remote**
   ```bash
   git remote get-url origin | grep -oP '(?<=github\.com[/:])[^\.]+'
   ```

2. **Select GH issue**
   If issue number provided, use it. Otherwise:
   - Query open issues with `status:todo` label:
     ```bash
     gh issue list --label "status:todo" --json number,title --jq '.[]'
     ```
   - If exactly one, auto-select
   - If multiple, use **AskUserQuestion** to let user pick
   - Announce: "Using issue #{number}"

3. **Check if openspec_change_name exists in body**
   ```bash
   gh issue view {number} --json body --jq '.body'
   ```
   If `openspec_change_name:` is present in the body, extract the change name and go to step 5 directly.

4. **Update issue status to specifying**
   ```bash
   # Remove status:todo, add status:specifying
   gh issue edit {number} --remove-label status:todo --add-label status:specifying
   ```

5. **Get description from issue body**
   ```bash
   gh issue view {number} --json body --jq '.body'
   ```
   This becomes the input to openspec propose.

6. **Run openspec propose**
   Use the **Skill tool** to invoke `openspec-propose`:
   - If change name found in step 3: pass the change name
   - Otherwise: pass the description from step 5

7. **After openspec propose completes** (only for new changes)
   - Find the created change directory (openspec status shows it)
   - Get the change name from the directory
   - Update `.openspec.yaml` with `github_issue_url: https://github.com/{owner}/{repo}/issues/{number}`
   - Update GH issue body to include `openspec_change_name: {change-name}`

**Output**

```
## Spec Work Started

**Issue:** #{number}
**Status:** specifying
**Change:** {change-name}

Run /os-spawn when ready to implement.
```

**Error: No todo issues found**
```
No issues found with status:todo. Create one with /os-new first.
```

**Prerequisites**
- `gh` CLI must be authenticated
- `openspec` CLI must be available
- At least one issue with `status:todo` label exists
