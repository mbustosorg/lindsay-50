---
name: os-propose
description: Start spec work on a GitHub issue. Updates status to specifying, runs openspec propose, links GH issue to openspec change.
license: MIT
compatibility: Requires gh CLI, openspec CLI.
metadata:
  author: openspec-generic
  version: "1.2"
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

7. **Link and commit** (after openspec propose completes)
   - Find the created change directory (`openspec status --change "<name>"`)
   - Get the change name from the directory
   - Update `.openspec.yaml` with `github_issue_url: https://github.com/{owner}/{repo}/issues/{number}`
   - Update GH issue body to include `openspec_change_name: {change-name}`
   - `git add openspec/changes/<name>/ && git commit -m "propose: <name> specs"`

8. **Human gate — confirm ao-ready**
   Use **AskUserQuestion** to confirm:
   > "Commit is ready. Mark as ao-ready for implementation?"

   If yes:
   - Capture the current `main` HEAD SHA: `LOCAL_SHA=$(git rev-parse main)`
   - `git push origin main` (pushes the spec commit to origin)
   - **Verify the push landed** — `git fetch origin main && [ "$(git rev-parse origin/main)" = "$LOCAL_SHA" ]`. If the SHA doesn't match, the push was rejected (branch protection, hook, auth, wrong upstream) — surface the error and **do NOT** flip the label to `ao-ready`. Leave the issue at `status:specifying` and tell the user to investigate.
   - Only after the SHA matches: remove `status:specifying`, add `status:ao-ready` label to the GH issue

   If no:
   - Do not push yet — issue stays `status:specifying`, user can push and flip label manually

**Output**

If human confirms AND push verified:
```
## Spec Work Ready

**Issue:** #{number}
**Status:** ao-ready
**Change:** {change-name}

Committed and pushed (origin/main @ {sha}). Run /os-spawn when ready to implement.
```

If human confirms but push failed:
```
## Spec Work Blocked — push did not land

**Issue:** #{number}
**Status:** specifying (NOT ao-ready)
**Change:** {change-name}

Local commit is at {local-sha}, but origin/main is at {remote-sha}. The push was rejected
(likely branch protection, hook, or auth). Investigate and re-run, or push manually and
flip the label with `gh issue edit {number} --remove-label status:specifying --add-label status:ao-ready`.
```

If human declines:
```
## Spec Work Committed

**Issue:** #{number}
**Status:** specifying
**Change:** {change-name}

Commit ready locally. Push and flip to ao-ready manually when ready.
```

**Error: No todo issues found**
```
No issues found with status:todo. Create one with /os-new first.
```

**Prerequisites**
- `gh` CLI must be authenticated
- `openspec` CLI must be available
- At least one issue with `status:todo` label exists
