# NPC sprites

People on the factory floor, made by `tools/gen_npcs.py` (same palette and outline as
`gen_sprites.py`; edit the script and re-run rather than editing files by hand). The game code
that uses them is `game/scripts/npc_layer.gd` and `game/scripts/npc.gd`.

## Conventions

- People are 40x40 px, drawn on a 32-unit grid (viewBox `0 0 32 32`, exported at 1.25x), seen
  from straight above and **facing east (+x)**. The game rotates the sprite to face the way it
  walks, so one set of frames covers every direction. Centre the sprite on the node.
- Outline `#16181C`, 1.6 units on the figures. Coveralls carry the role colour.
- The staff door is 64x64 (one floor cell), anchored at its top-left like the other tiles.

## Files

| SpriteFrames | Loops (fps) | Look |
|---|---|---|
| `worker.tres` | `walk` (10), `carry` (10, arms out holding a crate ahead), `idle` (2), `work` (6, typing on a tablet), `wait` (4, arms folded, tapping a foot), `cheer` (8, arms up, hop) | Agent worker: blue `#4C8DF2` coveralls (the Running colour), white hard hat |
| `mechanic.tres` | `walk` (10), `run` (16), `idle` (2), `repair` (8, wrench swing) | Mechanic: steel coveralls, hazard-yellow `#E8C547` hard hat, red toolbox |
| `janitor.tres` | `walk` (10), `idle` (2), `sweep` (6, push broom side to side) | Janitor: mint-grey coveralls, flat cap, bucket |
| `door.tres` | `default` frames f0 (shut) to f3 (open); the game picks the frame | Staff door: steel frame, hazard stripes, two sliding leaves |

`footbridge.svg` (64x64) is a walkway over one straight belt cell, its deck running north-south
(for an east-west belt; the game turns it 90 degrees for a north-south belt). The game draws one
wherever you place a footbridge with the Rooms bar in build mode; people cross belts only there.

`worker_band.svg` is a white ring that sits on the worker's hard hat; the game tints it with the
accent colour of the machine the worker is at. `worker.svg`, `mechanic.svg`, `janitor.svg` and
`door.svg` are still frames for previews.

### Idle fidgets

Every few seconds (5 to 14, random) a person standing idle plays one pass of an `idle_*` loop, then goes back to `idle` (`npc.gd`, `_fidget_step`). A kind that lacks a loop just skips it.

| Loop | Frames (fps) | Who | Look |
|---|---|---|---|
| `idle_look` | 8 (6) | worker, mechanic, janitor | turns the head left, back, right, back (the hat peak shows the way) |
| `idle_stretch` | 8 (6) | worker, mechanic, janitor | both arms swing up and out to the sides and back; toolbox / bucket stay put |
| `idle_sip` | 8 (4) | worker | brings a coffee mug up to the face, holds it, lowers it |
