extends Node2D
## One work item. It only moves when the backend says so.

const SIZE := 26.0

var item_id := 0
var text := ""
var source := ""  # e.g. "GitHub #12", empty when typed in the game
var state := "queued"
var selected := false:
	set(value):
		selected = value
		queue_redraw()

var _tween: Tween


func set_state(new_state: String) -> void:
	state = new_state
	queue_redraw()


func move_to(target: Vector2, duration: float) -> void:
	if _tween and _tween.is_valid():
		_tween.kill()
	if duration <= 0.0:
		position = target
		return
	_tween = create_tween()
	_tween.tween_property(self, "position", target, duration)


func hit(world_point: Vector2) -> bool:
	var half := Vector2(SIZE, SIZE) * 0.6
	return Rect2(global_position - half, half * 2.0).has_point(world_point)


func _draw() -> void:
	var col := Color(0.82, 0.62, 0.36)
	match state:
		"scrapped":
			col = Color(0.55, 0.3, 0.3)
		"shipped":
			col = Color(0.45, 0.72, 0.45)
	var r := Rect2(-SIZE / 2.0, -SIZE / 2.0, SIZE, SIZE)
	draw_rect(r, col)
	draw_rect(r, col.darkened(0.6), false, 2.0)
	draw_line(r.position, r.end, col.darkened(0.4), 1.5)
	draw_line(Vector2(r.end.x, r.position.y), Vector2(r.position.x, r.end.y), col.darkened(0.4), 1.5)
	if selected:
		draw_rect(r.grow(4.0), Color.WHITE, false, 2.0)
	draw_string(ThemeDB.fallback_font, Vector2(-30, SIZE / 2.0 + 14), "#%d" % item_id,
			HORIZONTAL_ALIGNMENT_CENTER, 60, 11, Color(0.85, 0.85, 0.85))
	if not source.is_empty():
		# A small tag marks crates that came from an issue tracker.
		draw_circle(Vector2(SIZE / 2.0, -SIZE / 2.0), 5.0, Color(0.55, 0.75, 1.0))
