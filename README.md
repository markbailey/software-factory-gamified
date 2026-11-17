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
   - **Document writer** has Claude write the pull request's title and description (filling in
     the repo's PR template if it has one).
   - **Packager** pushes the branch and opens a pull request (or reuses the branch's open one),
     using that title and description. It runs no AI itself.
   - **Risk analyser** has Claude rate the change low, medium or high risk and stamps the crate.
   - **Reviewer** has Claude review the diff and posts the review on the PR. It only reviews and
     comments: a review asking for changes leaves by its red port.
   - **Approver** **waits for you** (or for a review on GitHub, or lets low-risk changes through
     by itself). The machine shows how many crates are waiting; they line up underneath it in yellow.
   - **CI** waits for the PR's checks to pass (it passes after 30s if there are none).
   - **Merger** merges the PR (squash by default), then deletes the branch and worktree.

   **Red belts:** a crate that fails leaves by the machine's red port and goes wherever its belt
   goes. On the default line the Tester, Reviewer, Approver and CI red belts run back to the
   Coder: the crate keeps its branch and PR, and Claude gets what went wrong (the test output,
   the review, or the failing CI log) and fixes it on top of its earlier work. After
   `max_rework` trips (3, in `backend/machines.toml`) the crate goes to the scrap bin. A red
   belt can also lead on to any other machine or dock, or into a scrap bin; with no red belt,
   a failed crate waits at the machine for you. Several crates can be in the line at once, one
   per machine (the Approver and CI take any number).

4. **Steer crates.** Click a crate, and buttons appear above its log:
   - **Approve** / **Send back** / **Scrap** at the Approver. Send back returns the crate to the
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

## Tests

The backend has tests that run a real factory in a temporary folder, with stand-ins for
`claude` and `gh` (so they cost nothing and touch no GitHub repo). Install pytest once, then run
them from `backend`:
```
.venv\Scripts\python -m pip install -e .[test]
.venv\Scripts\python -m pytest
```
They take about a minute. They check that crates ship, survive the backend being killed or
stopped mid-step, wait at paused machines, cope with a layout saved while they run, follow a red
belt laid after they were held, and that parallel crates don't clash in git.

## First-time setup for GitHub

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
  with read-only tools. It posts nothing itself.
- **Approve review** (an Approver) **waits for you**: click the crate, read the draft in its log,
  and press **Approve** to post it, or **Reject** to throw it away without posting anything.
  A note you type before Approve goes at the top of the review. Start the note with
  `Verdict: Comment` to post it as a comment instead of an approval or a request for changes.
- **Review poster** (a Document writer) posts the review as you on GitHub, pinned to the commit Claude read.
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
before the Approver. Claude rates the change **low**, **medium** or **high** risk by its blast
radius: how much could break, for how many people, and how hard it would be to notice and
undo. Docs and tests are low; a shared helper or a new dependency is medium; auth, security,
data migrations, public APIs or CI/deploy config are high, and so is anything it can't rate.
The result is stamped on the crate (a green circle, amber triangle or red square in its
bottom-left corner) and shown in the crate's tooltip, log title and the machine status window.
The Reviewer is told the rating too. A crate sent back for rework loses its stamp and is
rated again on its next pass.

In build mode, an Approver has **Approve by itself when the crate's risk is**: *Never: always
ask me* (the default), *Low*, or *Low or medium*. With *Low*, a crate whose stamp is low goes
straight on to CI; anything higher, or with no stamp, still waits for you, and its log says
why. (A review asking for changes has already left by the Reviewer's red port.) Approving this
way needs a Risk analyser earlier on the line, and Save says so if there isn't one. The
Approve review machine on the review line never approves by itself, because its review is
posted under your name.

Click an **intake** (or one of its storages) to see what is waiting to come in from each of its sources: the open
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

## Belt speed

Crates ride the belts at the belts' own speed, so a longer belt takes longer. Set how fast every
belt runs with **Belt speed** in the build panel (0.5 to 6 cells a second; it changes straight
away, no Save needed). The belt animation runs to match.

## Retries

A machine can try a failed crate again by itself: in build mode, click it and set **Retries
when it fails** (up to 5). The crate goes round the same machine again with why it failed as
its notes (a Coder sees the error and fixes it), and only when the retries are used up does it
take its red belt or wait for you. A red belt back to an earlier machine is used instead of
retries. Run again on a held crate gives it its retries back.

## Crates sent to the wrong machine

Every Claude machine is told what the other kinds of machine do. If a crate isn't its kind of
work (a Coder given a pull request that only needs merging, say), it changes nothing and says
which machine it needs. If the line has one, a worker carries the crate straight to it. If it
doesn't (or the crate has been passed round three times), the crate is rolled back into the
scrap bin as **Not for this line**, with a rollback label:

- an issue goes back to the to-do list (Jira: the handback transition, usually To Do),
  unassigned and labelled `needs <machine>` (Jira: `needs-merger`), and is never closed,
  even by a bin that closes issues;
- a pull request stays open and gets the same `needs <machine>` label, so a line that has
  that machine can pick it up with a storage search such as `label:"needs merger"`.

Picking the issue up again takes the old `needs ...` label off.

## Limit crates per line

To stop a line jamming, set **Most crates on the line at once** in its intake's settings (build
mode, click the intake). When that many crates are on the line (on belts, in machines or waiting
at a gate for you), new ones wait at the intake and its storages bring nothing new in until one
leaves. Crates held for you don't count. 0 means no limit.

## Stats, Claude cost and budgets

The chart button in the top bar (next to pause) opens **Factory stats**: crates shipped and
scrapped, the typical time from intake to dock, how many shipped only after rework, and what
Claude cost, per day, per line and per machine (runs, time per run, how often it fails and at
which step). Pick Today, 7 or 30 days at the top. A crate's log shows what Claude has cost on it
so far. Costs are what Claude Code reports for each run; on a subscription that's what the
same use would cost on the API, not a bill.

To cap spending, set these in `backend/factory.toml` (0 means no limit) and restart the backend:
```
[budget]
crate_usd = 2      # a crate that has cost this is held before its next Claude machine
daily_usd = 20     # after this much today, crates wait in front of Claude machines until midnight
```
A held crate's Run again lets it carry on past its budget.

## Alerts when the game is closed

The backend can tell you when a crate waits for your approval, is held or scrapped, when
Claude goes away, or when the day's budget is spent. Add to `backend/factory.toml`:
```
[alerts]
desktop = true                             # a Windows notification on this PC
webhook = "https://ntfy.sh/some-long-random-topic"   # optional: to your phone (ntfy app)
on = ["approval", "held", "scrapped", "claude_down", "budget"]   # add "shipped" for every PR
```
The webhook can also be a Discord or Slack incoming-webhook URL. Anyone who knows an ntfy topic
name can read it, so pick a long random one.

## Pausing, and stopping the backend

- **Pause** a machine from its window (click it), a line from its intake's window, or the whole
  factory with **Pause factory** in the top bar. A crate already in a machine finishes it; the
  others wait on the belt in front of the next paused machine, and a paused line or factory
  brings nothing new in from its storages. Paused things are dimmed with a blue pause sign.
  What's paused is kept in `backend/paused.json`, so it stays paused after a restart.
- **Closing the game or stopping the backend** doesn't scrap anything any more: when the backend
  starts again, each crate it left on a belt or in a machine carries on from that machine
  (which runs again from its first step). Crates waiting for you or held stay where they were.
  A crate whose line can't run any more waits, held, until the line is back (or you scrap it).

## Building your own pipelines

Press **Build** in the top bar. The crates hide, a grid appears on the floor and the build
menu opens on the right: your lines, **New line** / **Delete line**, and the machines you can
add. Click anything on the floor to open its own settings window on the left. You can have
several lines (pipelines), each with its own intake, shipping dock and scrap bin.
- **Place a machine:** click its button under **Add a machine**, then click an empty spot on
  the floor (Esc stops).
- **Move** anything (machines, the intake, dock and scrap bin) by dragging it. Shift+drag
  moves its whole line.
- **Pick several:** Shift+click machines (again to take one out), or hold Shift and drag a box
  on empty floor. A box also picks the room blocks inside it (walls, windows, doors, belt ports,
  floor, signs, footbridges and worker doors). Picked things are outlined in blue; drag any of
  them to move them all, with belts between picked machines keeping their shape, or press
  Delete to remove them. One Undo puts machines and blocks back together. Esc lets go.
- **Undo:** Ctrl+Z or the **Undo** button steps back one change at a time: moves, new and
  removed machines, belts, settings and room pieces, in the order you made them.
- **Lay a belt:** every machine has two outputs on its right side. Drag from the **green**
  port (it passed) to the next machine or the dock. Drag from the **red** port (it failed) to
  wherever failed crates should go: back to an earlier machine for rework (keeping the branch
  and PR), on to any machine or dock, or into a scrap bin. A machine with no red belt holds
  failed crates for you. Belts find their own way round machines; a second rework belt into
  the same machine joins the first.
- **Lay a belt by hand:** turn on **Conveyor belt** at the top of the machine list, then drag
  from an output across the floor, cell by cell, into where it goes (go back over a cell to take
  it up; Esc stops). Crates ride the path you laid, at the belt speed, and footbridges you place
  on its straight parts work like on any belt. If the way is blocked, or you later move something onto
  it, the belt finds its own way instead. Click a hand-laid belt and press **Let it find its own
  way** to hand it back to the router. Dragging from a port without Conveyor belt on still links
  machines the usual way.
- **Free conveyors and loops:** with **Conveyor belt** on, drag anywhere on open floor. A drag
  that doesn't run from an output into an input stays where you laid it as a free conveyor; bring
  it back round to the cell you started on (4 cells or more) to close a loop. Drag on from a free
  conveyor's end to make it longer; once one run goes from an output into an input it becomes a
  link and crates ride it. Free conveyors run with the other belts but carry no crates. Click one
  and press Delete to take it up.
- **Place first, link later:** machines, docks and bins can go down before there's any line. They
  sit on a line with no intake (saved, paused until it has one). Place an Intake while that line
  is picked and it becomes its intake; or belt into one of its machines from another line and it
  joins that line, along with the machines its belts lead on to.
- **Splitter and Belt merger:** from the machine list. A Splitter sends crates out of its two
  east outputs in turn (one each way, then round again). A Belt merger takes belts into either of
  its two inputs, from any machines or lines, and every crate carries straight on out of its one
  output. Neither does any work on the crates.
- **Joining belts:** end a hand-laid belt on the side of another and it runs into it (a merge).
  From a machine's output into a belt that carries crates, it's a link to wherever that belt goes,
  and crates run on along it. Conveyors join conveyors the same way. A belt can't branch off
  another one: use a Splitter for that.
- **Conveyor bridges:** lay a belt straight over a straight stretch of another and it bridges
  over it; crates on both carry on through. A bend, a join or a cell already bridged can't be
  crossed.
- **More docks and bins:** place **Shipping dock** or **Scrap bin** again from the machine list
  to add another to the line you're on. Crates ship into whichever dock their line's last belt
  runs into, and are scrapped (or rolled back) into whichever bin the machine's red belt runs
  into. Each bin has its own rules (close issues, close PRs, Jira status). Select an extra one
  and press Delete to remove it.
- **Machine settings:** rename it, make it wait for your approval (where that's a choice),
  set a time limit for each step, or edit Claude's prompt. The panel says where the red belt
  sends failed crates, and lists the machine's steps.
- **Storages:** issues and PRs come in from **Storage** machines. Place one, pick what it holds
  (Jira issues, GitHub issues, open PRs, PRs waiting for your review, or PRDs) and give it a
  filter (a JQL for Jira, a GitHub search for the others; blank uses the default shown). Then
  drag from its port into an intake: that line takes what's in it. GitHub storages (issues,
  open PRs, PRDs) can read from their own repo; left blank they read the line's. The line's
  repo, set on its intake, is where it codes, unless a storage names its own repo: then that
  storage's crates are coded, branched and PR'd in its repo, so one line can serve several
  repos. One intake can take several storages of the same source when they differ by repo or
  filter. Click a
  storage outside build mode to see what's in it; the number on it is how many aren't crates yet.
  Layouts saved before storages get one storage per ticked source when they load.
- **Intake settings:** the line's name, its repo, and whether it also takes specs typed in the
  game. The sources show under each intake.
- **Remove:** select a machine or a belt and press Delete (or the Remove button). Removing a
  machine joins the machine before it to the one after it.
- **Save** sends everything to the backend, which checks it (every machine on one path from
  the intake to the dock, a Coder before the Packager, rework belts going back, and so on). If
  it refuses, the panel says why. You can save while crates are on the line: a crate inside a
  machine finishes it, then each carries on at the next machine it was heading for. If that
  machine is gone it goes to the next one along its old way that's still there (or back to the
  start of the line), and if its whole line is gone it waits, held, until the line is back or
  you scrap it. **Cancel** throws the draft away.

The saved layout is `backend/pipelines.json`; delete it to go back to the default (the build
line from `default_line` in `machines.toml`, or `line.json` if you saved one with an older
game, plus the review line).

The machine types and their steps live in `backend/machines.toml`. Each step is a command, a working folder
and optionally text sent on stdin; placeholders such as `{text}`, `{branch}` and `{worktree}`
are listed at the top of that file. Only executables in `[security] allowed_commands` (in
`factory.toml`) can run, and only in folders inside `workspace/`.

If you delete `backend/factory.db` to start fresh, also delete `workspace/` so crate
numbers and branch names don't collide.
