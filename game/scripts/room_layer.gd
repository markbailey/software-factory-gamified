extends Node2D
## Rooms: walls, windows, doors, belt ports and floor blocks you build in build mode.
##
## It is the look of the factory only (phase 1 of the rooms plan): nothing here reaches the
## backend. What a block does:
## - wall, window: stops machines, belts and people. A window is a wall you can see through.
## - door: people walk through it; machines and belts can't.
## - belt port: one belt goes straight through it, across the wall; people and machines can't.
## - floor: a floor style under a room (looks only).
## - footbridge: a walkway over a straight belt. People cross belts only at footbridges
##   (npc_layer.gd reads `bridges` through factory_floor.room_painter, draws them and walks
##   people over them; it adds none of its own).
## - worker door: where people come in and go out, on any free floor cell; place as many as
##   you like. npc_layer.gd reads `staff_doors`, draws them and sends each person through the
##   one nearest their machine. Until you place one there is a single staff door
##   (npc_layer.gd's own); erase it, or press Remove worker doors, and there are no doors at
##   all: people then appear at their machines and fade out when done (`staff_door_on`).
## Drawing a wall across a belt that runs straight across it leaves a belt port there, so
## walling in a working line doesn't cut its belts.
##
## Blocks are kept in user://factory_rooms.json (not with the line layout, so they never need
## an empty factory to save). factory_floor.gd routes belts round them (room_walls) and
## npc_layer.gd walks people round them.

const FactoryFloor := preload("res://scripts/factory_floor.gd")

signal undo_pushed  # a room edit went onto the undo stack
signal undo_cleared  # the room undo stack was emptied (Save or Cancel)

# Set by main.gd for one Undo across every build edit: undo_router undoes the latest edit of any
# kind (calling undo() here when it was a room edit), can_undo_check says whether there is one.
var undo_router := Callable()
var can_undo_check := Callable()

const CELL := 64
const SAVE_FILE := "user://factory_rooms.json"
const THEME_PATH := "res://assets/ui/theme/factory_theme.tres"
const BUILD_PANEL := 320.0  # main.gd's build panel on the right
const UNDO_MAX := 60
const DOOR_REACH := 80.0  # a door slides open while someone is this close
const HALF := 13.0  # half the wall thickness

const COLOR_OUTLINE := Color("16181C")
const COLOR_WALL := Color("3E424B")  # the wall's sides
const COLOR_WALL_TOP := Color("596070")  # its top face
const COLOR_RIDGE := Color("6B7383")  # a thin ridge along the top
const COLOR_SHADOW := Color(0, 0, 0, 0.28)
const COLOR_GLASS := Color(0.55, 0.75, 1.0, 0.55)
const COLOR_DOOR := Color("6B717D")
const COLOR_AMBER := Color("F2B84C")
const COLOR_PASS := Color("5ABF66")
const COLOR_FAIL := Color("E5484D")
const COLOR_ACCENT := Color("8CBFFF")
const COLOR_FLAP := Color(0.08, 0.09, 0.11, 0.62)

const WALL_KINDS := ["wall", "window", "door", "port"]
const DIRS := {"n": Vector2i(0, -1), "e": Vector2i(1, 0), "s": Vector2i(0, 1), "w": Vector2i(-1, 0)}

## Floor styles: a tile, and a colour laid over it.
const FLOORS := {
	"plate": {"name": "Tread plate", "tile": "res://assets/floor/floor_plate.svg", "tint": Color(0, 0, 0, 0)},
	"grate": {"name": "Grate", "tile": "res://assets/floor/floor_grate.svg", "tint": Color(0, 0, 0, 0)},
	"hazard": {"name": "Hazard stripes", "tile": "res://assets/floor/floor_hazard.svg", "tint": Color(0, 0, 0, 0)},
	"concrete": {"name": "Concrete", "tile": "res://assets/floor/floor_plain.svg", "tint": Color(0.75, 0.75, 0.72, 0.13)},
	"blue": {"name": "Blue epoxy", "tile": "res://assets/floor/floor_plain.svg", "tint": Color(0.3, 0.5, 0.95, 0.16)},
	"green": {"name": "Green epoxy", "tile": "res://assets/floor/floor_plain.svg", "tint": Color(0.3, 0.8, 0.45, 0.13)},
	"sand": {"name": "Sand", "tile": "res://assets/floor/floor_plain.svg", "tint": Color(0.95, 0.75, 0.4, 0.13)},
	"red": {"name": "Red epoxy", "tile": "res://assets/floor/floor_plain.svg", "tint": Color(0.95, 0.35, 0.35, 0.12)},
}
const FLOOR_ORDER := ["plate", "grate", "concrete", "blue", "green", "sand", "red", "hazard"]

# Rooms bar buttons are icon-only; the name and what it does show in the tooltip.
const TOOL_ICONS := {
	"room": "icon_room", "wall": "icon_wall", "window": "icon_window", "door": "icon_door",
	"port": "icon_belt_port", "bridge": "icon_bridge", "staff": "icon_worker_door",
	"floor": "icon_floor", "label": "icon_name", "erase": "icon_erase",
}

const TOOLS := [
	["room", "Room", "Drag a rectangle: walls round the edge and floor inside."],
	["wall", "Wall", "Drag to draw a wall. Where it crosses a belt it leaves a belt port."],
	["window", "Window", "Drag to draw windows: walls you can see through."],
	["door", "Door", "Click or drag along a wall. People walk through doors; belts can't."],
	["port", "Belt port", "Click or drag along a wall where a belt should cross it."],
	["bridge", "Footbridge", "Click or drag along a straight belt to put footbridges over it. People cross belts only at footbridges; erase removes them."],
	["staff", "Worker door", "Click free floor to put a worker door there; add as many as you like. Workers come in through the door nearest their machine and leave by it; erase removes one (the first staff door too)."],
	["floor", "Floor", "Drag a rectangle to lay floor."],
	["label", "Name", "Click the floor to name a room. Click a name to rename it, drag it to move it; clear the text to remove it."],
	["erase", "Erase", "Drag over walls, floor, names, footbridges and doors (the staff door too) to remove them."],
]

var factory_floor: FactoryFloor
var npc_layer: Node2D  # optional: doors open when its people come near

