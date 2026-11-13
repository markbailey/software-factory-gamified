# Software Factory

A top-down factory game in Godot where the factory is a real software pipeline.
Crates are work items, machines run real command-line tools, belts are queues.
The Python backend owns the truth; the game shows it and sends commands.

```
game/       Godot 4 project (GDScript) - open game/project.godot
backend/    Python service: WebSocket API, SQLite work store, CLI stage runner
protocol/   The messages between game and backend
docs/       Architecture and milestone notes
workspace/  The throwaway test project machines run in (git-ignored)
```

## Run it

You need Python 3.10+.

1. Start the backend. The first time, create a virtual environment and install into it.
   No activation script is needed, so this works where PowerShell scripts are blocked:
   ```
   cd backend
   python -m venv .venv
   .venv\Scripts\python -m pip install -e .
   .venv\Scripts\python -m factory
   ```
   After that, only the last line is needed. (macOS/Linux: use `.venv/bin/python`.)
   (If you use [uv](https://docs.astral.sh/uv/), `uv run python -m factory` does all of this in one step.)
   It listens on `ws://127.0.0.1:8765/ws` and prepares `workspace/test-project` (see below).
2. Open `game/project.godot` in Godot and press Play (F5).
3. Type a spec in the top bar and press Enter, for example "Add a greet(name) function".
   The crate travels down the line:
   - **Coder** fetches, makes branch `crate/<n>` in its own worktree under
     `workspace/worktrees/crate-<n>`, runs `claude -p` on the spec (prompt on stdin; it can
     read and edit files, but not run commands), and commits.
   - **Tester** runs the project's tests (`backend/scripts/run_tests.py`: unittest, or pytest
     if installed). No tests counts as a pass.
   - **Packager** pushes the branch and opens a pull request with `gh pr create`.

   A failure at any step sends the crate to the scrap bin with the full log. Several crates
   can be in the line at once, one per machine.

### First-time setup for GitHub

- `gh auth login`, then `gh auth setup-git` so `git push` uses the same sign-in.
- The test project is `workspace/test-project`, connected to the `remote` in
  `backend/factory.toml` (markbailey/test-project). On start the backend clones it, or connects
  an existing local copy and pushes `main` if GitHub's is empty.

### Work from issue trackers

- **GitHub Issues:** add the label `factory` to an open issue in markbailey/test-project.
  Within a minute it becomes a crate (marked with a blue dot). Its PR says "Closes #n", and the
  factory comments on the issue when the PR opens or the crate is scrapped. Each issue is
  only picked up once.
- **Jira:** set `enabled = true` under `[intake.jira]` in `backend/factory.toml`, fill in
  `base_url`, `email` and `jql`, and set the `JIRA_API_TOKEN` environment variable to an API
  token before starting the backend. Jira issues get a comment when their PR opens.

Click any crate to see its log. Stop and restart the backend while the game is running:
the game reconnects and redraws from the backend's state.

## Changing the line

The line is the list of `[[machines]]` in `backend/factory.toml`, in order; the game draws
whatever is there. Each machine is a list of steps. Each step is a command, a working
folder and optionally text sent on stdin; placeholders such as `{text}`, `{branch}` and
`{worktree}` are listed in that file. Only executables in `[security] allowed_commands` can
run, and only in folders inside `workspace/`. The prompt Claude gets is the Coder's `prompt`.

If you delete `backend/factory.db` to start fresh, also delete `workspace/` so crate
numbers and branch names don't collide.
