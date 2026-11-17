extends Node2D
## One work item. It only moves when the backend says so.

const SIZE := 32.0
const AMBER := Color("F2B84C")
const STAMP := 14.0  # size of the risk stamp in the bottom-left corner
const RISK_COLORS := {"low": Color("5ABF66"), "medium": Color("F2B84C"), "high": Color("E5484D")}
const RISK_WORDS := {"low": "Low", "medium": "Medium", "high": "High"}

var item_id := 0
var text := ""
var source := ""  # e.g. "GitHub #12", empty when typed in the game
var has_pr := false:  # a pull request is open for it: a small PR badge in the top-right corner
	set(value):
		has_pr = value
		queue_redraw()
var state := "queued"
var location := "intake"
var loops := 0  # times sent back for rework
var risk := ""  # "low", "medium" or "high" once a Risk analyser has stamped it
var risk_reason := ""
var waiting_for := ""  # what a step is waiting on outside, e.g. "Waiting for an approving review on GitHub"
var scrap_kind := ""  # why it was scrapped: needs_info, already_done, blocked, work_failed, rejected
var came_back := false  # riding the rework rise back into a machine
var selected := false:
	set(value):
		selected = value
		queue_redraw()

var _tween: Tween
# Moves that arrived while the crate was still riding a belt: done in turn, so it never cuts a
# corner off the belts ([waypoints, duration] each).
var _queued: Array = []
static var _textures := {}


func set_risk(level: String, reason: String) -> void:
	risk = level if RISK_COLORS.has(level) else ""
	risk_reason = reason
	queue_redraw()


func risk_words() -> String:
	return str(RISK_WORDS.get(risk, ""))


## Crates that have arrived somewhere they wait (a gate, the dock, the bin) leave the floor:
## the machine or station shows a count, and its window lists them.
const OFF_FLOOR := ["scrapped", "shipped", "awaiting_approval", "held"]


func set_state(new_state: String) -> void:
	state = new_state
	if not state in OFF_FLOOR:
		visible = true  # on its way again (approved, retried)
	queue_redraw()


## Fade out where it is (it went into the scrap bin, which no belt leads to).
func vanish() -> void:
	_queued.clear()
	if _tween and _tween.is_valid():
		_tween.kill()
	var fade := create_tween()
	fade.tween_property(self, "modulate:a", 0.0, 0.35)
	fade.finished.connect(func() -> void:
		modulate.a = 1.0
		_settle())


## Scrapped crates aren't drawn on the floor: they ride into the bin, then vanish into it.
func _settle() -> void:
	visible = not state in OFF_FLOOR


func move_to(target: Vector2, duration: float) -> void:
	_queued.clear()
	if _tween and _tween.is_valid():
		_tween.kill()
	if duration <= 0.0:
		position = target
		_settle()
		return
	_tween = create_tween()
	_tween.tween_property(self, "position", target, duration)
	_tween.finished.connect(_settle)


## Move through waypoints, splitting the time by distance (so speed stays even).
func move_path(waypoints: Array, duration: float) -> void:
	if is_moving() and duration > 0.0 and not waypoints.is_empty():
		# Finish this belt first; catch up a little faster if moves pile up.
		_queued.append([waypoints, duration * (0.5 if _queued.size() >= 2 else 1.0)])
		return
	_queued.clear()
	if _tween and _tween.is_valid():
		_tween.kill()
	if duration <= 0.0 or waypoints.is_empty():
		if not waypoints.is_empty():
			position = waypoints[-1]
		_settle()
		return
	var total := 0.0
	var from := position
	for p in waypoints:
		total += from.distance_to(p)
		from = p
	_tween = create_tween()
	from = position
	for p in waypoints:
		var share := from.distance_to(p) / total if total > 0.0 else 1.0 / waypoints.size()
		_tween.tween_property(self, "position", p, duration * share)
		from = p
	_tween.finished.connect(_next_move)


func _next_move() -> void:
	if _queued.is_empty():
		_settle()
		return
	var next: Array = _queued.pop_front()
	_tween = null
	var rest := _queued.duplicate()
	_queued.clear()
	move_path(next[0], next[1])
	_queued = rest


func is_moving() -> bool:
	return _tween != null and _tween.is_valid() and _tween.is_running()


func hit(world_point: Vector2) -> bool:
	if not visible:
		return false
	var half := Vector2(SIZE, SIZE) * 0.6
	return Rect2(global_position - half, half * 2.0).has_point(world_point)


func _sprite_name() -> String:
	match state:
		"shipped":
			return "crate_shipped"
		"scrapped":
			return "crate_scrapped"
	match kind():
		"pr":
			return "crate_pr"
		"issue":
			return "crate_issue"
	return "crate"


## What it came in as: "pr" (a pull request), "issue" (GitHub / Jira issue, a PRD) or "spec"
## (typed in the game). Each has its own look: plain crate, blue issue tag, purple PR tag.
func kind() -> String:
	if source.is_empty():
		return "spec"
	return "pr" if source.begins_with("PR #") else "issue"


func _texture(sprite: String) -> Texture2D:
	var path := "res://assets/crates/%s.svg" % sprite
	if not _textures.has(path):
		_textures[path] = load(path) if ResourceLoader.exists(path) else null
	return _textures[path]


func _draw() -> void:
	var r := Rect2(-SIZE / 2.0, -SIZE / 2.0, SIZE, SIZE)
	var tex := _texture(_sprite_name())
	if tex:
		draw_texture_rect(tex, r, false)
	else:
		draw_rect(r, Color("C9975B"))
		draw_rect(r, Color("16181C"), false, 2.0)
	if not risk.is_empty():
		_draw_stamp(Rect2(-SIZE / 2.0 + 2.0, 0.0, STAMP, STAMP))  # bottom-left, as in assets/README.md
	if has_pr and kind() != "pr" and not state in ["shipped", "scrapped"]:
		var badge := _texture("badge_pr")  # top-right, the corner the issue tag leaves free
		if badge:
			draw_texture_rect(badge, Rect2(SIZE / 2.0 - 13.0, -SIZE / 2.0 + 1.0, 12.0, 12.0), false)
	if state == "awaiting_approval":
		draw_rect(r.grow(3.0), AMBER, false, 2.0)
	elif state == "held":
		draw_rect(r.grow(3.0), Color("E5484D"), false, 2.0)
	if selected:
		draw_rect(r.grow(6.0), Color.WHITE, false, 2.0)
	draw_string(ThemeDB.fallback_font, Vector2(-30, SIZE / 2.0 + 13), "#%d" % item_id,
			HORIZONTAL_ALIGNMENT_CENTER, 60, 11, Color(0.85, 0.85, 0.85))


## The risk stamp: the art's badge if there is one, else a coloured disc with a mark.
func _draw_stamp(r: Rect2) -> void:
	var tex := _texture("risk_" + risk)
	if tex:
		draw_texture_rect(tex, r, false)
		return
	var c := r.get_center()
	draw_circle(c, r.size.x / 2.0 + 1.0, Color("16181C"))
	draw_circle(c, r.size.x / 2.0 - 0.5, RISK_COLORS[risk])
	var bars := {"low": 1, "medium": 2, "high": 3}[risk] as int
	for i in bars:  # one, two or three bars, so it reads without colour too
		var x := c.x - (bars - 1) * 2.0 + i * 4.0
		draw_line(Vector2(x, c.y - 3.0), Vector2(x, c.y + 3.0), Color("16181C"), 2.0)