var walls := {}  # Vector2i -> "wall" | "window" | "door" | "port"
var floors := {}  # Vector2i -> floor style id
var labels := {}  # Vector2i -> a room's name, drawn as a sign centred on that cell
var bridges := {}  # Vector2i -> true: a footbridge over the belt on that cell
var staff_doors := {}  # Vector2i -> true: a worker door on that floor cell
var staff_door_on := true  # false once you remove the first staff door (npc_layer.gd's own)
var _undo: Array = []
# Room changes are a draft while build mode is open, like the lines: the build panel's Save keeps
# them (writes the file), Cancel or closing build mode puts back what was saved.
var _saved := {}  # {"walls", "floors"} as last saved
var _build_panel: Object = null

var tool := ""
var floor_style := "plate"
var _drag := {}  # {"from": Vector2i, "to": Vector2i} while the left button is held
var _hover := Vector2i(-99999, -99999)
var _door_open := {}  # door cell -> 0..1
var _tiles := {}

var _ui: Control
var _bar: PanelContainer
var _buttons := {}  # tool id -> Button
var _style_pick: OptionButton
var _hint: Label
var _undo_button: Button
var _editor: LineEdit  # typing a room's name
var _editor_cell := Vector2i.ZERO


func _ready() -> void:
	_build_ui()
	_load()
	_saved = _state()
	_apply()


# Data ---------------------------------------------------------------------------------------

func _load() -> void:
	if not FileAccess.file_exists(SAVE_FILE):
		return
	var data = JSON.parse_string(FileAccess.get_file_as_string(SAVE_FILE))
	if not data is Dictionary:
		return
	for row in data.get("walls", []):
		if row is Array and row.size() == 3 and str(row[2]) in WALL_KINDS:
			walls[Vector2i(int(row[0]), int(row[1]))] = str(row[2])
	for row in data.get("floors", []):
		if row is Array and row.size() == 3 and FLOORS.has(str(row[2])):
			floors[Vector2i(int(row[0]), int(row[1]))] = str(row[2])
	for row in data.get("labels", []):
		if row is Array and row.size() == 3 and str(row[2]).strip_edges() != "":
			labels[Vector2i(int(row[0]), int(row[1]))] = str(row[2])
	for row in data.get("bridges", []):
		if row is Array and row.size() >= 2:
			bridges[Vector2i(int(row[0]), int(row[1]))] = true
	for row in data.get("staff_doors", []):
		if row is Array and row.size() >= 2:
			staff_doors[Vector2i(int(row[0]), int(row[1]))] = true
	# Saved before the staff door could be removed: your own doors had already replaced it.
	staff_door_on = bool(data.get("staff_door", staff_doors.is_empty()))


## Everything you can build here, copied (for undo and for Cancel).
func _state() -> Dictionary:
	return {"walls": walls.duplicate(), "floors": floors.duplicate(), "labels": labels.duplicate(),
			"bridges": bridges.duplicate(), "staff_doors": staff_doors.duplicate(), "staff_door": staff_door_on}


func _set_state(st: Dictionary) -> void:
	walls = (st.get("walls", {}) as Dictionary).duplicate()
	floors = (st.get("floors", {}) as Dictionary).duplicate()
	labels = (st.get("labels", {}) as Dictionary).duplicate()
	bridges = (st.get("bridges", {}) as Dictionary).duplicate()
	staff_doors = (st.get("staff_doors", {}) as Dictionary).duplicate()
	staff_door_on = bool(st.get("staff_door", true))


## Hook the build panel's Save button (main.gd's build_panel), once.
func _watch_save() -> void:
	if _build_panel != null:
		return
	var panel = get_parent().get("build_panel") if get_parent() else null
	if panel is Object and panel.has_signal("save_requested"):
		_build_panel = panel
		panel.save_requested.connect(func(_pipelines: Array) -> void: _keep_draft())
		# The build panel's "Clear all" empties the factory: the worker doors go with it (a
		# draft like the rest: Save keeps it, Cancel brings them back).
		if panel is Node:
			for b in (panel as Node).find_children("*", "Button", true, false):
				# The button is icon-only now: its name leads the tooltip.
				if (b as Button).text == "Clear all" or (b as Button).tooltip_text.begins_with("Clear all:"):
					(b as Button).pressed.connect(clear_worker_doors.bind(false))


## Save pressed: these rooms are the saved ones now.
## Take away every worker door (an undoable draft edit, like the blocks).
func clear_worker_doors(say := true) -> void:
	if staff_doors.is_empty() and not staff_door_on:
		if say:
			_flash("There are no doors to remove.")
		return
	var before := _state()
	staff_doors.clear()
	staff_door_on = false
	_undo.append(before)
	undo_pushed.emit()
	if _undo.size() > UNDO_MAX:
		_undo.pop_front()
	_apply()
	_refresh_ui()
	if say:
		_flash("Doors removed: people now appear at their machines. Save keeps it, Undo or Cancel brings them back.")


# Picked with the build panel's Shift+box (build_panel.gd's _room_cells) -------------------------

## Every cell in `r` holding something built here: walls, floor, names, footbridges and doors
## (the first staff door too, while it stands).
func cells_in(r: Rect2i) -> Array:
	var out: Array = []
	var first := _staff_door_cell() if staff_door_on and staff_doors.is_empty() else Vector2i(-99999, -99999)
	for x in range(r.position.x, r.end.x):
		for y in range(r.position.y, r.end.y):
			var c := Vector2i(x, y)
			if walls.has(c) or floors.has(c) or labels.has(c) or bridges.has(c) or staff_doors.has(c) or c == first:
				out.append(c)
	return out


## Remove what is built on `cells` (Delete on a picked group). The build panel records the
## undo step: this pushes the layer's own state for its rooms_undo, without undo_pushed.
func erase_cells(cells: Array) -> void:
	var before := _state()
	var first := _staff_door_cell()
	for c in cells:
		if staff_doors.has(c) or (c == first and staff_doors.is_empty()):
			staff_door_on = false  # removing doors never brings the first one back
		walls.erase(c)
		floors.erase(c)
		labels.erase(c)
		bridges.erase(c)
		staff_doors.erase(c)
	if _state() != before:
		_push_quiet(before)
	_apply()
	_refresh_ui()


