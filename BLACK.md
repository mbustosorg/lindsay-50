# Black Formatter

This project uses [Black](https://black.readthedocs.io) as the canonical Python formatter. The configuration lives in [pyproject.toml](pyproject.toml) (the `[tool.black]` section) and is the single source of truth — the CLI, the VSCode extension, and the agent-orchestrator's pre-push `black .` all read from there.

## Install (one time)

```bash
source .venv/bin/activate
pip install black==26.5.1
```

The pinned version (`black==26.5.1`) matches `required-version` in `pyproject.toml` so behavior can't drift silently on a Black upgrade.

## VSCode

1. Install the **ms-python.black-formatter** extension (Microsoft, free on the marketplace).
2. Add the following to your workspace `settings.json` (Ctrl+Shift+P → "Preferences: Open Workspace Settings (JSON)"):
   ```json
   {
       "[python]": {
           "editor.defaultFormatter": "ms-python.black-formatter"
       },
       "editor.formatOnSave": true
   }
   ```

   Note: this project's `.gitignore` excludes `.vscode/`, so this snippet is per-developer, not committed. To share editor config with the team, un-ignore `.vscode/settings.json` in `.gitignore` and commit the file.

Verify: open any `.py` file, type something unformatted, save. Black should reformat it. If it doesn't, check **View → Output → Black Formatter** for errors.

## PyCharm

PyCharm 2023.2 and newer has built-in Black support — no plugin needed.

1. Make sure Black is installed in the project's venv (`pip install black==26.5.1`).
2. **Settings → Tools → Actions on Save** → enable **"Run Black formatter"**.
3. Optional: **Settings → Tools → Black** to confirm the binary path resolves to the venv's `black`. Leave **Arguments** blank so it reads `pyproject.toml` automatically.

For older PyCharm (< 2023.2), install the [Black plugin](https://plugins.jetbrains.com/plugin/22321-black) from the JetBrains marketplace, or set up a [File Watcher](https://www.jetbrains.com/help/pycharm/file-watchers.html):

- File type: `Python`
- Program: `$PyInterpreterDirectory$/black`
- Arguments: `$FilePath$`
- Working directory: `$ProjectFileDir$`
- Trigger: After save

## Config (`pyproject.toml`)

| Key | Value | Why |
|---|---|---|
| `line-length` | `120` | More permissive than the default 88; matches the longest existing signatures in the codebase. |
| `target-version` | `["py310", "py311", "py312"]` | Matches `.python-version`. |
| `required-version` | `"26.5.1"` | Pinned. Bump deliberately. |
| `include` | `'\.pyi?$'` | Default; only format `.py` and `.pyi` files. |
| `extend-exclude` | `.venv`, `.git`, `build`, `dist`, `.ao`, `design` | Prevents `black .` from touching the venv (slow, destructive) and the `design/` assets. |

To override locally for a one-off (rare):

```bash
black --line-length 100 path/to/file.py
```

## Verifying

From the repo root:

```bash
black --check .        # exit 0 = clean
black --diff .         # see what would change (no writes)
black .                # apply formatting in place
```

## CI / pre-push

This repo doesn't have a CI lint step yet. The agent-orchestrator framework auto-runs `black .` as a pre-push step (see [AGENT-DEV-PROCESS.md](AGENT-DEV-PROCESS.md) for the framework's role) — that step reads `pyproject.toml` and uses this config, so the editor and the pre-push stay in lockstep.
