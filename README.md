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

   **Rework loops:** when the Tester, Reviewer or CI monitor fails, the crate rides the return
   belt back to the Coder. It keeps its branch and PR, and Claude gets what went wrong (the test
   output, the review, or the failing CI log) and fixes it on top of its earlier work. The
   Reviewer sends a crate back on its own when Claude's verdict is "Needs changes". After
   `max_rework` trips (3, in `backend/machines.toml`) the crate goes to the scrap bin.
   Any other failure sends the crate to the scrap bin with the full log. Several crates
   can be in the line at once, one per machine (the Reviewer and CI monitor take any number).

4. **Steer crates.** Click a crate, and buttons appear above its log:
   - **Approve** / **Send back** / **Scrap** at the Reviewer. Send back returns the crate to the
     Coder with your note; a "Request changes" review on GitHub does the same. Scrap closes the
     PR and throws the crate away. (At a gate with nowhere to send crates back, the button says
     Reject and scraps.)
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
  Within a minute it becomes a crate (marked with a blue dot). The factory assigns the issue
  to you and labels it `in progress`. Its PR says "Closes #n", and the factory comments on the
  issue when the PR opens or the crate is scrapped.
- **Jira:** set `enabled = true` under `[intake.jira]` in `backend/factory.toml`, fill in
  `base_url`, `email` and `jql`, and set the `JIRA_API_TOKEN` environment variable to an API
  token before starting the backend. Picked-up issues are assigned to you and moved to
  In Progress; shipped ones to Done.

### Why a crate was scrapped, and what happens to its issue

When a step fails, the factory first asks whether **its own tooling** broke rather than the
crate: `gh` signed out, git can't sign in, no network, a command that isn't installed, or one
of the factory's scripts crashing. Then nothing is scrapped. A red banner says what broke and
what to do, the crate waits at its machine, and machines that need that tool hold their crates.
The backend checks the tool every 30 seconds and the step runs again once it works (**Check
now** checks straight away; for a crashed script, fix it and press **Try again**).

Anything else that ends in the scrap bin gets a kind, shown in the scrap bin's list:

| Kind | Means |
|---|---|
| Needs info | Claude changed nothing: the issue is unclear or missing something (its questions are in the comment) |
| Already done | Claude changed nothing: the code already does it |
| Blocked | Claude changed nothing: something outside the repo stops it |
| Work failed | Tests, CI, a review or a step kept failing |
| Rejected | You scrapped or rejected it at a gate |

The issue it came from is **handed back**: a comment with the reason, a label for the kind
(`needs info`, `already done`, `blocked`, `factory failed`), unassigned, and the `factory`
label removed so it isn't picked straight back up (on Jira it moves back to To Do). Put the
`factory` label back once it's sorted and it comes in again. Nothing is closed or deleted.

Options under `[intake.github]` in `backend/factory.toml`: `assign = false` to leave the
assignee alone, `in_progress_label = ""` to skip that label, and `handback_labels` (a table
from kind to label, e.g. `handback_labels = { needs_info = "question" }`). Under
`[intake.jira]`: `assign`, `pickup_transition` (default "In Progress"), `handback_transition`
("To Do"), `shipped_transition` ("Done"), `trigger_label` ("factory"; the label that brings an
issue in, removed on hand-back so the JQL should include `labels = factory`) and
`handback_labels`. Set a transition to `""` to leave the status alone.

Waiting and shipped crates are off the floor too: a machine waiting for you shows how many in
its corner (click it for the list), and the shipping dock shows how many have shipped (click it
for the list).

### Review pull requests that are waiting for you

The review line picks up open, non-draft pull requests where you are a requested reviewer,
in any repo you can see (`gh search prs --review-requested=@me`), every two minutes. Each one
becomes a crate on the review line:
- **CI check** waits for the PR's checks to go green. Red checks send the crate to the scrap bin.
- **PR reviewer** saves the PR's description and diff (no checkout) and has Claude review them
  with read-only tools. Then it **waits for you**: click the crate, read the draft in its log,
  and press **Approve** to post it, or **Reject** to throw it away without posting anything.
  A note you type before Approve goes at the top of the review. Start the note with
  `Verdict: Comment` to post it as a comment instead of an approval or a request for changes.
- **Review poster** posts the review as you on GitHub, pinned to the commit Claude read.
  Claude's first line picks the kind: `Verdict: Approve`, `Verdict: Request changes` or
  `Verdict: Comment`. If new commits arrived in the meantime it posts nothing.