## Whether the blocks on `cells` could move by `shift`: each lands on a cell no other block,
## machine (`landing`) or, for a door or footbridge, wrong kind of cell already holds.
func can_shift(cells: Array, shift: Vector2i, landing: Dictionary) -> bool:
	var picked := {}
	for c in cells:
		picked[c] = true
	for c in cells:
		var to: Vector2i = c + shift
		if landing.has(to):
			return false
		if not picked.has(to) and (walls.has(to) or bridges.has(to) or staff_doors.has(to)):
			return false
		if staff_doors.has(c) and factory_floor.belt_cells.has(to):
			return false
		if bridges.has(c) and _straight_belt(to) == "":
			return false
	return true


## Move the blocks on `cells` by `shift` (the build panel records the undo step, as above).
func shift_cells(cells: Array, shift: Vector2i) -> void:
	var before := _state()
	var moved := {"walls": {}, "floors": {}, "labels": {}, "bridges": {}, "staff_doors": {}}
	var maps := {"walls": walls, "floors": floors, "labels": labels, "bridges": bridges, "staff_doors": staff_doors}
	for name in maps:
		var m: Dictionary = maps[name]
		for c in cells:
			if m.has(c):
				moved[name][c + shift] = m[c]
				m.erase(c)
	for name in maps:
		(maps[name] as Dictionary).merge(moved[name], true)
	if _state() != before:
		_push_quiet(before)
	_apply()
	_refresh_ui()


## Whether a door people use stands on `c`: a worker door, or the first staff door.
func door_at(c: Vector2i) -> bool:
	return staff_doors.has(c) or (staff_door_on and staff_doors.is_empty() and c == _staff_door_cell())


## Move a worker door (dragged in build mode). An undoable edit like the blocks.
func move_door(from: Vector2i, to: Vector2i) -> bool:
	if not staff_doors.has(from) or staff_doors.has(to) or walls.has(to) or factory_floor.belt_cells.has(to) \
			or _under_machine(to):
		return false
	var before := _state()
	staff_doors.erase(from)
	staff_doors[to] = true
	_undo.append(before)
	undo_pushed.emit()
	if _undo.size() > UNDO_MAX:
		_undo.pop_front()
	_apply()
	_refresh_ui()
	return true


func _push_quiet(before: Dictionary) -> void:
	_undo.append(before)
	if _undo.size() > UNDO_MAX:
		_undo.pop_front()


## Where npc_layer.gd's first staff door stands (shown only until you place a worker door).
func _staff_door_cell() -> Vector2i:
	if npc_layer and npc_layer.has_method("staff_door_cell"):
		return npc_layer.staff_door_cell()
	return Vector2i(-99999, -99999)


func _keep_draft() -> void:
	_save()
	_undo.clear()
	undo_cleared.emit()
	_refresh_ui()


## Build mode closed without saving: back to the saved rooms.
func _discard_draft() -> void:
	_undo.clear()
	undo_cleared.emit()
	_drag = {}
	_close_editor(false)
	if _state() == _saved:
		_refresh_ui()
		return
	_set_state(_saved)
	_apply()
	_refresh_ui()


func _save() -> void:
	_saved = _state()
	var w: Array = []
	for c in walls:
		w.append([c.x, c.y, walls[c]])
	var f: Array = []
	for c in floors:
		f.append([c.x, c.y, floors[c]])
	var file := FileAccess.open(SAVE_FILE, FileAccess.WRITE)
	var l: Array = []
	for c in labels:
		l.append([c.x, c.y, labels[c]])
	var b: Array = []
	for c in bridges:
		b.append([c.x, c.y])
	var sd: Array = []
	for c in staff_doors:
		sd.append([c.x, c.y])
	if file:
		file.store_string(JSON.stringify({"version": 1, "walls": w, "floors": f, "labels": l, "bridges": b,
				"staff_doors": sd, "staff_door": staff_door_on}))


## Hand the blocks to the floor (it routes belts round them) and redraw.
func _apply() -> void:
	if factory_floor == null:
		return
	factory_floor.room_painter = self
	var out := {}
	for c in walls:
		out[c] = {"kind": walls[c], "axis": _axis(c)}
	factory_floor.set_room_walls(out)
	queue_redraw()
	_forget_gone_picks()


## Room blocks picked in the build panel (its _room_cells) that are now gone, however they went
## (Erase, Delete, Remove worker doors, Clear all, Undo), stop being picked: no highlight is left
## on the floor and the "press Delete" hint goes too.
func _forget_gone_picks() -> void:
	var panel = _build_panel if _build_panel != null else (get_parent().get("build_panel") if get_parent() else null)
	if not (panel is Object and is_instance_valid(panel)):
		return
	var picked = panel.get("_room_cells")
	if not (picked is Dictionary) or (picked as Dictionary).is_empty():
		return
	var gone := false
	for c in (picked as Dictionary).keys():
		if not (walls.has(c) or floors.has(c) or labels.has(c) or bridges.has(c) or door_at(c)):
			(picked as Dictionary).erase(c)
			gone = true
	if gone:
		if panel.has_method("_refresh_ui"):
			panel.call("_refresh_ui")
		if panel.has_method("show_message"):
			panel.call("show_message", "")


func _push_undo() -> void:
	_undo.append(_state())
	undo_pushed.emit()
	if _undo.size() > UNDO_MAX:
		_undo.pop_front()
	_refresh_ui()


## Ctrl+Z or the Undo button: the shared build undo when main.gd set one, else rooms only.
func _undo_pressed() -> void:
	if undo_router.is_valid():
		undo_router.call()
	else:
		undo()
	_refresh_ui()


func refresh_undo() -> void:
	_refresh_ui()


func undo() -> void:
	if _undo.is_empty():
		return
	var last: Dictionary = _undo.pop_back()
	_set_state(last)
	_apply()
	_refresh_ui()


## The way the wall runs through cell c: "h" (east-west) or "v" (north-south). From the wall
## blocks next to it; a lone block follows the belt on it (the wall runs across the belt).
func _axis(c: Vector2i, extra := {}) -> String:
	var horiz := _is_block(c + DIRS["e"], extra) or _is_block(c + DIRS["w"], extra)
	var vert := _is_block(c + DIRS["n"], extra) or _is_block(c + DIRS["s"], extra)
	if horiz and not vert:
		return "h"
	if vert and not horiz:
		return "v"
	if horiz and vert:
		return "h"
	var belt := _straight_belt(c)
	if belt == "ew":
		return "v"
	return "h"


func _is_block(c: Vector2i, extra := {}) -> bool:
	if extra.has(c):
		return extra[c] != ""
	return walls.has(c)


