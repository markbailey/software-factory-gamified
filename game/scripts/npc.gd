extends Node2D
## One person on the factory floor (see npc_layer.gd for who comes when, and why).
##
## It walks a list of world points, turning to face the way it goes (the sprites face east),
## then plays a loop ("work", "wait", "repair", "sweep", "idle") facing a given way. A worker
## can also carry a crate: it keeps just behind the crate as the crate slides along, arms out.
## Sprites and SpriteFrames: res://assets/npcs/ (made by tools/gen_npcs.py).

signal arrived(npc)
signal carried(npc)  # a carry() is over: the crate is set down

const WALK_SPEED := 160.0  # px per second
const RUN_SPEED := 330.0
const TURN_SPEED := 12.0  # how quickly it turns to a new heading (radians per second, eased)
const FADE_SECONDS := 0.35
const CARRY_GAP := 26.0  # from the person's centre to the carried crate's centre
const CARRY_CATCH_UP := 700.0  # px per second while getting into place behind the crate
## Now and then a person standing idle looks around, stretches or (a worker) sips a coffee:
## one pass of an "idle_*" loop, if its SpriteFrames has it, then back to "idle".
const FIDGETS := ["idle_look", "idle_stretch", "idle_sip"]
const FIDGET_EVERY := Vector2(5.0, 14.0)  # seconds between fidgets (random in this range)

var kind := ""  # "worker", "mechanic" or "janitor"
var key := ""  # what it is here for (a job, a gate, a repair); set by the layer
var sprite: AnimatedSprite2D
var band: Sprite2D  # the worker's hard hat band, tinted with the machine's accent

var _path: Array = []  # world points still to walk to
var _speed := WALK_SPEED
var _loop := "idle"  # what to play once the path is walked
var _face := 0.0  # heading (radians) to turn to once there
var _heading := 0.0
var _cheer := 0.0  # seconds of cheering left
var _leaving := false
var _fade := 0.0  # 0 = invisible, 1 = shown
var _carry_left := 0.0  # seconds of carrying left
var _carry_seconds := 0.0
var _carry_crate: Node2D  # the crate followed while carrying (or null: the line below)
var _carry_from := Vector2.ZERO  # in this node's parent's space
var _carry_to := Vector2.ZERO
var _carry_dir := Vector2.RIGHT  # the way the crate goes
var _carry_last := Vector2.ZERO  # where the crate was last frame
var _fidget_in := 0.0  # seconds until the next fidget while idle
var _fidget_left := 0.0  # seconds left of the fidget playing now


func setup(npc_kind: String, frames: SpriteFrames, band_texture: Texture2D = null) -> void:
	kind = npc_kind
	sprite = AnimatedSprite2D.new()
	sprite.sprite_frames = frames
	add_child(sprite)
	if band_texture:
		band = Sprite2D.new()
		band.texture = band_texture
		add_child(band)
	modulate.a = 0.0
	_play("idle")
	_fidget_in = randf_range(FIDGET_EVERY.x, FIDGET_EVERY.y)


## Tint for the hard hat band (the accent of the machine this worker is at).
func set_band(color: Color) -> void:
	if band:
		band.modulate = color


## Walk through `points` (world positions), then play `loop` facing `face` radians.
func walk(points: Array, loop: String, face: float, run := false) -> void:
	_path = points.duplicate()
	_loop = loop
	_face = face
	_speed = RUN_SPEED if run else WALK_SPEED
	if _path.is_empty():
		_arrive()


## Change what it does where it stands (or will do once it gets there).
func set_loop(loop: String, face := NAN) -> void:
	_loop = loop
	_fidget_left = 0.0
	if not is_nan(face):
		_face = face
	if _path.is_empty() and _cheer <= 0.0 and _carry_left <= 0.0:
		_play(_loop)


## Late for the job: run the rest of the way.
func hurry() -> void:
	if not _path.is_empty() and not _leaving:
		_speed = RUN_SPEED


func cheer(seconds: float) -> void:
	if _leaving or _carry_left > 0.0 or not sprite.sprite_frames.has_animation("cheer"):
		return
	_cheer = seconds
	_play("cheer")


