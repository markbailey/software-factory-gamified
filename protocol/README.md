# Game ⇄ backend protocol (v0, slices 1-5)

One WebSocket at `ws://127.0.0.1:8765/ws`. Every message is a JSON object with a `type`.
The backend owns the truth: the game only draws what events tell it and sends commands.

## Commands (game → backend)

| type | fields | meaning |
| --- | --- | --- |
| `submit_work` | `text` (string, max 4000 chars) | Put a new spec crate on the intake dock |
| `approve` | `item_id` | Let a crate waiting at a gate machine carry on |
| `reject` | `item_id`, `note`? | Close the crate's PR and scrap it; the note is saved |
| `retry` | `item_id`, `note`? | Send a scrapped crate back to the Coder, with the note for Claude |
| `save_line` | `machines[]` of `{type, id?, name?, gate?, prompt?}` | Replace the line (build mode). Refused with an `error` if the line is invalid or crates are still in it; on success every game gets a new `snapshot` |

## Events (backend → game)

Every event except `snapshot` and `error` carries `seq`, a number that only ever increases.
The game ignores any event whose `seq` is not greater than the last one it applied.

| type | fields | meaning |
| --- | --- | --- |
| `snapshot` | `seq`, `layout`, `items[]` | Full state, sent on connect and after a line is saved. `seq` is the last event already included. `layout.machines` lists the line in order and `layout.catalog` the machine types you can add; both use the machine fields below |
| `item_created` | `item` | A new crate at `intake` |
| `item_moved` | `item_id`, `to`, `duration`, `state`? | Crate starts moving to location `to`, arriving after `duration` seconds |
| `job_started` | `item_id`, `machine`, `command[]` | Machine started; `command` lists its step names |
| `job_log` | `item_id`, `machine`, `line` | One line of the command's output |
| `job_progress` | `item_id`, `machine`, `progress` (0 to 1), `step`? | Machine moved to a new step, or a command printed `PROGRESS <n>` |
| `job_finished` | `item_id`, `machine`, `exit_code` | Command exited with 0 |
| `pr_opened` | `item_id`, `url` | A machine's output contained a GitHub pull request URL |
| `job_failed` | `item_id`, `machine`, `exit_code`, `step` | Command failed or was refused; the crate goes to `scrap` |
| `error` | `message` | The last command was rejected |

## Item

```json
{ "id": 1, "text": "Add a login page", "state": "running", "location": "machine",
  "created_at": 1763060000.0, "log": ["$ python demo_job.py ...", "Received spec: ..."] }
```

`location` is `intake`, `belt_<i>` (the belt in front of machine i; the last one leads to the
dock), a machine id, `dock` or `scrap`. Items also carry `source` (`game`, `github`, `jira`),
`source_ref` (issue number or key), `source_url` and `pr_url`.
`state` is one of `queued`, `waiting`, `running`, `awaiting_approval`, `moving`, `shipped`, `scrapped`.
Items also carry `notes` (your last reject or retry note) and `review` (Claude's last reply).

## Machine

```json
{ "id": "tester-2", "type": "tester", "name": "Second tester", "description": "...",
  "sprite": "tester", "steps": ["Run tests", "Commit lockfile"], "gate": false,
  "prompt": "", "has_prompt": false, "needs": ["worktree"], "provides": [], "unique": false }
```

`needs` and `provides` say what a machine requires earlier in the line (`worktree` comes from
the Coder, `pr` from the Packager). A `unique` type can appear once.

## Machine output conventions

A line of the form `PROGRESS 0.4` becomes a `job_progress` event instead of a log line.
Exit code 0 means success; anything else scraps the crate.