## "ns" or "ew" when a single belt runs straight through cell c, else "" (none, a corner, a
## merge or a bridge).
func _straight_belt(c: Vector2i) -> String:
	if factory_floor == null or not factory_floor.belt_cells.has(c):
		return ""
	var cell: Dictionary = factory_floor.belt_cells[c]
	if cell.has("over") or (cell.get("in", []) as Array).size() != 1:
		return ""
	var out := str(cell.get("out", ""))
	var entry := str(cell["in"][0])
	if out in ["n", "s"] and entry in ["n", "s"] and out != entry:
		return "ns"
	if out in ["e", "w"] and entry in ["e", "w"] and out != entry:
		return "ew"
	return ""


func _under_machine(c: Vector2i) -> bool:
	for key in factory_floor.footprints:
		if Rect2i(factory_floor.footprints[key], Vector2i(2, 2)).has_point(c):
			return true
	return false


## What `kind` becomes on cell c (line: "h", "v" or "" for a single cell, the way the wall
## being drawn runs), or "" where it can't go: under a machine or station, or on a belt that
## doesn't run straight across the wall.
func _kind_at(kind: String, c: Vector2i, line: String) -> String:
	if _under_machine(c):
		return ""
	if not factory_floor.belt_cells.has(c):
		return kind
	var belt := _straight_belt(c)
	var across := belt != "" and (line == "" or (line == "h" and belt == "ns") or (line == "v" and belt == "ew"))
	if kind == "door" or not across:
		return ""
	return "port"


## The cells a drag covers, each with what it would become ("" = can't go there).
func _plan(from: Vector2i, to: Vector2i) -> Dictionary:
	var out := {}
	var box := Rect2i(from, Vector2i.ONE).merge(Rect2i(to, Vector2i.ONE))
	match tool:
		"room":
			var x0 := box.position.x
			var y0 := box.position.y
			var x1 := box.end.x - 1
			var y1 := box.end.y - 1
			for x in range(x0, x1 + 1):
				for y in range(y0, y1 + 1):
					var edge := x == x0 or x == x1 or y == y0 or y == y1
					if not edge:
						continue
					var line := "h" if (y == y0 or y == y1) and not (x == x0 or x == x1) else \
							("v" if (x == x0 or x == x1) and not (y == y0 or y == y1) else "")
					var keep := str(walls.get(Vector2i(x, y), ""))
					if keep == "door" or keep == "port":
						out[Vector2i(x, y)] = keep
					elif line == "":  # a corner: no belt may pass
						out[Vector2i(x, y)] = "" if factory_floor.belt_cells.has(Vector2i(x, y)) or \
								_under_machine(Vector2i(x, y)) else "wall"
					else:
						out[Vector2i(x, y)] = _kind_at("wall", Vector2i(x, y), line)
		"wall", "window", "door", "port":
			var d := to - from
			var horizontal := absi(d.x) >= absi(d.y)
			var line := "" if d == Vector2i.ZERO else ("h" if horizontal else "v")
			var cells: Array = []
			if horizontal:
				for x in range(mini(from.x, to.x), maxi(from.x, to.x) + 1):
					cells.append(Vector2i(x, from.y))
			else:
				for y in range(mini(from.y, to.y), maxi(from.y, to.y) + 1):
					cells.append(Vector2i(from.x, y))
			for c in cells:
				out[c] = _kind_at(tool, c, line)
		"bridge":
			var d := to - from
			var cells: Array = []
			if absi(d.x) >= absi(d.y):
				for x in range(mini(from.x, to.x), maxi(from.x, to.x) + 1):
					cells.append(Vector2i(x, from.y))
			else:
				for y in range(mini(from.y, to.y), maxi(from.y, to.y) + 1):
					cells.append(Vector2i(from.x, y))
			for c in cells:
				out[c] = "bridge" if _straight_belt(c) != "" and not walls.has(c) else ""
		"staff":
			var free := not walls.has(to) and not factory_floor.belt_cells.has(to) and not _under_machine(to)
			out[to] = "staff" if free else ""
		"floor", "erase":
			for x in range(box.position.x, box.end.x):
				for y in range(box.position.y, box.end.y):
					out[Vector2i(x, y)] = tool
	return out


func _commit(from: Vector2i, to: Vector2i) -> void:
	var plan := _plan(from, to)
	var changed := false
	var skipped := 0
	var before := _state()
	match tool:
		"room":
			var box := Rect2i(from, Vector2i.ONE).merge(Rect2i(to, Vector2i.ONE))
			for x in range(box.position.x, box.end.x):
				for y in range(box.position.y, box.end.y):
					floors[Vector2i(x, y)] = floor_style
			for c in plan:
				if plan[c] == "":
					skipped += 1
				else:
					walls[c] = plan[c]
		"wall", "window", "door", "port":
			for c in plan:
				if plan[c] == "":
					skipped += 1
				else:
					walls[c] = plan[c]
		"bridge":
			for c in plan:
				if plan[c] == "":
					skipped += 1
				else:
					bridges[c] = true
		"staff":
			for c in plan:
				if plan[c] == "":
					skipped += 1
				else:
					staff_doors[c] = true
					staff_door_on = false  # your doors replace the first one for good
		"floor":
			for c in plan:
				floors[c] = floor_style
		"erase":
			var first_door := _staff_door_cell()
			if not staff_doors.is_empty():
				staff_door_on = false  # erasing your doors never brings the first one back
			for c in plan:
				if staff_door_on and c == first_door and staff_doors.is_empty():
					staff_door_on = false
				bridges.erase(c)
				staff_doors.erase(c)
				walls.erase(c)
				floors.erase(c)
				labels.erase(c)
	changed = _state() != before
	if changed:
		_undo.append(before)
		undo_pushed.emit()
		if _undo.size() > UNDO_MAX:
			_undo.pop_front()
		_apply()
	_refresh_ui()
	if skipped > 0 and tool == "staff":
		_flash("A worker door goes on free floor: not under a machine or station, on a belt or in a wall.")
	elif skipped > 0 and tool == "bridge":
		_flash("%d cell%s left out: a footbridge goes over a belt that runs straight (not a corner, junction or wall)." % [
				skipped, "" if skipped == 1 else "s"])
	elif skipped > 0:
		_flash("%d cell%s left out: under a machine, or on a belt that doesn't run straight across." % [
				skipped, "" if skipped == 1 else "s"])


