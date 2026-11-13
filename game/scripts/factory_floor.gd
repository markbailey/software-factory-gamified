extends Node2D
## Draws the factory floor on a square grid from the backend's layout: an intake,
## then a belt and a machine for each machine in the line, a last belt, the
## shipping dock, and one scrap bin that every machine can reject into.
## It knows where each location is, and nothing about the backend.
## A later isometric view can replace this script alone.

const CELL := 64
const ROW := 3
const SCRAP_ROW := 6
const ROWS := 9
const STRIDE := 5  # cells per belt + machine pair

const COLOR_FLOOR := Color(0.16, 0.17, 0.19)
const COLOR_TEXT := Color(0.92, 0.92, 0.92)
const COLOR_QUIET := Color(0.65, 0.66, 0.7)

var cols := 12
var points := {}  # location name -> Vector2i cell
var belts: Array = []  # [first_col, last_col] for each belt
var machine_order: Array = []  # machine ids, in line order
var machines := {}  # id -> {name, steps, state, progress, step}


## machines_layout: Array of {id, name, steps[]} from the backend snapshot.
func set_layout(machines_layout: Array) -> void:
	points.clear()
	belts.clear()
	machine_order.clear()
	machines.clear()
	points["intake"] = Vector2i(1, ROW)
	var c := 2
	for i in machines_layout.size():
		var m: Dictionary = machines_layout[i]
		var id := str(m.get("id", "m%d" % i))
		belts.append([c, c + 3])
		points["belt_%d" % i] = Vector2i(c + 2, ROW)
		points[id] = Vector2i(c + 4, ROW)
		machine_order.append(id)
		var steps: Array = m.get("steps", [])
		machines[id] = {
			"name": str(m.get("name", id)),
			"steps": steps.size(),
			"state": "idle",
			"progress": 0.0,
			"step": "",
		}
		c += STRIDE
	belts.append([c, c + 3])
	points["belt_%d" % machines_layout.size()] = Vector2i(c + 2, ROW)
	points["dock"] = Vector2i(c + 4, ROW)
	cols = c + 6
	var middle := (1 + c + 4) / 2
	points["scrap"] = Vector2i(middle, SCRAP_ROW)
	queue_redraw()


func cell_center(c: Vector2i) -> Vector2:
	return Vector2((c.x + 0.5) * CELL, (c.y + 0.5) * CELL)


func point(location: String) -> Vector2:
	var c: Vector2i = points.get(location, points.get("intake", Vector2i(1, ROW)))
	return cell_center(c)


func world_size() -> Vector2:
	return Vector2(cols * CELL, ROWS * CELL)


func set_machine_state(id: String, state: String, progress: float, step := "") -> void:
	if not machines.has(id):
		return
	var m: Dictionary = machines[id]
	m["state"] = state
	m["progress"] = clampf(progress, 0.0, 1.0)
	if not step.is_empty() or state != "running":
		m["step"] = step
	queue_redraw()


func machine_progress(id: String) -> float:
	if not machines.has(id):
		return 0.0
	return float(machines[id]["progress"])


func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, world_size()), COLOR_FLOOR)
	var grid := Color(1, 1, 1, 0.04)
	for x in range(cols + 1):
		draw_line(Vector2(x * CELL, 0), Vector2(x * CELL, ROWS * CELL), grid)
	for y in range(ROWS + 1):
		draw_line(Vector2(0, y * CELL), Vector2(cols * CELL, y * CELL), grid)

	for b in belts:
		_draw_belt(int(b[0]), int(b[1]))
	var scrap := point("scrap")
	for id in machine_order:
		draw_dashed_line(point(id) + Vector2(0, 62), scrap - Vector2(0, 44),
				Color(0.85, 0.3, 0.3, 0.45), 2.0, 8.0)

	_draw_station(point("intake"), "Intake", Color(0.35, 0.55, 0.85))
	_draw_station(point("dock"), "Shipping dock", Color(0.35, 0.7, 0.4))
	_draw_station(scrap, "Scrap bin", Color(0.8, 0.3, 0.3))
	for id in machine_order:
		_draw_machine(id)


func _draw_belt(x0: int, x1: int) -> void:
	var r := Rect2(x0 * CELL, ROW * CELL + 14, (x1 - x0 + 1) * CELL, CELL - 28)
	draw_rect(r, Color(0.27, 0.28, 0.3))
	draw_rect(r, Color(0.45, 0.46, 0.48), false, 2.0)
	for x in range(x0, x1 + 1):
		var c := cell_center(Vector2i(x, ROW))
		draw_polyline(PackedVector2Array([c + Vector2(-6, -8), c + Vector2(4, 0), c + Vector2(-6, 8)]),
				Color(0.6, 0.6, 0.62), 2.0)


func _draw_station(p: Vector2, label: String, col: Color) -> void:
	var r := Rect2(p - Vector2(40, 40), Vector2(80, 80))
	draw_rect(r, col.darkened(0.6))
	draw_rect(r, col, false, 3.0)
	_label(p + Vector2(0, -50), label, 14, COLOR_TEXT)


func _draw_machine(id: String) -> void:
	var m: Dictionary = machines[id]
	var p := point(id)
	var state := str(m["state"])
	var col := Color(0.5, 0.52, 0.56)
	match state:
		"running":
			col = Color(0.3, 0.55, 0.95)
		"done":
			col = Color(0.35, 0.75, 0.4)
		"failed":
			col = Color(0.9, 0.3, 0.3)
	var r := Rect2(p - Vector2(58, 58), Vector2(116, 116))
	draw_rect(r, Color(0.21, 0.22, 0.25))
	draw_rect(r, col, false, 4.0)
	draw_rect(Rect2(p - Vector2(20, 20), Vector2(40, 40)), Color(0.11, 0.11, 0.13))

	_label(p + Vector2(0, -86), str(m["name"]), 16, COLOR_TEXT)
	_label(p + Vector2(0, -68), "%d steps" % int(m["steps"]), 11, COLOR_QUIET)
	_label(p + Vector2(0, -30), state.capitalize(), 12, col)
	var step := str(m["step"])
	if not step.is_empty():
		_label(p + Vector2(0, 36), step, 11, COLOR_TEXT, 112.0)

	var bar := Rect2(p.x - 46, p.y + 42, 92, 8)
	draw_rect(bar, Color(0, 0, 0, 0.5))
	draw_rect(Rect2(bar.position, Vector2(bar.size.x * float(m["progress"]), bar.size.y)), col)


func _label(p: Vector2, text: String, size: int, col: Color, width := 240.0) -> void:
	draw_string(ThemeDB.fallback_font, Vector2(p.x - width / 2.0, p.y), text,
			HORIZONTAL_ALIGNMENT_CENTER, width, size, col)
