# Game ⇄ backend protocol (v0)

One WebSocket at `ws://127.0.0.1:8765/ws`. Every message is a JSON object with a `type`.
The backend owns the truth: the game only draws what events tell it and sends commands.

## Commands (game → backend)

| type | fields | meaning |
| --- | --- | --- |
| `submit_work` | `text` (string, max 20000 chars), `pipeline`? | Put a new spec crate on the intake of the line that takes typed specs (or the named pipeline) |
| `approve` | `item_id` | Let a crate waiting at a gate machine carry on |
| `reject` | `item_id`, `note`? | At a machine with a fail belt, send the crate back for rework with the note; otherwise close its PR and scrap it |
| `scrap` | `item_id`, `note`? | Close the PR of a crate waiting at a gate and scrap it |
| `retry` | `item_id`, `note`? | Send a scrapped crate back to the Coder, with the note for Claude |
| `intake_list` | `pipeline` | What the pipeline's intake sources have (answered by `intake_list`, to this game only) |
| `intake_pull` | `pipeline`, `source`, `ref` | Make one issue a crate now (GitHub issues only; `ref` is the issue number). Answered by `item_created` and a fresh `intake_list`, or `error` |
| `intake_check` | `pipeline`, `source`? | Poll the source (or all of the pipeline's sources) now, then answer with `intake_list` |
| `check_agent` | | Check now whether Claude is available (answered by `agent_status`) |
| `check_tools` | | Check the broken tools again now (answered by `tool_status`). A problem no check can see (a crashed factory script) is taken as fixed, and its step runs again |
| `save_pipelines` | `pipelines[]` (see Pipeline) | Replace every pipeline (build mode). Refused with an `error` if one is invalid or crates are still in the factory; on success every game gets a new `snapshot` |
| `save_line` | `machines[]` of `{type, id?, name?, gate?, prompt?, on_fail?}` | Older games: replace the first pipeline with a straight line |

## Events (backend → game)

Every event except `snapshot` and `error` carries `seq`, a number that only ever increases.
The game ignores any event whose `seq` is not greater than the last one it applied.

| type | fields | meaning |
| --- | --- | --- |
| `snapshot` | `seq`, `layout`, `items[]` | Full state, sent on connect and after the pipelines are saved. `seq` is the last event already included. `layout.pipelines` lists the pipelines (machines merged with their catalogue fields, plus `order`: machine ids in travel order), `layout.catalog` the machine types you can add, `layout.sources` the intake sources |
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
| `intake_list` | `pipeline`, `sources[]` | Reply to `intake_list`/`intake_pull`/`intake_check` (no `seq`). Each source: `source`, `enabled`, `about`, `poll_seconds`, `last_poll`, `next_poll` (unix seconds), `poll_error`, `error`, and `items[]` of `{ref, label, title, url, by, tags[], auto, can_pull, item_id, item_state}`. `auto` means it comes in by itself on the next poll; `item_id` is its crate, or null |
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

`pos` is the top-left cell of a 2x2 footprint on a grid of 64px cells. A belt leaves `intake`
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