## The cells holding floor or walls, as one rectangle (the floor counts them in its bounds).
func floor_rect() -> Rect2i:
	var r := Rect2i()
	var first := true
	for c in floors:
		r = Rect2i(c, Vector2i.ONE) if first else r.merge(Rect2i(c, Vector2i.ONE))
		first = false
	return r


# Input --------------------------------------------------------------------------------------

func set_tool(id: String) -> void:
	_close_editor(true)
	tool = id
	_drag = {}
	for t in _buttons:
		(_buttons[t] as Button).set_pressed_no_signal(t == id)
	_refresh_ui()
	queue_redraw()


# Room names -----------------------------------------------------------------------------------

## Change a name: text "" removes it; move_to puts the existing name on another cell.
func _set_label(c: Vector2i, text: String, move_to = null) -> void:
	var before := _state()
	if move_to is Vector2i:
		var name: String = labels.get(c, "")
		labels.erase(c)
		if name != "":
			labels[move_to] = name
	elif text.strip_edges() == "":
		labels.erase(c)
	else:
		labels[c] = text.strip_edges()
	if _state() != before:
		_undo.append(before)
		undo_pushed.emit()
		if _undo.size() > UNDO_MAX:
			_undo.pop_front()
		_refresh_ui()
	queue_redraw()


func _open_editor(c: Vector2i) -> void:
	_editor_cell = c
	_editor.text = labels.get(c, "")
	_editor.placeholder_text = "Room name"
	var screen := get_viewport().get_canvas_transform() * factory_floor.cell_center(c)
	_editor.size = Vector2(220, 0)
	_editor.position = screen - Vector2(110, 18)
	_editor.visible = true
	_editor.grab_focus()
	_editor.select_all()
	queue_redraw()


## keep: true writes what was typed, false throws it away.
func _close_editor(keep: bool) -> void:
	if _editor == null or not _editor.visible:
		return
	_editor.visible = false
	_editor.release_focus()
	if keep:
		_set_label(_editor_cell, _editor.text)
	queue_redraw()


## Runs before main.gd's input (it is a child of main), and only while a room tool is picked.
func _unhandled_input(event: InputEvent) -> void:
	if factory_floor == null or not factory_floor.editing:
		return
	var key := event as InputEventKey
	if key and key.pressed and not key.echo:
		var focus := get_viewport().gui_get_focus_owner()
		if focus is LineEdit or focus is TextEdit:
			return
		if key.keycode == KEY_Z and key.is_command_or_control_pressed():
			_undo_pressed()
			get_viewport().set_input_as_handled()
			return
		if key.keycode == KEY_ESCAPE and _editor.visible:
			_close_editor(false)
			get_viewport().set_input_as_handled()
			return
		if key.keycode == KEY_ESCAPE and tool != "":
			set_tool("")
			get_viewport().set_input_as_handled()
		return
	if tool == "":
		return
	var mouse := event as InputEventMouse
	if mouse == null:
		return
	if Input.is_physical_key_pressed(KEY_SPACE):
		return  # Space + drag pans
	var at: Vector2 = (make_input_local(mouse) as InputEventMouse).position
	var c := factory_floor.cell_at(at)
	var motion := event as InputEventMouseMotion
	if motion:
		if not factory_floor.ghost.is_empty():
			set_tool("")  # the build panel is placing or moving a machine
			return
		if c != _hover:
			_hover = c
			if not _drag.is_empty():
				_drag["to"] = c
			queue_redraw()
		return
	var button := event as InputEventMouseButton
	if button == null or button.button_index != MOUSE_BUTTON_LEFT:
		return
	if tool == "label":
		if button.pressed:
			_close_editor(true)
			_drag = {"from": c, "to": c}
		elif not _drag.is_empty():
			var from: Vector2i = _drag["from"]
			_drag = {}
			if labels.has(from) and c != from:
				_set_label(from, "", c)  # moved
			else:
				_open_editor(from)
		queue_redraw()
		get_viewport().set_input_as_handled()
		return
	if button.pressed:
		_drag = {"from": c, "to": c}
		queue_redraw()
		get_viewport().set_input_as_handled()
	elif not _drag.is_empty():
		var from: Vector2i = _drag["from"]
		_drag = {}
		_commit(from, c)
		queue_redraw()
		get_viewport().set_input_as_handled()


func _process(delta: float) -> void:
	if factory_floor == null:
		return
	var editing := factory_floor.editing
	if _bar.visible != editing:
		_bar.visible = editing
		if editing:
			_watch_save()
		else:
			set_tool("")
			_discard_draft()
	# Doors slide open while someone is near.
	var people: Array = []
	if npc_layer and npc_layer.visible:
		for n in npc_layer.get_children():
			if n is Node2D and (n as Node2D).visible:
				people.append((n as Node2D).position)
	var changed := false
	for c in walls:
		if walls[c] != "door":
			continue
		var at := factory_floor.cell_center(c)
		var near := false
		for p in people:
			if at.distance_to(p) < DOOR_REACH:
				near = true
				break
		var was: float = _door_open.get(c, 0.0)
		var now := move_toward(was, 1.0 if near else 0.0, delta * 4.0)
		if now != was:
			_door_open[c] = now
			changed = true
	if changed:
		queue_redraw()


# Drawing ------------------------------------------------------------------------------------

func _tile(path: String) -> Texture2D:
	if not _tiles.has(path):
		_tiles[path] = load(path) if ResourceLoader.exists(path) else null
	return _tiles[path]


## Called by the floor after it lays its plain tiles and before the belts.
## A wall cell gets floor only on the side(s) facing a room: each quarter of it takes the floor
## of the cell diagonally beyond that corner, so a room's floor stops at the middle of its walls.
func draw_floor(canvas: CanvasItem, _area: Rect2i) -> void:
	for c in floors:
		if not walls.has(c):
			_floor_tile(canvas, floors[c], Rect2(Vector2(c) * CELL, Vector2(CELL, CELL)), Rect2(0, 0, 1, 1))
	var quarter := CELL / 2.0
	for c in walls:
		for dx in [-1, 1]:
			for dy in [-1, 1]:
				var diag: Vector2i = c + Vector2i(dx, dy)
				var style := ""
				if floors.has(diag) and not walls.has(diag):
					style = floors[diag]
				elif floors.has(c) and not walls.has(c + Vector2i(dx, 0)) and floors.has(c + Vector2i(dx, 0)):
					style = floors[c + Vector2i(dx, 0)]
				elif floors.has(c) and not walls.has(c + Vector2i(0, dy)) and floors.has(c + Vector2i(0, dy)):
					style = floors[c + Vector2i(0, dy)]
				if style == "":
					continue
				var qx := 0.0 if dx < 0 else quarter
				var qy := 0.0 if dy < 0 else quarter
				_floor_tile(canvas, style, Rect2(Vector2(c) * CELL + Vector2(qx, qy), Vector2(quarter, quarter)),
						Rect2(qx / CELL, qy / CELL, 0.5, 0.5))


