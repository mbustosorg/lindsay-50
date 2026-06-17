---
name: os-new
description: Create a new GitHub issue for an OpenSpec change. Creates issue with status=todo, ready for os-propose to flesh out.
license: MIT
compatibility: Requires gh CLI.
metadata:
  author: openspec-generic
  version: "1.0"
---

Create a new GitHub issue for an OpenSpec change.

**Input**: The issue title. User provides it directly.

**Steps**

1. **Collect title**
   If no title provided, ask user for the issue title.

2. **Detect repo from git remote**
   ```bash
   git remote get-url origin | grep -oP '(?<=github\.com[/:])[^\.]+'
   ```
   Format: `owner/repo`

3. **Create GH issue**
   ```bash
   gh api repos/{owner}/{repo}/issues --input - -X POST
   ```
   With JSON body:
   ```json
   {
     "title": "<user-title>",
     "body": "",
     "labels": ["spec-driven"]
   }
   ```

4. **Add status:todo label**
   ```bash
   gh api repos/{owner}/{repo}/issues/{number}/labels --field labels='["status:todo"]' -X POST
   ```

5. **Report result**

**Output**

```
## GitHub Issue Created

**Issue:** https://github.com/{owner}/{repo}/issues/{number}
**Status:** todo

Ready to flesh out with /os-propose
```

**Prerequisites**
- `gh` CLI must be authenticated: `gh auth login`
- Must be run from git repository root