New commits on a PR make a fresh crate, and an older crate for that PR that is still waiting
for you is scrapped. GitHub removes you from the requested reviewers once you post, so a PR
comes back when its author asks you to review it again. The factory never closes or comments
on these PRs by itself. Settings (on/off, extra search terms such as `org:my-company`) are
under `[intake.review_requests]` in `backend/factory.toml`.

### Refiner: a clear spec before any code

The **Refiner** sits first on the build line, before the Coder. Claude reads the issue and the
repo (read-only) and rewrites the issue as a spec: the goal, the files involved, acceptance
criteria as a checklist, and what's out of scope. The Coder, Risk analyser and Reviewer then
work from that spec (`{spec}` in prompts; the original stays `{text}`), and it's in the
crate's log. If the issue is too vague, already done, or blocked by something outside the
repo, the Refiner hands it back straight away with Claude's questions, before any code is
written. If you built your build line in the game before the Refiner existed, add it in
build mode, in front of the Coder.

### Planning: from a product requirements doc to issues

The **Planning line** (under the other lines) turns a PRD into a plan and a set of issues that
the build line then works through.

- **Bring a PRD in:** label a GitHub issue `prd`, or click the Planning line's intake and paste
  the PRD into the box there, then press **Plan it**.
- **Planner** (waits for you): Claude reads the PRD and the repo (read-only) and writes a
  summary, the approach, open questions and a list of small issues, each with acceptance
  criteria and what it waits for. Read it in the crate's log. **Approve** it, or type a note and
  press **Send back** to have it planned again with your note. If the PRD is too unclear,
  Claude asks its questions instead and the PRD issue is handed back as "needs info".
- **Issue maker:** creates a tracking issue `Plan: ...` (label `plan`) with the plan and a task
  list, and one issue per task saying which issues it waits for. Tasks that wait for nothing
  get the `factory` label straight away, so the build line picks them up; each other task gets
  it once everything it waits for has shipped. It comments on the PRD issue with the link.

Settings are under `[intake.prd]` in `backend/factory.toml`: `enabled` (on whenever the GitHub
intake is), `label` ("prd") and `poll_seconds`. It uses the repo and labels from
`[intake.github]`. GitHub only for now.

### Merging when the branch needs an approval

The Merger waits until GitHub lets the pull request merge, then squash-merges it. While
branch rules hold it back (an approving review, required checks, a branch behind its base) the
crate stays on the Merger, and the machine shows how many are waiting and why; click it for
the list. It works on many PRs at once, so ready ones merge past the waiting ones. It checks
every 30 seconds and waits as long as it takes.

- Where the repo allows **auto-merge**, it turns it on, so GitHub merges the moment it's
  allowed even with the factory closed. Without auto-merge it just keeps checking itself.
- A branch that's behind its base is brought up to date once.
- If a reviewer **requests changes**, a **merge conflict** appears, or a required **check
  fails**, it turns auto-merge off (so nothing half-fixed merges) and sends the crate back to
  the Coder with the review or the error. After the fix it waits for the reviewer to look again.
- If the PR is **closed**, the crate goes to the scrap bin.

### When Claude isn't available

The backend checks that Claude is there every five minutes (a one-word request to the small
model), and straight away when a Claude step fails. While it isn't (not signed in, usage limit
reached, service down, no internet), a red banner says why, machines that use Claude show a
pause sign, and crates wait on the belt in front of them. Other machines (tests, CI, merging)
keep going. A Claude step that failed because Claude went away runs again, instead of the
crate failing. When a check passes the banner goes and everything carries on by itself.
**Check now** in the banner checks again straight away. Settings are under `[health]` in
`backend/factory.toml`.

### Risk analyser and approving low-risk changes by themselves