## Part of a floor tile: `part` is the share of the tile to draw (0..1 on each side).
func _floor_tile(canvas: CanvasItem, id: String, r: Rect2, part: Rect2) -> void:
	var style: Dictionary = FLOORS.get(id, FLOORS["plate"])
	var tex := _tile(style["tile"])
	if tex:
		var size := Vector2(tex.get_size())
		canvas.draw_texture_rect_region(tex, r, Rect2(part.position * size, part.size * size))
	var tint: Color = style["tint"]
	if tint.a > 0.0:
		canvas.draw_rect(r, tint)


func _draw() -> void:
	var rects: Array = []  # [Rect2] wall bodies
	var tops: Array = []  # [Rect2] their top faces (inset from the sides, not where they join)
	var details: Array = []  # [cell, kind, axis]
	for c in walls:
		var kind: String = walls[c]
		var axis := _axis(c)
		var o := Vector2(c) * CELL
		var mid := CELL / 2.0
		if kind == "door" or kind == "port":
			# An opening: short posts at both ends, the gap between them. Door blocks side by
			# side make one wide door, with posts only at its ends.
			var run: Array = _door_run(c, axis) if kind == "door" else [c, c]
			if axis == "h":
				var a := Rect2(o + Vector2(0, mid - HALF), Vector2(9, HALF * 2))
				var b := Rect2(o + Vector2(CELL - 9, mid - HALF), Vector2(9, HALF * 2))
				if run[0] == c:
					rects.append(a)
					tops.append(a.grow_individual(0, -3, -3, -4))
				if run[1] == c:
					rects.append(b)
					tops.append(b.grow_individual(-3, -3, 0, -4))
			else:
				var a := Rect2(o + Vector2(mid - HALF, 0), Vector2(HALF * 2, 9))
				var b := Rect2(o + Vector2(mid - HALF, CELL - 9), Vector2(HALF * 2, 9))
				if run[0] == c:
					rects.append(a)
					tops.append(a.grow_individual(-3, 0, -4, -3))
				if run[1] == c:
					rects.append(b)
					tops.append(b.grow_individual(-3, -3, -4, 0))
		else:
			var post := Rect2(o + Vector2(mid - HALF, mid - HALF), Vector2(HALF * 2, HALF * 2))
			rects.append(post)
			tops.append(post.grow_individual(-3, -3, -3, -4))
			for d in DIRS:
				if walls.has(c + DIRS[d]):
					var arm := _arm(o, d)
					rects.append(arm)
					tops.append(arm.grow_individual(0, -3, 0, -4) if d in ["e", "w"] else arm.grow_individual(-3, 0, -4, 0))
		details.append([c, kind, axis])

	for r in rects:
		draw_rect(Rect2(r.position + Vector2(5, 6), r.size), COLOR_SHADOW)
	for r in rects:
		draw_rect(r.grow(2), COLOR_OUTLINE)
	for r in rects:
		draw_rect(r, COLOR_WALL)
	for r in tops:
		draw_rect(r, COLOR_WALL_TOP)
	for d in details:
		_draw_detail(d[0], d[1], d[2])
	_draw_labels()

	_draw_preview()


const LABEL_SIZE := 26

## Room names: a dark sign on the floor with the name in it.
func _draw_labels() -> void:
	var font := ThemeDB.fallback_font
	var picking := tool == "label" and factory_floor != null and factory_floor.editing
	for c in labels:
		if _editor != null and _editor.visible and c == _editor_cell:
			continue
		var text: String = labels[c]
		var size := font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, LABEL_SIZE)
		var center := factory_floor.cell_center(c)
		var plate := Rect2(center - Vector2(size.x / 2.0 + 14, 22), Vector2(size.x + 28, 44))
		if not _drag.is_empty() and picking and _drag["from"] == c and _drag["to"] != c:
			plate.position += factory_floor.cell_center(_drag["to"]) - center
			center = factory_floor.cell_center(_drag["to"])
		draw_rect(Rect2(plate.position + Vector2(4, 5), plate.size), COLOR_SHADOW)
		draw_rect(plate.grow(2), COLOR_OUTLINE)
		draw_rect(plate, Color(0.11, 0.12, 0.14, 0.92))
		draw_rect(plate.grow(-3), COLOR_WALL_TOP, false, 1.0)
		draw_string(font, Vector2(plate.position.x + 14, center.y + LABEL_SIZE * 0.36), text,
				HORIZONTAL_ALIGNMENT_LEFT, -1, LABEL_SIZE, Color("E6E9EF"))
		if picking and c == _hover:
			draw_rect(plate.grow(5), COLOR_ACCENT, false, 2.0)


func _arm(o: Vector2, d: String) -> Rect2:
	var mid := CELL / 2.0
	match d:
		"e":
			return Rect2(o + Vector2(mid, mid - HALF), Vector2(mid, HALF * 2))
		"w":
			return Rect2(o + Vector2(0, mid - HALF), Vector2(mid, HALF * 2))
		"n":
			return Rect2(o + Vector2(mid - HALF, 0), Vector2(HALF * 2, mid))
	return Rect2(o + Vector2(mid - HALF, mid), Vector2(HALF * 2, mid))


