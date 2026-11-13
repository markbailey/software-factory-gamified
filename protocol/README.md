# Game ⇄ backend protocol (v0, slices 1-3)

One WebSocket at `ws://127.0.0.1:8765/ws`. Every message is a JSON object with a `type`.
The backend owns the truth: the game only draws what events tell it and sends commands.

## Commands (game → backend)

| type | fields | meaning |
| --- | --- | --- |
| `submit_work` | `text` (string, max 4000 chars) | Put a new spec crate on the intake dock |

Coming in later slices: `place_machine`, `connect_belt`, `approve`, `reject`, `retry`.

## Events (backend → game)

Every event except `snapshot` and `error` carries `seq`, a number that only ever increases.
The game ignores any event whose `seq` is not greater than the last one it applied.

| type | fields | meaning |
| --- | --- | --- |
| `snapshot` | `seq`, `layout`, `items[]` | Full state, sent once on connect. `seq` is the last event already included. `layout.machines` lists `{id, name, steps[]}` in line order |
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
`state` is one of `queued`, `waiting`, `running`, `moving`, `shipped`, `scrapped`.

## Machine output conventions

A line of the form `PROGRESS 0.4` becomes a `job_progress` event instead of a log line.
Exit code 0 means success; anything else scraps the crate.