Put a **Risk analyser** after the Packager (it reads the PR's diff, so it needs a PR) and
before the Reviewer. Claude rates the change **low**, **medium** or **high** risk by its blast
radius: how much could break, for how many people, and how hard it would be to notice and
undo. Docs and tests are low; a shared helper or a new dependency is medium; auth, security,
data migrations, public APIs or CI/deploy config are high, and so is anything it can't rate.
The result is stamped on the crate (a green circle, amber triangle or red square in its
bottom-left corner) and shown in the crate's tooltip, log title and the machine status window.
The Reviewer is told the rating too. A crate sent back for rework loses its stamp and is
rated again on its next pass.

In build mode, a Reviewer that waits for your approval has **Approve by itself when the review
looks good and the risk is**: *Never: always ask me* (the default), *Low*, or *Low or medium*.
With *Low*, a crate whose review says "Looks good" and whose stamp is low goes straight on to
CI; anything higher, or with no stamp, still waits for you, and its log says why. Approving
this way needs a Risk analyser earlier on the line, and Save says so if there isn't one.
The PR reviewer on the review line never approves by itself, because its review is posted
under your name.

Click an **intake** to see what is waiting to come in from each of its sources: the open
GitHub issues in the repo, the Jira issues your JQL finds, and the pull requests waiting for
your review. Each row says whether it is already a crate (and where that crate is; **Crate #n**
opens its log), will come in by itself on the next check (labelled issues, Jira issues and
review requests), or is not in the factory. **Pull in** makes any open GitHub issue a crate on
that line straight away, labelled or not. **Check now** looks again without waiting for the
next poll, and **Open** opens the issue or PR in your browser. Sources that are switched off
in `factory.toml` say so. The window refreshes every 20 seconds.

Click a machine to open its status window: what it is doing now (the crate, the step it is
on with progress, and how long it has been at it), its steps ticked off, the crates waiting in
front of it or for your approval, where its failures go, and its latest output. It also says
when it is paused waiting for Claude. Esc or clicking empty floor closes it.

Scrapped crates drop into the **scrap bin** instead of piling up on the floor: the bin shows
whether it is empty or has crates in it, with a count. Click it for the list of scrapped crates,
newest first, each with why it stopped (the machine and step that failed, or your rejection)
and **Log** and **Retry** buttons. Clicking the shipping dock lists the shipped crates the
same way.

Whenever a crate is sent back for rework, it carries the reason: the failing step's output
(test failures, CI logs, the review), and when you send it back from a gate, your note plus
what that machine found. With no note, the Coder is told to work from the machine's findings.
If Claude ends up changing nothing, the Coder fails with "Claude made no changes" and
Claude's own explanation (a vague spec, work that's already done, notes it disagrees with),
which also goes into the comment on the GitHub or Jira issue.

Move around the floor by dragging empty floor with the left button (or with the right or
middle button anywhere), with the arrow keys or WASD, and zoom with the scroll wheel. F fits
the whole factory in view.

Click any crate to see its log. Stop and restart the backend while the game is running:
the game reconnects and redraws from the backend's state.

## Building your own pipelines

Press **Build** in the top bar. The crates hide, a grid appears on the floor and the build
menu opens on the right: your lines, **New line** / **Delete line**, and the machines you can
add. Click anything on the floor to open its own settings window on the left. You can have
several lines (pipelines), each with its own intake, shipping dock and scrap bin.
- **Place a machine:** click its button under **Add a machine**, then click an empty spot on
  the floor (Esc stops).
- **Move** anything (machines, the intake, dock and scrap bin) by dragging it.
- **Lay a belt:** every machine has two outputs on its right side. Drag from the **green**
  port (it passed) to the next machine or the dock. Drag from the **red** port (it failed)
  back to an earlier machine to send failures there for rework, keeping the branch and PR.
  A machine with no red belt fails into the scrap bin. Belts find their own way round
  machines; a second rework belt into the same machine joins the first.
- **Machine settings:** rename it, make it wait for your approval, pick where its failures go,
  set a time limit for each step, or edit Claude's prompt (Coder and Reviewer). It also lists
  the machine's steps.
- **Intake settings:** the line's name and where its crates come from. Tick any of specs typed
  in the game, GitHub issues, Jira issues and PRs waiting for your review. Each source feeds
  one line (a source another line takes is greyed out). GitHub and Jira issues go to the line
  that takes typed specs if no line ticks them. The sources show under each intake.
- **Remove:** select a machine or a belt and press Delete (or the Remove button). Removing a
  machine joins the machine before it to the one after it.
- **Save** sends everything to the backend, which checks it (every machine on one path from
  the intake to the dock, a Coder before the Packager, rework belts going back, and so on) and
  only accepts it when the factory is empty. If it refuses, the panel says why. **Cancel**
  throws the draft away.

The saved layout is `backend/pipelines.json`; delete it to go back to the default (the build
line from `default_line` in `machines.toml`, or `line.json` if you saved one with an older
game, plus the review line).

The machine types and their steps live in `backend/machines.toml`. Each step is a command, a working folder
and optionally text sent on stdin; placeholders such as `{text}`, `{branch}` and `{worktree}`
are listed at the top of that file. Only executables in `[security] allowed_commands` (in
`factory.toml`) can run, and only in folders inside `workspace/`.

If you delete `backend/factory.db` to start fresh, also delete `workspace/` so crate
numbers and branch names don't collide.