func _draw_detail(c: Vector2i, kind: String, axis: String) -> void:
	var o := Vector2(c) * CELL
	var mid := CELL / 2.0
	var center := o + Vector2(mid, mid)
	match kind:
		"wall", "window":
			# A lighter cap down the middle of the wall, and glass in a window.
			var arms: Array = []
			for d in DIRS:
				if walls.has(c + DIRS[d]):
					arms.append(d)
			var w := 2.0 if kind == "wall" else 8.0
			var color := COLOR_RIDGE if kind == "wall" else COLOR_GLASS
			draw_rect(Rect2(center - Vector2(w, w) / 2.0, Vector2(w, w)), color)
			for d in arms:
				var to: Vector2 = center + Vector2(DIRS[d]) * mid
				var r := Rect2(center, Vector2.ZERO).expand(to)
				r = r.grow_individual(w / 2.0 if d in ["n", "s"] else 0.0, w / 2.0 if d in ["e", "w"] else 0.0,
						w / 2.0 if d in ["n", "s"] else 0.0, w / 2.0 if d in ["e", "w"] else 0.0)
				draw_rect(r, color)
				if kind == "window":
					var glint := Color(1, 1, 1, 0.55)
					if d in ["e", "w"]:
						draw_line(Vector2(r.position.x, r.position.y + 1.5), Vector2(r.end.x, r.position.y + 1.5), glint, 1.0)
					else:
						draw_line(Vector2(r.position.x + 1.5, r.position.y), Vector2(r.position.x + 1.5, r.end.y), glint, 1.0)
			if arms.is_empty():
				draw_rect(Rect2(center - Vector2(w, w) / 2.0, Vector2(w, w)), color)
		"door":
			# The whole door is drawn from its first block: two leaves that slide apart.
			var run: Array = _door_run(c, axis)
			if run[0] != c:
				return
			var span: Vector2i = run[1] - run[0]
			var cells: int = span.x + span.y + 1
			var open := 0.0
			for i in cells:
				var step := Vector2i(i, 0) if axis == "h" else Vector2i(0, i)
				open = maxf(open, float(_door_open.get(c + step, 0.0)))
			var gap: float = CELL * cells - 18.0  # between the posts
			var leaf := gap / 2.0 * (1.0 - open * 0.85)
			var track := 4.0
			if axis == "h":
				draw_rect(Rect2(o + Vector2(9, mid - track / 2.0), Vector2(gap, track)), COLOR_OUTLINE)
				_door_leaf(Rect2(o + Vector2(9, mid - 5), Vector2(leaf, 10)), "e")
				_door_leaf(Rect2(o + Vector2(9 + gap - leaf, mid - 5), Vector2(leaf, 10)), "w")
			else:
				draw_rect(Rect2(o + Vector2(mid - track / 2.0, 9), Vector2(track, gap)), COLOR_OUTLINE)
				_door_leaf(Rect2(o + Vector2(mid - 5, 9), Vector2(10, leaf)), "s")
				_door_leaf(Rect2(o + Vector2(mid - 5, 9 + gap - leaf), Vector2(10, leaf)), "n")
		"port":
			# A curtain of rubber flaps across the opening (the belt shows through it dimmed), and
			# green marks on the posts.
			var n := 6
			var seam := Color(COLOR_WALL_TOP, 0.9)
			if axis == "h":
				var curtain := Rect2(o + Vector2(9, mid - HALF + 2), Vector2(CELL - 18, HALF * 2 - 4))
				draw_rect(curtain, COLOR_FLAP)
				var step := curtain.size.x / n
				for i in range(1, n):
					var x := curtain.position.x + i * step
					draw_line(Vector2(x, curtain.position.y), Vector2(x, curtain.end.y), seam, 1.0)
				draw_rect(Rect2(o + Vector2(6, mid - HALF), Vector2(3, HALF * 2)), COLOR_PASS)
				draw_rect(Rect2(o + Vector2(CELL - 9, mid - HALF), Vector2(3, HALF * 2)), COLOR_PASS)
			else:
				var curtain := Rect2(o + Vector2(mid - HALF + 2, 9), Vector2(HALF * 2 - 4, CELL - 18))
				draw_rect(curtain, COLOR_FLAP)
				var step := curtain.size.y / n
				for i in range(1, n):
					var y := curtain.position.y + i * step
					draw_line(Vector2(curtain.position.x, y), Vector2(curtain.end.x, y), seam, 1.0)
				draw_rect(Rect2(o + Vector2(mid - HALF, 6), Vector2(HALF * 2, 3)), COLOR_PASS)
				draw_rect(Rect2(o + Vector2(mid - HALF, CELL - 9), Vector2(HALF * 2, 3)), COLOR_PASS)


## The first and last block of the door that cell c is part of, along its wall.
func _door_run(c: Vector2i, axis: String) -> Array:
	var step := Vector2i(1, 0) if axis == "h" else Vector2i(0, 1)
	var a := c
	while walls.get(a - step, "") == "door" and _axis(a - step) == axis:
		a -= step
	var b := c
	while walls.get(b + step, "") == "door" and _axis(b + step) == axis:
		b += step
	return [a, b]


## One sliding door leaf; `edge` is the side that meets the other leaf (amber stripe).
func _door_leaf(r: Rect2, edge: String) -> void:
	if r.size.x <= 0.5 or r.size.y <= 0.5:
		return
	draw_rect(r.grow(1), COLOR_OUTLINE)
	draw_rect(r, COLOR_DOOR)
	var s := 3.0
	match edge:
		"e":
			draw_rect(Rect2(Vector2(r.end.x - s, r.position.y), Vector2(s, r.size.y)), COLOR_AMBER)
		"w":
			draw_rect(Rect2(r.position, Vector2(s, r.size.y)), COLOR_AMBER)
		"s":
			draw_rect(Rect2(Vector2(r.position.x, r.end.y - s), Vector2(r.size.x, s)), COLOR_AMBER)
		"n":
			draw_rect(Rect2(r.position, Vector2(r.size.x, s)), COLOR_AMBER)


