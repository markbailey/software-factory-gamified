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
     `workspace/worktrees/crate-<n>`, runs `claude -p` on the spec (it can read and edit
     files, but not run commands), and commits.
   - **Tester** installs dependencies and runs the project's tests
     (`backend/scripts/run_tests.py`: `pnpm test` / npm / yarn, plus unittest or pytest), then
     commits the lockfile if the install changed it. No tests counts as a pass.
   - **Packager** pushes the branch and opens a pull request (or reuses the branch's open one).
   - **Reviewer** has Claude review the diff, posts the review on the PR, then **waits for you**.
     The machine shows how many crates are waiting; they line up underneath it in yellow.
   - **CI monitor** waits for the PR's checks to pass (it passes after 30s if there are none).
   - **Merger** squash-merges the PR, then deletes the branch and worktree.

   A failure at any step sends the crate to the scrap bin with the full log. Several crates
   can be in the line at once, one per machine (the Reviewer and CI monitor take any number).

4. **Steer crates.** Click a crate, and buttons appear above its log:
   - **Approve** / **Reject** at the Reviewer. Reject closes the PR and scraps the crate, and
     your note is saved.
   - **Retry** a scrapped crate. It starts again at the Coder from a fresh branch, and your
     note is given to Claude ("Notes from the last attempt").
   - **Open PR** opens the crate's pull request in your browser.

   Crates waiting for approval stay waiting if you restart the backend.

A janitor in the backend tidies `workspace/` every 10 minutes (and at startup): it removes the
worktrees and local branches of shipped crates, of crates scrapped over a day ago, and of
crates the factory doesn't know about. Settings are under `[cleanup]` in `factory.toml`.

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

Press **Build line** in the top bar. The crates hide and a panel lists the machines in order:
- **Add** a machine type from the drop-down (it goes after the selected one), **Up** / **Down**
  to reorder, **Remove** to take one out.
- Click a machine on the floor or in the list to rename it, tick **Wait for my approval after
  this machine** to make it a gate, or edit Claude's prompt (Coder and Reviewer).
- The floor previews your draft as you go. **Save line** sends it to the backend, which checks
  it (for example the Packager needs a Coder before it) and only accepts it when no crates are
  in the line. **Cancel** throws the draft away.

The saved line is `backend/line.json`; delete it to go back to the default line. The machine
types and their steps live in `backend/machines.toml`. Each step is a command, a working folder
and optionally text sent on stdin; placeholders such as `{text}`, `{branch}` and `{worktree}`
are listed at the top of that file. Only executables in `[security] allowed_commands` (in
`factory.toml`) can run, and only in folders inside `workspace/`.

If you delete `backend/factory.db` to start fresh, also delete `workspace/` so crate
numbers and branch names don't collide.
