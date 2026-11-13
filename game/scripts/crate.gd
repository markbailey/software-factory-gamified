extends Node2D
## One work item. It only moves when the backend says so.

const SIZE := 32.0
const AMBER := Color("F2B84C")

var item_id := 0
var text := ""
var source := ""  # e.g. "GitHub #12", empty when typed in the game
var state := "queued"
var location := "intake"
var selected := false:
	set(value):
		selected = value
		queue_redraw()

var _tween: Tween
static var _textures := {}


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


func _sprite_name() -> String:
	match state:
		"shipped":
			return "crate_shipped"
		"scrapped":
			return "crate_scrapped"
	return "crate" if source.is_empty() else "crate_issue"


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
	if state == "awaiting_approval":
		draw_rect(r.grow(3.0), AMBER, false, 2.0)
	if selected:
		draw_rect(r.grow(6.0), Color.WHITE, false, 2.0)
	draw_string(ThemeDB.fallback_font, Vector2(-30, SIZE / 2.0 + 13), "#%d" % item_id,
			HORIZONTAL_ALIGNMENT_CENTER, 60, 11, Color(0.85, 0.85, 0.85))
