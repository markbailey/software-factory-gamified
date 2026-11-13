extends Node2D
## Draws the factory floor from the backend's layout using the sprites in res://assets:
## an intake, then a belt and a machine for each machine in the line, a last belt, the
## shipping dock, and one scrap bin that every machine can reject into.
##
## This node draws the floor and belts. Machines are drawn on a second canvas
## (attach_machine_layer) that sits above the crates, so a crate inside a machine is hidden.
## It knows where each location is, and nothing about the backend.

const CELL := 64
const ROW := 3  # belt row; machines take rows ROW and ROW + 1
const SCRAP_ROW := 6
const ROWS := 9
const STRIDE := 4  # cells per belt (2) + machine (2)
const LAMP := Vector2(110, 14)

const COLOR_TEXT := Color(0.92, 0.92, 0.92)
const COLOR_QUIET := Color(0.65, 0.66, 0.7)
const COLOR_AMBER := Color("F2B84C")
const STATE_COLORS := {
	"idle": Color("80858F"),
	"running": Color("4C8DF2"),
	"done": Color("5ABF66"),
	"failed": Color("E5484D"),
}
const OVERLAYS := {"running": "overlay_running", "done": "overlay_done", "failed": "overlay_failed"}

var cols := 12
var points := {}  # location name -> Vector2 (world position where crates sit)
var footprints := {}  # machine or station id -> Vector2i top-left cell of its 2x2 footprint
var belts: Array = []  # [first_col, last_col] for each belt
var machine_order: Array = []  # machine ids, in line order
var machines := {}  # id -> {name, sprite, steps, gate, state, progress, step, waiting}
var highlighted := ""  # machine id outlined in build mode

var _machine_layer: CanvasItem
var _textures := {}


## The canvas that machines are drawn on (add it above the crates).
func attach_machine_layer(layer: CanvasItem) -> void:
	_machine_layer = layer
	layer.draw.connect(_draw_machines)


## machines_layout: Array of {id, name, sprite, steps[], gate} in line order.
func set_layout(machines_layout: Array) -> void:
	points.clear()
	footprints.clear()
	belts.clear()
	machine_order.clear()
	var old := machines
	machines = {}

	footprints["intake"] = Vector2i(1, ROW)
	points["intake"] = _center(Vector2i(1, ROW))
	var c := 3
	for i in machines_layout.size():
		var m: Dictionary = machines_layout[i]
		var id := str(m.get("id", "m%d" % i))
		belts.append([c, c + 1])
		points["belt_%d" % i] = cell_center(Vector2i(c + 1, ROW))
		footprints[id] = Vector2i(c + 2, ROW)
		points[id] = _center(Vector2i(c + 2, ROW))
		machine_order.append(id)
		var steps: Array = m.get("steps", [])
		var previous: Dictionary = old.get(id, {})
		machines[id] = {
			"name": str(m.get("name", id)),
			"sprite": str(m.get("sprite", "coder")),
			"steps": steps.size(),
			"gate": bool(m.get("gate", false)),
			"state": previous.get("state", "idle"),
			"progress": previous.get("progress", 0.0),
			"step": previous.get("step", ""),
			"waiting": previous.get("waiting", 0),
		}
		c += STRIDE
	belts.append([c, c + 1])
	points["belt_%d" % machines_layout.size()] = cell_center(Vector2i(c + 1, ROW))
	footprints["dock"] = Vector2i(c + 2, ROW)
	# Shipped crates stack up just past the dock, scrapped ones beside the bin.
	points["dock"] = cell_center(Vector2i(c + 4, ROW + 1))
	cols = c + 6
	var middle := int((c + 3) / 2)
	footprints["scrap"] = Vector2i(middle, SCRAP_ROW)
	points["scrap"] = cell_center(Vector2i(middle + 2, SCRAP_ROW + 1))
	_redraw()


func cell_center(c: Vector2i) -> Vector2:
	return Vector2((c.x + 0.5) * CELL, (c.y + 0.5) * CELL)


func point(location: String) -> Vector2:
	return points.get(location, points.get("intake", Vector2.ZERO))


func world_size() -> Vector2:
	return Vector2(cols * CELL, ROWS * CELL)


## The machine id whose footprint contains a world point, or "".
func machine_at(world_point: Vector2) -> String:
	for id in machine_order:
		var r := Rect2(Vector2(footprints[id]) * CELL, Vector2(2, 2) * CELL)
		if r.has_point(world_point):
			return id
	return ""


func set_machine_state(id: String, state: String, progress: float, step := "") -> void:
	if not machines.has(id):
		return
	var m: Dictionary = machines[id]
	m["state"] = state
	m["progress"] = clampf(progress, 0.0, 1.0)
	if not step.is_empty() or state != "running":
		m["step"] = step
	_redraw()


## counts: machine id -> number of crates waiting there for approval.
func set_waiting(counts: Dictionary) -> void:
	for id in machines:
		machines[id]["waiting"] = int(counts.get(id, 0))
	_redraw()


func set_highlight(id: String) -> void:
	highlighted = id
	_redraw()