func _draw_preview() -> void:
	if tool == "" or factory_floor == null or not factory_floor.editing:
		return
	if _drag.is_empty():
		var r := Rect2(Vector2(_hover) * CELL, Vector2(CELL, CELL))
		draw_rect(r.grow(-2), COLOR_ACCENT if tool != "erase" else COLOR_FAIL, false, 2.0)
		return
	var plan := _plan(_drag["from"], _drag["to"])
	if tool == "room":
		var box := Rect2i(_drag["from"], Vector2i.ONE).merge(Rect2i(_drag["to"], Vector2i.ONE))
		var style: Dictionary = FLOORS[floor_style]
		var tex := _tile(style["tile"])
		var area := Rect2(Vector2(box.position) * CELL, Vector2(box.size) * CELL)
		if tex:
			draw_texture_rect(tex, area, true, Color(1, 1, 1, 0.35))
		draw_string(ThemeDB.fallback_font, area.position + Vector2(8, -8), "%d x %d" % [box.size.x, box.size.y],
				HORIZONTAL_ALIGNMENT_LEFT, -1, 16, COLOR_ACCENT)
	for c in plan:
		var r := Rect2(Vector2(c) * CELL, Vector2(CELL, CELL))
		var what: String = plan[c]
		if what == "":
			draw_rect(r.grow(-3), Color(COLOR_FAIL, 0.25))
			draw_rect(r.grow(-3), COLOR_FAIL, false, 2.0)
		elif what == "erase":
			if walls.has(c) or floors.has(c) or bridges.has(c) or staff_doors.has(c) \
					or (staff_door_on and staff_doors.is_empty() and c == _staff_door_cell()):
				draw_rect(r.grow(-3), Color(COLOR_FAIL, 0.2))
			draw_rect(r.grow(-3), COLOR_FAIL, false, 1.5)
		elif what == "floor":
			var style: Dictionary = FLOORS[floor_style]
			var tex := _tile(style["tile"])
			if tex:
				draw_texture_rect(tex, r, false, Color(1, 1, 1, 0.55))
			draw_rect(r.grow(-3), COLOR_ACCENT, false, 1.5)
		else:
			var color := COLOR_PASS if what == "port" else (COLOR_AMBER if what == "door" else COLOR_ACCENT)
			draw_rect(r.grow(-3), Color(color, 0.25))
			draw_rect(r.grow(-3), color, false, 2.0)


# Tool bar -----------------------------------------------------------------------------------

func _build_ui() -> void:
	var layer := CanvasLayer.new()
	add_child(layer)
	_ui = Control.new()
	_ui.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	_ui.mouse_filter = Control.MOUSE_FILTER_IGNORE
	if ResourceLoader.exists(THEME_PATH):
		_ui.theme = load(THEME_PATH)
	layer.add_child(_ui)

	_bar = PanelContainer.new()
	_bar.theme_type_variation = &"PanelToast"
	_bar.visible = false
	_ui.add_child(_bar)
	_bar.anchor_left = 1.0
	_bar.anchor_right = 1.0
	_bar.anchor_top = 1.0
	_bar.anchor_bottom = 1.0
	_bar.offset_right = -BUILD_PANEL - 12.0
	_bar.offset_bottom = -12.0
	_bar.grow_horizontal = Control.GROW_DIRECTION_BEGIN
	_bar.grow_vertical = Control.GROW_DIRECTION_BEGIN

	var box := VBoxContainer.new()
	_bar.add_child(box)
	var head := HBoxContainer.new()
	box.add_child(head)
	var title := Label.new()
	title.text = "Rooms"
	title.theme_type_variation = &"LabelHeading"
	head.add_child(title)
	_hint = Label.new()
	_hint.theme_type_variation = &"LabelMuted"
	_hint.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_hint.custom_minimum_size = Vector2(420, 0)
	_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	head.add_child(_hint)

	var row := HBoxContainer.new()
	box.add_child(row)
	for t in TOOLS:
		var b := Button.new()
		b.icon = load("res://assets/ui/%s.svg" % TOOL_ICONS.get(t[0], "icon_room"))
		b.toggle_mode = true
		b.theme_type_variation = &"ButtonTile"
		b.tooltip_text = "%s: %s" % [t[1], t[2]]
		b.focus_mode = Control.FOCUS_NONE
		var id: String = t[0]
		b.toggled.connect(func(on: bool) -> void: set_tool(id if on else ""))
		row.add_child(b)
		_buttons[id] = b
		if id == "floor":
			_style_pick = OptionButton.new()
			_style_pick.focus_mode = Control.FOCUS_NONE
			_style_pick.tooltip_text = "Floor for the Room and Floor tools"
			for s in FLOOR_ORDER:
				_style_pick.add_item(FLOORS[s]["name"])
			_style_pick.item_selected.connect(func(i: int) -> void:
				floor_style = FLOOR_ORDER[i]
				queue_redraw())
			row.add_child(_style_pick)
	var clear_doors := Button.new()
	clear_doors.icon = load("res://assets/ui/icon_clear_all.svg")
	clear_doors.theme_type_variation = &"ButtonGhost"
	clear_doors.tooltip_text = "Remove worker doors: take away every door, the staff door too (people then appear at their machines)"
	clear_doors.focus_mode = Control.FOCUS_NONE
	clear_doors.pressed.connect(clear_worker_doors)
	row.add_child(clear_doors)
	var sep := VSeparator.new()
	row.add_child(sep)
	_undo_button = Button.new()
	_undo_button.icon = load("res://assets/ui/icon_undo.svg")
	_undo_button.theme_type_variation = &"ButtonGhost"
	_undo_button.tooltip_text = "Undo: undo the last room change (Ctrl+Z)"
	_undo_button.focus_mode = Control.FOCUS_NONE
	_undo_button.pressed.connect(_undo_pressed)
	row.add_child(_undo_button)

	_editor = LineEdit.new()
	_editor.visible = false
	_editor.max_length = 40
	_editor.alignment = HORIZONTAL_ALIGNMENT_CENTER
	_ui.add_child(_editor)
	_editor.text_submitted.connect(func(_t: String) -> void: _close_editor(true))
	_editor.focus_exited.connect(func() -> void: _close_editor(true))
	_refresh_ui()


func _refresh_ui() -> void:
	if _hint == null:
		return
	var text := "Pick a block, then click or drag on the floor. Save keeps room changes, Cancel drops them."
	for t in TOOLS:
		if t[0] == tool:
			text = t[2]
	_hint.text = text
	_undo_button.disabled = not bool(can_undo_check.call()) if can_undo_check.is_valid() else _undo.is_empty()


var _flash_tween: Tween

## A short note in the hint line (then it goes back to the tool's hint).
func _flash(text: String) -> void:
	_hint.text = text
	_hint.add_theme_color_override("font_color", COLOR_AMBER)
	if _flash_tween:
		_flash_tween.kill()
	_flash_tween = create_tween()
	_flash_tween.tween_interval(4.0)
	_flash_tween.tween_callback(func() -> void:
		_hint.remove_theme_color_override("font_color")
		_refresh_ui())
