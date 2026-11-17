extends Node
## Sound for the factory floor: one-shots for what happens (a crate arrives, a machine starts,
## finishes or fails, a crate ships or is scrapped, a PR opens, a crate waits for you, button
## clicks) and two loops that follow what is going on (machines humming, belts rolling).
##
## It only listens: main.gd adds it and passes it the backend's messages (on_message), and it
## reads main.gd's crates and pauses for the loops. Nothing here changes the game.
## Sounds: res://assets/sfx/ (made by tools/gen_sfx.py, original and free to use).
## M (or the speaker button in the top bar) turns sound on and off; the choice is remembered.

const SFX_DIR := "res://assets/sfx/"
const BUS := "SFX"
const SETTINGS := "user://settings.cfg"
const VOICES := 10  # one-shots that can play at once
const MIN_GAP := 0.08  # seconds before the same one-shot may play again
const SILENT_DB := -60.0
const FADE_DB_PER_SECOND := 40.0
const VOLUME := {  # dB for each one-shot, so they sit together
	"ui_click": -10.0, "ui_toggle": -9.0, "crate_drop": -8.0, "crate_ship": -6.0, "crate_scrap": -6.0,
	"machine_start": -12.0, "machine_done": -12.0, "machine_fail": -8.0, "attention": -7.0,
	"pr_opened": -8.0,
}

var main: Node  # main.gd: crates (item_id -> Crate), pauses, stats_button
var muted := false

var _streams := {}  # name -> AudioStream (or null when the file isn't there)
var _voices: Array[AudioStreamPlayer] = []
var _last_played := {}  # name -> seconds
var _hum: AudioStreamPlayer
var _belt: AudioStreamPlayer
var _running := {}  # machine id -> true while it works on a crate
var _last_seq := 0
var _button: Button


func _ready() -> void:
	if AudioServer.get_bus_index(BUS) == -1:
		AudioServer.add_bus()
		AudioServer.set_bus_name(AudioServer.bus_count - 1, BUS)
		AudioServer.set_bus_send(AudioServer.bus_count - 1, "Master")
	for i in VOICES:
		var p := AudioStreamPlayer.new()
		p.bus = BUS
		add_child(p)
		_voices.append(p)
	_hum = _loop_player("machine_hum_loop")
	_belt = _loop_player("belt_loop")
	_load_settings()
	_add_button()
	# Click sounds for every button, now and later.
	for b in get_tree().root.find_children("*", "BaseButton", true, false):
		_hook_button(b)
	get_tree().node_added.connect(func(n: Node) -> void:
		if n is BaseButton:
			_hook_button(n))


# Backend events -------------------------------------------------------------

func on_message(msg: Dictionary) -> void:
	var kind := str(msg.get("type", ""))
	if kind == "snapshot":
		# A fresh picture of the factory (connect, reconnect, a saved layout): no sounds for it.
		_last_seq = int(msg.get("seq", 0))
		_running.clear()
		for item in msg.get("items", []):
			if str(item.get("state", "")) == "running":
				_running[str(item.get("location", ""))] = true
		return
	if not msg.has("seq"):
		return
	var seq := int(msg.get("seq", 0))
	if seq <= _last_seq:
		return
	_last_seq = seq
	var machine := str(msg.get("machine", ""))
	match kind:
		"item_created":
			play("crate_drop")
		"job_started":
			_running[machine] = true
			play("machine_start")
		"job_finished":
			_running.erase(machine)
			play("machine_done")
		"job_failed":
			_running.erase(machine)
			play("machine_fail")
		"item_held":
			play("attention")
		"pr_opened":
			play("pr_opened")
		"item_moved":
			_on_moved(msg)


func _on_moved(msg: Dictionary) -> void:
	var duration := float(msg.get("duration", 0.0))
	match str(msg.get("state", "")):
		"shipped":
			play_later("crate_ship", duration)  # as it reaches the dock
		"scrapped":
			play_later("crate_scrap", duration)  # as it lands in the bin
		"awaiting_approval":
			play_later("attention", duration)


# Playing --------------------------------------------------------------------

func play(sound: String, volume_db := NAN) -> void:
	if muted:
		return
	var stream := _stream(sound)
	if stream == null:
		return
	var now := Time.get_ticks_msec() / 1000.0
	if now - float(_last_played.get(sound, -10.0)) < MIN_GAP:
		return
	_last_played[sound] = now
	var voice: AudioStreamPlayer = null
	for v in _voices:
		if not v.playing:
			voice = v
			break
	if voice == null:
		return  # busy enough already
	voice.stream = stream
	voice.volume_db = volume_db if not is_nan(volume_db) else float(VOLUME.get(sound, -10.0))
	voice.pitch_scale = randf_range(0.96, 1.04)  # so repeats don't sound copy-pasted
	voice.play()


func play_later(sound: String, seconds: float) -> void:
	if seconds <= 0.05:
		play(sound)
		return
	get_tree().create_timer(seconds).timeout.connect(play.bind(sound, NAN))