func machine_progress(id: String) -> float:
	if not machines.has(id):
		return 0.0
	return float(machines[id]["progress"])


func _redraw() -> void:
	queue_redraw()
	if _machine_layer:
		_machine_layer.queue_redraw()


func _center(top_left: Vector2i) -> Vector2:
	return (Vector2(top_left) + Vector2.ONE) * CELL


func _texture(path: String) -> Texture2D:
	if not _textures.has(path):
		_textures[path] = load(path) if ResourceLoader.exists(path) else null
	return _textures[path]


# Floor and belts -------------------------------------------------------------

func _draw() -> void:
	var tile := _texture("res://assets/floor/floor_plain.svg")
	for x in range(cols):
		for y in range(ROWS):
			var r := Rect2(Vector2(x, y) * CELL, Vector2(CELL, CELL))
			if tile:
				draw_texture_rect(tile, r, false)
			else:
				draw_rect(r, Color("292B30"))
	var hazard := _texture("res://assets/floor/floor_hazard.svg")
	if hazard and footprints.has("scrap"):
		var s: Vector2i = footprints["scrap"]
		for x in range(s.x - 1, s.x + 3):
			for y in [s.y - 1]:
				draw_texture_rect(hazard, Rect2(Vector2(x, y) * CELL, Vector2(CELL, CELL)), false)

	var belt := _texture("res://assets/belts/belt_straight_e.svg")
	for b in belts:
		for x in range(int(b[0]), int(b[1]) + 1):
			var r := Rect2(Vector2(x, ROW) * CELL, Vector2(CELL, CELL))
			if belt:
				draw_texture_rect(belt, r, false)
			else:
				draw_rect(r.grow(-14), Color("1C1F24"))


# Machines (drawn on the layer above the crates) ------------------------------

func _draw_machines() -> void:
	var canvas := _machine_layer
	_draw_station(canvas, "intake", "intake", "Intake")
	_draw_station(canvas, "dock", "shipping_dock", "Shipping dock")
	_draw_station(canvas, "scrap", "scrap_bin", "Scrap bin")
	for id in machine_order:
		_draw_machine(canvas, id)


func _draw_station(canvas: CanvasItem, id: String, sprite: String, label: String) -> void:
	if not footprints.has(id):
		return
	var origin := Vector2(footprints[id]) * CELL
	_draw_body(canvas, origin, sprite, Color("80858F"))
	_label(canvas, origin + Vector2(CELL, -8), label, 14, COLOR_TEXT)


func _draw_machine(canvas: CanvasItem, id: String) -> void:
	var m: Dictionary = machines[id]
	var origin := Vector2(footprints[id]) * CELL
	var state := str(m["state"])
	var col: Color = STATE_COLORS.get(state, STATE_COLORS["idle"])
	_draw_body(canvas, origin, str(m["sprite"]), col)
	if OVERLAYS.has(state):
		var overlay := _texture("res://assets/machines/%s.svg" % OVERLAYS[state])
		if overlay:
			canvas.draw_texture_rect(overlay, Rect2(origin, Vector2(128, 128)), false)
	if id == highlighted:
		canvas.draw_rect(Rect2(origin, Vector2(128, 128)).grow(4), COLOR_AMBER, false, 3.0)

	var top := origin + Vector2(CELL, -8)
	_label(canvas, top, str(m["name"]), 15, COLOR_TEXT)
	if bool(m.get("gate", false)):
		_label(canvas, top + Vector2(0, -18), "gate: waits for you", 11, COLOR_AMBER)
	var waiting := int(m.get("waiting", 0))
	if waiting > 0:
		_label(canvas, top + Vector2(0, -34), "%d waiting for you" % waiting, 13, COLOR_AMBER)

	var below := origin + Vector2(0, 2 * CELL + 6)
	var bar := Rect2(below + Vector2(16, 0), Vector2(96, 6))
	canvas.draw_rect(bar, Color(0, 0, 0, 0.5))
	canvas.draw_rect(Rect2(bar.position, Vector2(bar.size.x * float(m["progress"]), bar.size.y)), col)
	var step := str(m["step"])
	if not step.is_empty():
		_label(canvas, below + Vector2(CELL, 20), step, 11, COLOR_QUIET, 128.0)


func _draw_body(canvas: CanvasItem, origin: Vector2, sprite: String, lamp: Color) -> void:
	var tex := _texture("res://assets/machines/%s.svg" % sprite)
	var r := Rect2(origin, Vector2(128, 128))
	if tex:
		canvas.draw_texture_rect(tex, r, false)
	else:
		canvas.draw_rect(r.grow(-4), Color("454B56"))
		canvas.draw_rect(r.grow(-4), Color("16181C"), false, 2.0)
	canvas.draw_circle(origin + LAMP, 4.5, lamp)


func _label(canvas: CanvasItem, p: Vector2, text: String, size: int, col: Color, width := 240.0) -> void:
	canvas.draw_string(ThemeDB.fallback_font, Vector2(p.x - width / 2.0, p.y), text,
			HORIZONTAL_ALIGNMENT_CENTER, width, size, col)
