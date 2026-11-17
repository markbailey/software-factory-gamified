# Sound effects

Made by `tools/gen_sfx.py` (in the shared art folder): every sound is synthesised from sine
waves, noise and envelopes. No samples or third-party recordings, so they are original and free
to use, change and ship with the game. Edit the script and re-run it rather than editing the
files. 16-bit mono WAV, 44.1 kHz. Played by `game/scripts/sfx.gd`.

| File | When |
|---|---|
| `ui_click.wav` | any button pressed |
| `ui_toggle.wav` | a toggle button (Build), and the sound on / off button |
| `crate_drop.wav` | a new crate arrives at an intake |
| `machine_start.wav` | a machine starts work on a crate |
| `machine_done.wav` | a machine finishes a crate |
| `machine_fail.wav` | a machine fails a crate |
| `attention.wav` | a crate waits for you at a gate, or is held at a machine |
| `pr_opened.wav` | a pull request is opened for a crate |
| `crate_ship.wav` | a crate reaches the shipping dock |
| `crate_scrap.wav` | a crate lands in the scrap bin |
| `machine_hum_loop.wav` | loop: louder the more machines are working; silent when none are |
| `belt_loop.wav` | loop: louder the more crates ride the belts; silent when none move or the factory is paused |

The two loops carry their loop points in a `smpl` chunk, so Godot's default import (Loop Mode:
Detect From WAV) loops them without a gap. Sounds play on an `SFX` bus that `sfx.gd` adds.
Press **M**, or the speaker button in the top bar, to turn sound off and on; the choice is
saved in `user://settings.cfg`.