func set_muted(value: bool) -> void:
	muted = value
	AudioServer.set_bus_mute(AudioServer.get_bus_index(BUS), muted)
	if _button:
		_button.icon = _icon("icon_sound_off" if muted else "icon_sound_on")
		_button.tooltip_text = ("Sound is off: turn it on (M)" if muted else "Sound is on: turn it off (M)")
		_button.modulate = Color("5AB0FF") if muted else Color.WHITE
	var cfg := ConfigFile.new()
	cfg.load(SETTINGS)  # keep whatever else is in there
	cfg.set_value("audio", "muted", muted)
	cfg.save(SETTINGS)


## The loops follow the floor: the hum with the number of machines at work, the belts with
## the number of crates riding them. Both fade out while the factory is paused.
func _process(delta: float) -> void:
	var paused := false
	var moving := 0
	if main:
		var p = main.get("pauses")
		paused = p is Dictionary and bool(p.get("factory", false))
		var crates = main.get("crates")
		if crates is Dictionary:
			for crate in crates.values():
				if is_instance_valid(crate) and crate.is_moving():
					moving += 1
	var working := _running.size()
	_fade(_hum, SILENT_DB if working == 0 else lerpf(-26.0, -16.0, minf(working, 5) / 5.0), delta)
	_fade(_belt, SILENT_DB if moving == 0 or paused else lerpf(-28.0, -17.0, minf(moving, 6) / 6.0), delta)


func _fade(player: AudioStreamPlayer, target_db: float, delta: float) -> void:
	if player == null or player.stream == null:
		return
	player.volume_db = move_toward(player.volume_db, target_db, FADE_DB_PER_SECOND * delta)
	var audible := player.volume_db > SILENT_DB + 1.0
	if audible and not player.playing:
		player.play()
	elif not audible and player.playing:
		player.stop()


func _unhandled_input(event: InputEvent) -> void:
	var key := event as InputEventKey
	if key == null or not key.pressed or key.echo or key.keycode != KEY_M:
		return
	if key.is_command_or_control_pressed() or key.alt_pressed:
		return
	var focus := get_viewport().gui_get_focus_owner()
	if focus is LineEdit or focus is TextEdit:
		return
	set_muted(not muted)
	play("ui_toggle")
	get_viewport().set_input_as_handled()


# Setup ----------------------------------------------------------------------

func _stream(sound: String) -> AudioStream:
	if not _streams.has(sound):
		var path := SFX_DIR + sound + ".wav"
		_streams[sound] = load(path) if ResourceLoader.exists(path) else null
	return _streams[sound]


func _loop_player(sound: String) -> AudioStreamPlayer:
	var p := AudioStreamPlayer.new()
	p.bus = BUS
	p.volume_db = SILENT_DB
	var stream := _stream(sound)
	if stream is AudioStreamWAV:
		# The file carries its loop points; set them here too, in case it was imported without.
		var wav := stream.duplicate() as AudioStreamWAV
		if wav.loop_mode == AudioStreamWAV.LOOP_DISABLED:
			wav.loop_mode = AudioStreamWAV.LOOP_FORWARD
			wav.loop_begin = 0
			wav.loop_end = int(wav.get_length() * wav.mix_rate)
		stream = wav
	p.stream = stream
	add_child(p)
	return p


func _load_settings() -> void:
	var cfg := ConfigFile.new()
	var on := cfg.load(SETTINGS) == OK and bool(cfg.get_value("audio", "muted", false))
	muted = on
	AudioServer.set_bus_mute(AudioServer.get_bus_index(BUS), muted)


## A speaker button in the top bar, next to the stats button.
func _add_button() -> void:
	var next_to = main.get("stats_button") if main else null
	if not (next_to is Control) or next_to.get_parent() == null:
		return
	_button = Button.new()
	_button.theme_type_variation = &"ButtonGhost"
	_button.focus_mode = Control.FOCUS_NONE
	_button.set_meta("sfx_toggle", true)  # plays its own sound, not the click
	_button.pressed.connect(func() -> void:
		set_muted(not muted)
		play("ui_toggle"))
	var row: Node = next_to.get_parent()
	row.add_child(_button)
	row.move_child(_button, next_to.get_index() + 1)
	set_muted(muted)


func _hook_button(b: BaseButton) -> void:
	if b.has_meta("sfx_toggle") or b.has_meta("sfx_hooked"):
		return
	b.set_meta("sfx_hooked", true)
	b.pressed.connect(_on_button_pressed.bind(b))


func _on_button_pressed(b: BaseButton) -> void:
	if is_instance_valid(b) and not b.has_meta("sfx_toggle"):
		play("ui_toggle" if b.toggle_mode else "ui_click")


func _icon(name: String) -> Texture2D:
	var path := "res://assets/ui/%s.svg" % name
	return load(path) if ResourceLoader.exists(path) else null