## Carry a crate from `from` to `to` (parent-space points) over `seconds`, staying just behind
## it: follows `crate` when given (the crate moves itself), else the straight line between.
## Emits `carried` at the end; what to do next is up to the caller (walk() or leave()).
func carry(crate: Node2D, from: Vector2, to: Vector2, seconds: float) -> void:
	_path.clear()
	_cheer = 0.0
	_carry_crate = crate
	_carry_from = from
	_carry_to = to
	_carry_seconds = maxf(seconds, 0.05)
	_carry_left = _carry_seconds
	_carry_dir = (to - from).normalized() if to != from else Vector2.from_angle(_heading)
	_carry_last = from
	if is_instance_valid(crate) and crate.is_inside_tree() and get_parent():
		_carry_last = get_parent().to_local(crate.global_position)
	_play("carry")


func is_carrying() -> bool:
	return _carry_left > 0.0


## Walk out along `points` and disappear at the end.
func leave(points: Array) -> void:
	_leaving = true
	_cheer = 0.0
	_carry_left = 0.0
	walk(points, "idle", 0.0)


func is_leaving() -> bool:
	return _leaving


func is_walking() -> bool:
	return not _path.is_empty()


func current_loop() -> String:
	return _loop


func _process(delta: float) -> void:
	var target_alpha := 0.0 if (_leaving and _path.is_empty()) else 1.0
	_fade = move_toward(_fade, target_alpha, delta / FADE_SECONDS)
	modulate.a = _fade
	if _leaving and _path.is_empty() and _fade <= 0.0:
		queue_free()
		return

	if _carry_left > 0.0:
		_carry_step(delta)
		return

	if _cheer > 0.0:
		_cheer -= delta
		if _cheer <= 0.0:
			_play(_loop if _path.is_empty() else ("run" if _speed > WALK_SPEED else "walk"))
		return

	if not _path.is_empty():
		var goal: Vector2 = _path[0]
		var to := goal - position
		var step := _speed * delta
		if to.length() <= step:
			position = goal
			_path.pop_front()
			if _path.is_empty():
				_arrive()
				return
		else:
			position += to.normalized() * step
			_turn_to(to.angle(), delta)
		_play("run" if _speed > WALK_SPEED and sprite.sprite_frames.has_animation("run") else "walk")
	else:
		_turn_to(_face, delta)
		_fidget_step(delta)


## Idle people don't stand like statues: every few seconds, one short fidget.
func _fidget_step(delta: float) -> void:
	if _loop != "idle" or _leaving:
		_fidget_left = 0.0
		return
	if _fidget_left > 0.0:
		_fidget_left -= delta
		if _fidget_left <= 0.0:
			_play("idle")
		return
	_fidget_in -= delta
	if _fidget_in > 0.0:
		return
	_fidget_in = randf_range(FIDGET_EVERY.x, FIDGET_EVERY.y)
	var options: Array = []
	for anim in FIDGETS:
		if sprite.sprite_frames.has_animation(anim):
			options.append(anim)
	if options.is_empty():
		return
	var pick: String = options.pick_random()
	var fps := sprite.sprite_frames.get_animation_speed(pick)
	_fidget_left = sprite.sprite_frames.get_frame_count(pick) / maxf(fps, 0.1)
	sprite.play(pick)


func _carry_step(delta: float) -> void:
	_carry_left -= delta
	var at := _carry_from.lerp(_carry_to, clampf(1.0 - _carry_left / _carry_seconds, 0.0, 1.0))
	if is_instance_valid(_carry_crate) and _carry_crate.is_inside_tree() and get_parent():
		at = get_parent().to_local(_carry_crate.global_position)
		# Head the way the crate actually goes.
		if at.distance_to(_carry_last) > 0.5:
			_carry_dir = (at - _carry_last).normalized()
	_carry_last = at
	position = position.move_toward(at - _carry_dir * CARRY_GAP, CARRY_CATCH_UP * delta)
	_turn_to(_carry_dir.angle(), delta)
	_play("carry")
	if _carry_left <= 0.0:
		_carry_left = 0.0
		_carry_crate = null
		_play(_loop)
		carried.emit(self)


func _arrive() -> void:
	if not _leaving:
		_play(_loop)
	arrived.emit(self)


func _turn_to(angle: float, delta: float) -> void:
	_heading = lerp_angle(_heading, angle, clampf(TURN_SPEED * delta, 0.0, 1.0))
	rotation = _heading


func _play(anim: String) -> void:
	if not sprite.sprite_frames.has_animation(anim):
		anim = "idle"
	if sprite.animation != anim or not sprite.is_playing():
		sprite.play(anim)
