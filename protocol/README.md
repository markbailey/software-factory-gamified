# Game ⇄ backend protocol (v0)

One WebSocket at `ws://127.0.0.1:8765/ws`. Every message is a JSON object with a `type`.
The backend owns the truth: the game only draws what events tell it and sends commands.

## Commands (game → backend)

| type | fields | meaning |
| --- | --- | --- |
| `submit_work` | `text` (string, max 20000 chars), `pipeline`?, `storage`? | Put a new spec crate on the intake of the line that takes typed specs (or the named pipeline). With `storage` (the id of a Custom storage belted into that pipeline's intake), it joins the end of that storage's queue instead: the queue (kept across restarts) goes onto the line one crate at a time, each once the line isn't paused or full and the last one from that storage has left the intake. Each crate comes from that storage, is worked on in its repo and as its account. The queue is in that storage's `intake_list` entry: rows with `queue_id`, `queue_pos`, `queue_len` and the full `text`, next first |
| `custom_queue_edit` / `custom_queue_remove` / `custom_queue_move` | `id` (a row's `queue_id`), `text` (edit), `delta` (move: -1 up, 1 down) | Change a Custom storage's queue before the line takes the change. An `error` if it has already gone onto the line |
| `get_accounts` | | Answered by `accounts` |
| `save_account` | `service` ("github" or "jira"), `account` `{id?, name, token?, git_name?, git_email?}` / `{id?, name, base_url, email, token?}` | Add an account (no `id`) or change one. A token left out is kept, `""` clears it. Answered by `accounts` with `saved` (its id) and `check` |
| `remove_account` | `service`, `id` | Remove one; lines and storages that picked it use the default. Answered by `accounts` |
| `set_default_account` | `service`, `id` (`"@machine"` for this machine's sign-in) | The account the service uses unless a line or storage picks another. Answered by `accounts` |
| `check_accounts` | | Check who each account and this machine's sign-in is; answered by `accounts` with `check` |
| `approve` | `item_id` | Let a crate waiting at a gate machine carry on |
| `reject` | `item_id`, `note`? | At a machine with a fail belt, send the crate back for rework with the note; otherwise close its PR and scrap it |
| `scrap` | `item_id`, `note`? | Scrap a crate: one waiting at a gate or held, or (Scrap now) one on a belt or in a machine, whose work and running command are stopped first. Its PR is closed if the bin says so |
| `retry` | `item_id`, `note`? | Send a scrapped crate back to the Coder, with the note for Claude |
| `intake_list` | `pipeline` | What the pipeline's intake sources have (answered by `intake_list`, to this game only) |
| `intake_pull` | `pipeline`, `source`, `ref` | Make one issue a crate now (GitHub issues only; `ref` is the issue number). Answered by `item_created` and a fresh `intake_list`, or `error` |
| `storage_enabled` | `pipeline`, `storage`, `enabled` | Switch a storage on or off, saved with the layout (as `"enabled": false` on that storage in `pipelines.json`; build mode's storage settings have the same tick). A storage switched off stays belted in but isn't polled and brings nothing in (a Custom storage's queue waits); its `intake_list` entry has `disabled: true`. Answered by a fresh `snapshot`, or `error` |
| `intake_check` | `pipeline`, `source`? | Poll the source (or all of the pipeline's sources) now, then answer with `intake_list` |
| `intake_close` | `pipeline`, `source`, `storage`, `refs[]`, `comment`?, `reason`?, `transition`? | Close issues or PRs sitting in a storage (`refs` are row `ref`s from `intake_list`: issue/PR numbers, PR urls for `pr_review`, Jira keys), as the account that storage works as. `comment` is posted first. GitHub issues: `reason` `completed` or `not planned`. Open PRs and PRs to review are closed (branch kept). Jira: moved through `transition` (its name or target status), or by default the transition into a done-category status (Done/Closed/Resolved first). Answered by `intake_closed` and a fresh `intake_list` without them, or `error`. Closing is outward-facing: the game confirms first |
| `intake_close_options` | `pipeline`, `source`, `storage`, `refs[]` | How `intake_close` would close these: answered by `intake_close_options` |
| `check_agent` | | Check now whether Claude is available (answered by `agent_status`) |
| `refresh_pr_states` | | Check again on GitHub the PRs of crates out of the line (shipped or scrapped) whose PR wasn't merged or closed when last checked, and send `pr_state` for each that changed. Batched (one GitHub call per 50 PRs) and at most once every 30 seconds; the backend also does it every 5 minutes. The dashboard sends it when it opens |
| `check_tools` | | Check the broken tools again now (answered by `tool_status`). A problem no check can see (a crashed factory script) is taken as fixed, and its step runs again |
| `crate_arrivals` | `on`? (default true) | This client will send `crate_arrived`. While at least one such client is connected, a crate waits on the belt until it is shown there before the machine starts on it (at most 20 seconds past the belt time). With none connected there is no wait |
| `crate_arrived` | `item_id`, `location` | The crate finished riding to `location` (the `to` of its last `item_moved`, or where the snapshot put it) |
| `save_pipelines` | `pipelines[]` (see Pipeline) | Replace every pipeline (build mode). Refused with an `error` if one is invalid or crates are still in the factory; on success every game gets a new `snapshot` |
| `save_line` | `machines[]` of `{type, id?, name?, gate?, prompt?, on_fail?}` | Older games: replace the first pipeline with a straight line |

## Events (backend → game)

Every event except `snapshot` and `error` carries `seq`, a number that only ever increases.
The game ignores any event whose `seq` is not greater than the last one it applied.

| type | fields | meaning |
| --- | --- | --- |
| `snapshot` | `seq`, `layout`, `items[]` | Full state, sent on connect and after the pipelines are saved. `seq` is the last event already included. `layout.pipelines` lists the pipelines (machines merged with their catalogue fields, plus `order`: machine ids in travel order), `layout.catalog` the machine types you can add, `layout.sources` the intake sources |
| `accounts` | `github[] {id, name, token_set, git_name, git_email}`, `jira[] {id, name, token_set, base_url, email}`, `defaults {github, jira}`, `machine {jira_site, jira_email, jira_ready}`, `file`, `saved`?, `check`? `{github: {<id or @machine>: {ok, message, login?}}, jira: {...}}`, `error`? | The factory's accounts, to this game only. Tokens are never sent. Pipelines choose with `accounts {github?, jira?}` and a storage with `account`: an id, `"@machine"`, or none for the default (the line's, for a storage) |
| `item_created` | `item` | A new crate at its pipeline's intake |
| `item_moved` | `item_id`, `to`, `duration`, `state`? | Crate starts moving to location `to`, arriving after `duration` seconds |
| `job_started` | `item_id`, `machine`, `command[]` | Machine started; `command` lists its step names |
| `job_log` | `item_id`, `machine`, `line` | One line of the command's output |
| `job_progress` | `item_id`, `machine`, `progress` (0 to 1), `step`? | Machine moved to a new step, or a command printed `PROGRESS <n>` |
| `job_finished` | `item_id`, `machine`, `exit_code` | Command exited with 0 |
| `item_risk` | `item_id`, `risk`, `reason` | A Risk analyser stamped the crate `low`, `medium` or `high` (`""` clears the stamp, when the crate is sent back for rework or retried) |
| `pr_opened` | `item_id`, `url` | A machine's output contained a GitHub pull request URL |
| `job_failed` | `item_id`, `machine`, `exit_code`, `step` | Command failed or was refused; the crate goes to `scrap` |
| `error` | `message` | The last command was rejected |
| `intake_list` | `pipeline`, `sources[]` | Reply to `intake_list`/`intake_pull`/`intake_check` (no `seq`). Each source: `source`, `enabled`, `about`, `poll_seconds`, `last_poll`, `next_poll` (unix seconds), `poll_error`, `error`, `can_close` (its items can be closed with `intake_close`), `account` (who it works as, e.g. "the work account"), `disabled` (true: the storage is switched off), and `items[]` of `{ref, label, title, url, by, tags[], auto, can_pull, item_id, item_state}`. `auto` means it comes in by itself on the next poll; `item_id` is its crate, or null |
| `intake_closed` | `pipeline`, `source`, `storage`, `account`, `closed[]`, `failed[]` | Reply to `intake_close` (no `seq`): the refs closed, and `{ref, error}` for each that wasn't |
| `intake_close_options` | `pipeline`, `source`, `storage`, `refs[]`, `account`, `transitions[]`, `default` | Reply to `intake_close_options`. Jira: `transitions[]` of `{id, name, to, done}` for the first ref, done ones first, and `default` the name `intake_close` uses with no `transition`. Empty for GitHub |
| `agent_status` | `available`, `reason`, `detail`, `since`, `checked_at`, `next_check_at`, `enabled` | Whether Claude is available (no `seq`; also in the snapshot as `agent`). While it isn't, machines with `uses_agent` hold their crates and a Claude step that failed runs again when it's back |
| `tool_status` | `outages[]` of `{key, label, reason, detail, since, can_check}` | The factory's own tooling that is broken: `gh` signed out, `git` can't sign in, `network`, a missing command, or `scripts` (one of the factory's scripts crashed). No `seq`; also in the snapshot as `tools`. A crate whose step failed this way waits at its machine and the step runs again once a check passes (every 30 seconds, or on `check_tools`); machines whose steps use that tool hold their crates meanwhile. `can_check` false means only `check_tools` clears it |
| `item_held` | `item_id`, `machine`, `kind`, `reason` | The crate is held at the machine (see `held` below); `kind` is the scrap reason it would have had |
| `item_waiting` | `item_id`, `machine`, `reason` | The crate's step is waiting on something outside the factory, e.g. "Waiting for an approving review on GitHub" (the Merger). `""` clears it. Not a failure; the crate stays `running`. Also on items as `waiting_for` |
| `item_triaged` | `item_id`, `kind`, `reason` | Why a crate was scrapped: `needs_info`, `already_done`, `blocked` (Claude changed nothing and said why), `work_failed`, or `rejected` (you scrapped it at a gate). Sent just before it moves to the scrap bin |

## Item

```json
{ "id": 1, "text": "Add a login page", "state": "running", "location": "machine",
  "created_at": 1763060000.0, "log": ["$ python demo_job.py ...", "Received spec: ..."] }
```

`pipeline` is the id of the pipeline the crate is on. `location` is `<pipeline>:intake`,
`<pipeline>:belt_<i>` (in front of the i-th machine in travel order; the last one leads to the
dock), a machine id, `<pipeline>:return_<i>` (the rework belt at machine i, for crates sent back),
`<pipeline>:dock` or `<pipeline>:scrap`. `loops` counts how many times the crate has been sent
back. Items also carry `source` (`game`, `github`, `jira`, `pr_review`, `prd`), `source_ref` (issue
number or key), `source_url`, `pr_url` and `head_sha` (the commit a PR review is pinned to).
`state` is one of `queued`, `waiting`, `running`, `awaiting_approval`, `held`, `moving`, `shipped`, `scrapped`.

`held`: the crate failed (or was rejected) at a machine and can't go anywhere: the machine's red
belt doesn't run to the scrap bin, or doesn't scrap for this reason (`scrap_when`), or rework ran
out. It waits at the machine (`location` is the machine) with the reason in `waiting_for`. Send
`retry` to run the machine again (with an optional `note`), or `scrap` to scrap it.
Items also carry `notes` (your last reject or retry note), `review` (Claude's last reply), and
`risk` (`""`, `low`, `medium`, `high`) with `risk_reason` once a Risk analyser has rated them,
`outcome` (what the Coder said when it changed nothing: `needs_info`, `already_done`, `blocked`)
and `scrap_kind` (see `item_triaged`) once scrapped.

## Machine

```json
{ "id": "tester-2", "type": "tester", "name": "Second tester", "description": "...",
  "sprite": "tester", "steps": ["Run tests", "Commit lockfile"], "gate": false,
  "prompt": "", "has_prompt": false, "needs": ["worktree"], "provides": [], "unique": false,
  "on_fail": "", "uses_agent": false, "parallel": false, "rework_here": false,
  "can_auto_approve": false, "auto_approve": "" }
```

`can_auto_approve` types (the Reviewer) take an `auto_approve` setting: `""` (always ask),
`"low"` or `"medium"`. A gate with it set approves by itself when its steps passed and the
crate's `risk` is at most that level; it needs a machine that `provides` `risk` (the Risk
analyser) earlier on the line.

`on_fail` is only used by `save_line`; pipelines say where failures go with `fail` belts. `needs` and `provides` say what a machine requires earlier in the line (`worktree` comes from
the Coder, `pr` from the Packager). A `unique` type can appear once. A `rework_here` machine
(the Planner) runs again itself when it fails or you send a crate back at its gate, with no
fail belt.

## Pipeline

```json
{ "id": "build", "name": "Build", "sources": ["game", "github", "jira"],
  "intake": {"pos": [1, 3]}, "dock": {"pos": [26, 3]}, "scrap": {"pos": [14, 7]},
  "machines": [{"id": "coder", "type": "coder", "name": "Coder", "pos": [5, 3],
                "gate": false, "prompt": "...", "timeout_seconds": null, "auto_approve": ""}],
  "belts": [{"from": "intake", "port": "out", "to": "coder", "path": [[3, 3], [4, 3]]},
            {"from": "coder", "port": "pass", "to": "dock"},
            {"from": "tester", "port": "fail", "to": "coder"}] }
```

`pos` is the top-left cell of a 2x2 footprint on a grid of 64px cells. `rot` (optional, on a machine,
station, bin, dock or storage) is how many quarter turns clockwise it's turned: 0 or missing
takes belts in on the west side and sends them out on the east, 1 in from the north and out
south, 2 in from the east, 3 in from the south. A belt leaves `intake`
from `out`, or a machine from `pass` or `fail`, and goes to a machine or `dock`. Following
`pass` belts from the intake must reach the dock through every machine once; a `fail` belt goes
back to an earlier machine (rework). `path` is the belt's cells as the game laid them out; the
backend only stores it. `timeout_seconds` (when set) replaces each step's time limit. Machine
ids are unique across all pipelines. `sources` lists where the intake takes crates from
(`game`, `github`, `jira`, `pr_review`); each source feeds at most one pipeline. Older files
have a single `source` string, which still works.

## Machine output conventions

A line of the form `PROGRESS 0.4` becomes a `job_progress` event instead of a log line.
A line `RISK <low|medium|high> <why>` stamps the crate (an `item_risk` event).
A line `WAITING <why>` marks the crate as waiting on something outside (an `item_waiting`
event); a bare `WAITING` clears it, and so does the step ending. A line `NO_REWORK` makes a
failing step scrap the crate even on a machine with `on_fail` (rework can't fix it).
A line `OUTCOME <needs_info|already_done|blocked>` (printed by `check_changes.py` when Claude
changed nothing) records why, and the crate is scrapped as that kind if it can't go back.
Exit code 0 means success. Otherwise the factory first asks whether its own tooling failed
(`triage.py`): if so the crate waits and the step runs again once it's fixed; if not, the
crate goes back for rework or to the scrap bin.
