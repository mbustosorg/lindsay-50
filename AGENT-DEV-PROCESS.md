# Agent Development Process

How to use the OpenSpec workflow (`/os-new`, `/os-propose`, `/os-spawn`) to drive changes from idea to implementation via GitHub issues and Claude Code agents.

---

## Overview

```
/os-new          /os-propose           /os-spawn
───────          ──────────             ────────
Create GH        Spec work →           Spawn AO
issue + label    artifacts + commit     worker to
status:todo      status:specifying     implement
                 → status:ao-ready     status:in-progress
```

This workflow is optional. You can also just write code and PR directly — the agent orchestrator and OpenSpec tooling are here to help, not enforce.

**Note:** Specs (`openspec/changes/`) are always committed to `main`. The AO worktree is created from current `main`, giving the agent the latest specs at spawn time. Future work may explore feature-branch-first workflows with PR review for spec approval, but that requires AO worktree plugin changes.

---

## Phase 1: `/os-new` — Create a GitHub Issue

**Creates:** GH issue tagged `status:todo`

Creates a GitHub issue and marks it as ready for spec work.

```bash
/os-new Add SQLite persistence and admin UI
```

**What happens:**
1. Detects repo from `git remote get-url origin`
2. Creates GH issue with title `"[spec] Add SQLite persistence and admin UI"` and label `spec-driven`
3. Adds label `status:todo`

**GitHub state:**
- Labels: `status:todo`, `spec-driven`
- Body: empty (filled by os-propose)

**Branch/git:** None yet.

**Output:**
```
## GitHub Issue Created

**Issue:** https://github.com/mbustosorg/lindsay-50/issues/5
**Status:** todo

Ready to flesh out with /os-propose
```

---

## Phase 2: `/os-propose` — Spec Work

**Creates:** `openspec/changes/<name>/` with proposal, design, specs, and tasks

Feeds a `status:todo` issue into the OpenSpec pipeline, generating all artifacts needed for implementation.

```bash
/os-propose 5
```

**What happens:**

```
1. Select issue #5 (status:todo)
2. Update GH issue: remove status:todo, add status:specifying
3. Get issue description as input
4. Skill openspec-propose: creates all artifacts
5. Link GH issue ↔ openspec change (bidirectional)
6. git add + commit openspec/changes/<name>/
7. Human gate: confirm check-in + push
```

**Artifacts created in `openspec/changes/<name>/`:**

| File | Purpose |
|------|---------|
| `.openspec.yaml` | Change metadata + linked GH issue URL |
| `proposal.md` | What & why (capabilities, impact) |
| `design.md` | How — decisions, goals/non-goals, risks, trade-offs |
| `specs/<cap>/spec.md` | Per-capability requirements (Gherkin scenarios) |
| `tasks.md` | Implementation checklist (`- [ ]` / `- [x]`) |

**GitHub state:**
- Label: `status:specifying` (during spec work)
- After human confirms push: label becomes `status:ao-ready`

**Branch/git:**
- Specs are committed to `main` (never to a feature branch)
- No new branch at this stage — commits to your current branch
- Commit message: `propose: <name> specs`
- GH issue body gets `openspec_change_name: <change-name>`

**Output (after human confirms):**
```
## Spec Work Ready

**Issue:** #5
**Status:** ao-ready
**Change:** add-sqlite-admin

Committed and pushed. Run /os-spawn when ready to implement.
```

---

## Phase 3: `/os-spawn` — Spawn Implementation Agent

**Creates:** Git worktree + AO worker tmux session

Spawns an Agent Orchestrator worker session to implement the change.

```bash
/os-spawn 5
```

**What happens:**

```
1. Select issue #5 (status:ao-ready)
2. Human gate: confirm spawn
3. Update GH issue: remove status:ao-ready, add status:in-progress
4. Construct execution prompt (reads issue → finds openspec_change_name
   → reads tasks.md + specs → implements)
5. cd ~/.agent-orchestrator && ao spawn "owner/project/number" --prompt "<prompt>"
```

**AO worker does:**
1. Reads GH issue body → finds `openspec_change_name`
2. Reads `openspec/changes/<name>/tasks.md`
3. Reads all spec files and design.md
4. Implements tasks in order, marking each `- [ ]` → `- [x]`
5. Commits when done

**GitHub state:**
- Label: `status:in-progress`

**Branch/git:**
- `ao spawn` creates a **git worktree** from `main` with a feature branch (e.g., `feat/add-sqlite-admin`)
- A **tmux session** is started (e.g., `linds-1`)
- The worker session owns the branch/PR
- **Specs always live on `main`**. The `openspec/changes/` directory is committed to `main` after human approval. The worktree is created from current `main`, so the agent always has the latest specs at spawn time.

**Output:**
```
## AO Worker Spawned

**Issue:** #5
**Status:** in-progress

AO agent is now working on this change.
```

---

## GitHub Issue Label Lifecycle

```
status:todo → status:specifying → status:ao-ready → status:in-progress → (merged/closed)
```

| Label | Meaning |
|-------|---------|
| `status:todo` | New issue, not started |
| `status:specifying` | In spec/design/artifacts phase |
| `status:ao-ready` | Specs committed, ready for implementation |
| `status:in-progress` | AO worker is implementing |
| `spec-driven` | Marks this as an OpenSpec-managed issue (permanent) |

---

## Quick Reference

| Command | GH Labels Changed | Git Changes | Worktree Created? |
|---------|------------------|-------------|-------------------|
| `/os-new` | adds `status:todo`, `spec-driven` | None | No |
| `/os-propose` | `todo` → `specifying` → `ao-ready` | New commit with `openspec/changes/<name>/` | No |
| `/os-spawn` | `ao-ready` → `in-progress` | ao spawn creates feature branch + worktree | **Yes** |

---

## Prerequisites

- `gh` CLI authenticated: `gh auth login`
- `openspec` CLI (for `/os-propose` internals)
- `ao` CLI (for `/os-spawn`)

---

## Example Full Flow

```bash
# 1. File a new idea as a GH issue
/os-new Add SQLite persistence and admin UI

# → GH issue #5 created, label: status:todo

# 2. Spec it out (artifacts generated, committed)
/os-propose 5

# → status:specifying → artifacts created → status:ao-ready
# → Commit: "propose: add-sqlite-admin specs"

# 3. When ready to build, spawn an agent
/os-spawn 5

# → status:in-progress
# → ao spawn creates worktree + tmux session
# → AO worker implements tasks.md, marks each [x] when done
```

---

## Skipping the Workflow

You don't have to use this. If you want to just implement something directly:

- Write the code in a branch
- Open a PR
- The AO orchestrator can still track and monitor it

The OpenSpec workflow is here to add structure and artifact traceability when you want it — not a requirement.
