extends Node2D
## Draws the factory floor from the backend's pipelines using the sprites in res://assets.
##
## Each pipeline is an intake, machines and a shipping dock joined by belts, plus a scrap
## bin, all placed on a grid of 64px cells (machines and stations are 2x2). A machine has
## an input on its west side and two outputs on its east side: "pass" (upper cell) and
## "fail" (lower cell). Pass belts arrive at the upper west cell, red belts (from a fail
## output back to an earlier machine, or into the scrap bin) at the lower west cell.
##
## Belts keep the path they were saved with while it still fits; otherwise they route
## themselves around machines (fewest turns). A second rework belt into the same machine
## joins the first one with a merge piece.
##
## Locations the backend uses: "<pipeline>:intake", "<pipeline>:belt_<i>" (in front of the
## i-th machine; belt_<n> leads to the dock), "<pipeline>:return_<i>" (the rework belt at
## machine i), "<pipeline>:dock", "<pipeline>:scrap", and machine ids.
##
## Machines are drawn on a second canvas (attach_machine_layer) that sits above the crates,
## so a crate inside a machine is hidden. This node knows nothing about the backend.

const CELL := 64
# Sprites to borrow when a newer machine's art isn't in res://assets yet.
const FALLBACK_SPRITES := {"risk_analyser": "reviewer", "planner": "refiner", "pr_merger": "merger",
		"sorter": "splitter"}
# A sorter's six outputs: the cell just outside each one (from the machine's top-left cell)
# and the way a belt leaves it (index into DIR_NAMES). Two on each of north, east and south.
const SORTER_PORTS := {
	"out_1": [Vector2i(0, -1), 0], "out_2": [Vector2i(1, -1), 0],
	"out_3": [Vector2i(2, 0), 1], "out_4": [Vector2i(2, 1), 1],
	"out_5": [Vector2i(0, 2), 2], "out_6": [Vector2i(1, 2), 2],
}
const LAMP := Vector2(110, 14)
const DIRS := {"n": Vector2i(0, -1), "e": Vector2i(1, 0), "s": Vector2i(0, 1), "w": Vector2i(-1, 0)}
const DIR_NAMES := ["n", "e", "s", "w"]
const OPPOSITE := {"n": "s", "s": "n", "e": "w", "w": "e"}
const STATIONS := ["intake", "dock", "scrap"]
const STATION_SPRITES := {"intake": "intake", "dock": "shipping_dock", "scrap": "scrap_bin"}
const STATION_NAMES := {"intake": "Intake", "dock": "Shipping dock", "scrap": "Scrap bin"}


## True for a scrap bin: "scrap", an extra bin's id ("scrap_2"), or either as a location or
## footprint key ("build:scrap_2").
static func is_bin(key: String) -> bool:
	var place := key.get_slice(":", key.get_slice_count(":") - 1)
	return place == "scrap" or (place.begins_with("scrap_") and place.trim_prefix("scrap_").is_valid_int())


## True for a shipping dock: "dock", an extra dock's id ("dock_2"), or either as a location or
## footprint key ("build:dock_2").
static func is_dock(key: String) -> bool:
	var place := key.get_slice(":", key.get_slice_count(":") - 1)
	return place == "dock" or (place.begins_with("dock_") and place.trim_prefix("dock_").is_valid_int())


## A pipeline's shipping dock ids: "dock" plus the extra ones placed in build mode.
static func docks_of(p: Dictionary) -> Array:
	var out: Array = ["dock"]
	for d in p.get("docks", []):
		if d is Dictionary:
			out.append(str(d.get("id", "")))
	return out


## True for a storage ("store_1", or as a footprint key "build:store_1"): it watches one
## source (Jira issues, GitHub issues, open PRs...) and feeds the intake it's belted into.
static func is_storage(key: String) -> bool:
	var place := key.get_slice(":", key.get_slice_count(":") - 1)
	return place.begins_with("store_") and place.trim_prefix("store_").is_valid_int()


## A pipeline's storages: [{id, source, query, pos}].
static func storages_of(p: Dictionary) -> Array:
	var raw = p.get("storages", [])
	return (raw as Array).filter(func(s) -> bool: return s is Dictionary) if raw is Array else []


## The storages whose belt runs into this pipeline's intake.
static func fed_storages(p: Dictionary) -> Array:
	var into := {}
	for b in p.get("belts", []):
		if str(b.get("port", "")) == "out" and str(b.get("to", "")) in ["intake", "%s:intake" % str(p.get("id", ""))]:
			into[str(b.get("from", ""))] = true
	return storages_of(p).filter(func(s) -> bool: return into.has(str(s.get("id", ""))))


## Where a line's crates come from: typed specs if its intake ticks them, and the source of
## each storage belted into it (layouts saved before storages: the intake's ticks).
static func line_sources(p: Dictionary) -> Array:
	var raw = p.get("sources", [p.get("source", "game")])
	var own: Array = raw if raw is Array else []
	if not p.has("storages"):
		return own
	var out: Array = ["game"] if "game" in own else []
	for st in fed_storages(p):
		out.append(str(st.get("source", "")))
	return out


## A pipeline's scrap bin ids: "scrap" plus the extra ones placed in build mode.
static func bins_of(p: Dictionary) -> Array:
	var out: Array = ["scrap"]
	for b in p.get("bins", []):
		if b is Dictionary:
			out.append(str(b.get("id", "")))
	return out
const STORAGE_NAMES := {"github": "GitHub issues", "jira": "Jira issues", "github_pr": "Open PRs",
		"pr_review": "PRs to review", "prd": "PRDs"}
const STORAGE_ICONS := {"github": "icon_github", "jira": "icon_jira", "github_pr": "icon_pr",
		"pr_review": "icon_review_request", "prd": "icon_prd"}
const STORAGE_DEFAULT_FILTERS := {"github": "labelled factory", "jira": "the factory's JQL",
		"github_pr": "author:@me", "pr_review": "waiting for my review", "prd": "labelled prd"}
const SOURCE_SHORT := {"game": "Typed", "github": "GitHub", "jira": "Jira", "pr_review": "PR reviews", "prd": "PRDs", "github_pr": "Open PRs"}
const ROUTE_MARGIN := 5  # how far outside the machines a belt may wander
const BRIDGE_COST := 4  # a belt bridges over another only when going round costs more

const COLOR_TEXT := Color(0.92, 0.92, 0.92)
const COLOR_QUIET := Color(0.65, 0.66, 0.7)
const COLOR_AMBER := Color("F2B84C")
const COLOR_PAUSED := Color("5AB0FF")  # paused by you
const COLOR_HELD := Color("8CBFFF")  # crates waiting on GitHub
const COLOR_PASS := Color("5ABF66")
const COLOR_FAIL := Color("E5484D")
const STATE_COLORS := {
	"idle": Color("80858F"),
	"running": Color("4C8DF2"),
	"done": Color("5ABF66"),
	"failed": Color("E5484D"),
}
# Animation (see assets/README.md). Every belt tile shows the same frame from one clock.
# The belt surface moves 2 px a frame (assets/README.md): fps = speed in px/s / 2, so crates,
# which ride at the same speed, sit still on the chevrons.
var belt_fps := 48.0
const BELT_FRAMES := 8
const RUN_FPS := 8.0
const RUN_FRAMES := 8
const IDLE_FPS := 2.0
const IDLE_FRAMES := 2
# Finished machines with result art (machines/anim/<sprite>_<result>_fN.svg) show it until
# their next job: the CI monitor lights green or red. Others keep their idle loop.
const STATE_RESULTS := {"done": "pass", "failed": "fail"}
const OVERLAYS := {"running": "overlay_running", "done": "overlay_done", "failed": "overlay_failed"}

var pipelines: Array = []  # the graphs being shown (belt paths are filled in by routing)
var catalog := {}  # type -> catalogue summary (sprite, steps, description...)
var points := {}  # location -> Vector2 (world position where crates sit)
var footprints := {}  # machine id or "<pid>:<station>" -> Vector2i top-left cell
var owner_of := {}  # footprint key -> pipeline id
var machines := {}  # id -> {name, sprite, steps, gate, state, progress, step, waiting, pipeline}
var order := {}  # pipeline id -> machine ids in travel order (following pass belts, up to a sorter)
var route_order := {}  # "<pipeline>@<route>" -> machine ids along that way through its sorters
var depth := {}  # machine id -> how many machines a crate passes before it (for belt order)
var routes: Array = []  # one per belt: {pid, from, port, to, kind, cells, ok, belt}
var belt_cells := {}  # Vector2i -> {"in": [sides], "out": side, "kind": "pass"|"fail", "route": index}
var bounds := Rect2i(0, 0, 12, 10)  # cells in use
var pauses := {"factory": false, "lines": [], "machines": []}  # what you paused
var agent_down := false  # Claude is unavailable: machines that use it are paused

# Build mode
var editing := false
var bin_counts := {}  # scrap bin key -> crates in it (they aren't drawn on the floor)
# Rooms (room_layer.gd): wall blocks on the floor. cell -> {"kind": "wall"|"window"|"door"|"port",
# "axis": "h"|"v"} (the way the wall runs). Walls, windows and doors stop belts and machines; a
# belt port lets one belt straight through, across the wall. room_painter draws room floors.
var room_walls := {}
var room_painter: Object = null
var storage_counts := {}  # storage key -> issues or PRs in it not yet taken (unknown: not there)
var _bin_drops := {}  # scrap bin key -> when its drop animation started (a crate just landed)
var _bins_seen := false  # no drop animation for crates already in the bin at startup
const DROP_FRAMES := 6
const DROP_FPS := 12.0
var highlighted := ""  # footprint key outlined in build mode
var highlighted_route := -1
var group := {}  # footprint keys picked together in build mode: drawn outlined
var group_cells := {}  # room block cells picked with them (Vector2i -> true)
var select_box := Rect2()  # the box being dragged to pick them (world), or empty
var ghost := {}  # {"pos": Vector2i, "ok": bool} where a machine is being placed or moved
var wire := {}  # {"from": Vector2, "to": Vector2, "kind": "pass"|"fail"} a belt being dragged

var _machine_layer: CanvasItem
var _textures := {}

# Belts run while crates are in the line; a stopped belt holds its frame.
var belts_running := false
var return_running := false
var _belt_frame := 0
var _return_frame := 0
var _machine_tick := -1


## Belt speed in cells a second (the backend's setting): the animation runs to match.
func set_belt_speed(cells_per_second: float) -> void:
	belt_fps = maxf(1.0, cells_per_second * CELL / 2.0)


## main: crates are in the line. back: a crate is riding a rework belt.
func set_belt_motion(main: bool, back: bool) -> void:
	belts_running = main
	return_running = back


func _process(_delta: float) -> void:
	var t := Time.get_ticks_msec() / 1000.0
	var clock := int(t * belt_fps) % BELT_FRAMES
	var floor_changed := false
	if belts_running and clock != _belt_frame:
		_belt_frame = clock
		floor_changed = true
	# Red belts run with the rest of the floor (and on their own while a crate rides one back).
	if (return_running or belts_running) and clock != _return_frame:
		_return_frame = clock
		floor_changed = true
	if floor_changed:
		queue_redraw()
	# Machines: running loops at 8 fps and idle blinks at 2 fps; 8 ticks/s covers both.
	var tick := int(t * RUN_FPS)
	if tick != _machine_tick:
		_machine_tick = tick
		if _machine_layer:
			_machine_layer.queue_redraw()
	if not _bin_drops.is_empty():
		_redraw()


## The canvas that machines are drawn on (add it above the crates).
func attach_machine_layer(layer: CanvasItem) -> void:
	_machine_layer = layer
	layer.draw.connect(_draw_machines)


## pipeline_list: the backend's layout.pipelines (or a draft from build mode). Belts with no
## path, or one that no longer fits, are routed again and their "path" is updated in place.
func set_pipelines(pipeline_list: Array, catalog_list: Array = []) -> void:
	if not catalog_list.is_empty():
		catalog.clear()
		for t in catalog_list:
			catalog[str(t.get("type", t.get("id", "")))] = t
	pipelines = pipeline_list
	var old := machines
	points.clear()
	footprints.clear()
	owner_of.clear()
	order.clear()
	machines = {}

	for p in pipelines:
		var pid := str(p.get("id", ""))
		for station in STATIONS:
			if not p.get(station) is Dictionary:
				continue  # a line can ship and scrap into another line's dock and bin
			var key := "%s:%s" % [pid, station]
			footprints[key] = _cell(p[station].get("pos", [0, 0]))
			owner_of[key] = pid
		for b in p.get("bins", []) + p.get("docks", []):
			var key := "%s:%s" % [pid, str(b.get("id", ""))]
			footprints[key] = _cell(b.get("pos", [0, 0]))
			owner_of[key] = pid
		for st in storages_of(p):
			var key := "%s:%s" % [pid, str(st.get("id", ""))]
			footprints[key] = _cell(st.get("pos", [0, 0]))
			owner_of[key] = pid
		for m in p.get("machines", []):
			var id := str(m.get("id", ""))
			footprints[id] = _cell(m.get("pos", [0, 0]))
			owner_of[id] = pid
			var base: Dictionary = catalog.get(str(m.get("type", "")), {})
			var previous: Dictionary = old.get(id, {})
			var name := str(m.get("name", ""))
			var steps = m.get("steps", base.get("steps", []))
			machines[id] = {
				"name": name if name != "" else str(base.get("name", id)),
				"sprite": str(m.get("sprite", base.get("sprite", "coder"))),
				"steps": steps.size() if steps is Array else 0,
				"gate": gated(m, base),
				"auto_approve": str(m.get("auto_approve", "")),
				"uses_agent": bool(m.get("uses_agent", base.get("uses_agent", false))),
				"sorter": bool(m.get("sorter", base.get("sorter", false))),
				"kind": str(m.get("kind", base.get("kind", ""))),
				"pipeline": pid,
				"state": previous.get("state", "idle"),
				"progress": previous.get("progress", 0.0),
				"step": previous.get("step", ""),
				"waiting": previous.get("waiting", 0),
				"result": previous.get("result", ""),
			}
	route_order.clear()
	for p in pipelines:
		order[str(p.get("id", ""))] = _chain(p)
	_find_depths()
	_route_all()
	_place_points()
	_redraw()


func _cell(raw) -> Vector2i:
	if raw is Array and raw.size() == 2:
		return Vector2i(int(raw[0]), int(raw[1]))
	return Vector2i.ZERO


## Machine ids in the order crates visit them: pass belts from the intake. At a sorter it
## follows the output in route ({sorter id: port}), or stops there.
func _chain(p: Dictionary, route := {}) -> Array:
	var outs := all_outputs()
	var out: Array = []
	var at := str(outs.get("%s:intake|out" % str(p.get("id", "")), ""))
	while machines.has(at) and not at in out:
		out.append(at)
		at = str(outs.get("%s|%s" % [at, next_port(at, route)], ""))
	return out


## Whether a placed machine waits for approval: the Approver always, the Reviewer never, the
## rest when their "wait for my approval" tick is on.
static func gated(m: Dictionary, base: Dictionary) -> bool:
	match str(base.get("gate_setting", "choice")):
		"always":
			return true
		"never":
			return false
	return bool(m.get("gate", base.get("gate", false)))


func is_sorter(id: String) -> bool:
	return bool(machines.get(id, {}).get("sorter", false))


## A splitter: a sorter with two outputs on its east side that takes turns.
func is_splitter(id: String) -> bool:
	return str(machines.get(id, {}).get("kind", "")) == "splitter"


## A belt merger: belts into either of its inputs, one belt out.
func is_merger(id: String) -> bool:
	return str(machines.get(id, {}).get("kind", "")) == "merger"


## The output a crate leaves machine id by: "pass", the one its route picked at a sorter, or
## "fail" when it failed there and the red belt led on.
func next_port(id: String, route: Dictionary) -> String:
	if str(route.get(id, "")) == "fail":
		return "fail"
	return str(route.get(id, "")) if is_sorter(id) else "pass"


## A line key is a pipeline id, or "<pipeline>@<route>" for crates a sorter sent one way, or
## that left a machine by a red belt that leads on ("build@sort_1=out_3,tester=fail"): the
## backend names their places that way.
static func base_of(key: String) -> String:
	return key.get_slice("@", 0)


static func route_of_key(key: String) -> Dictionary:
	var route := {}
	if not "@" in key:
		return route
	for part in key.get_slice("@", 1).split(","):
		if "=" in part:
			route[part.get_slice("=", 0)] = part.get_slice("=", 1)
	return route


## The machines along a line key, in travel order.
func chain_for(key: String) -> Array:
	if not "@" in key:
		return order.get(key, [])
	if not route_order.has(key):
		var p := _pipeline_of(base_of(key))
		route_order[key] = _chain(p, route_of_key(key)) if not p.is_empty() else []
		_place_line_points(key, route_order[key])
	return route_order[key]


func _pipeline_of(pid: String) -> Dictionary:
	for p in pipelines:
		if str(p.get("id", "")) == pid:
			return p
	return {}


## The belt in front of the i-th machine of a line: [from, port].
func _belt_source(key: String, chain: Array, i: int) -> Array:
	if i == 0:
		return ["intake", "out"]
	if i - 1 >= chain.size():
		return ["", ""]
	var prev := str(chain[i - 1])
	return [prev, next_port(prev, route_of_key(key))]


## How far along the line each machine is (every way through sorters), for belt order.
func _find_depths() -> void:
	depth.clear()
	var outs := all_outputs()
	for p in pipelines:
		var at := str(outs.get("%s:intake|out" % str(p.get("id", "")), ""))
		var queue: Array = [[at, 0]] if machines.has(at) else []
		while not queue.is_empty():
			var next: Array = queue.pop_front()
			var id := str(next[0])
			if depth.has(id) and int(depth[id]) <= int(next[1]):
				continue
			depth[id] = int(next[1])
			var ports: Array = SORTER_PORTS.keys() if is_sorter(id) else ["pass", "fail"]
			for port in ports:
				var to := str(outs.get("%s|%s" % [id, port], ""))
				if machines.has(to) and int(next[1]) < 200:
					queue.append([to, int(next[1]) + 1])


## The machine a crate on this belt is waiting in front of, or "".
func machine_ahead(location: String) -> String:
	if not ":belt_" in location:
		return ""
	var key := location.get_slice(":", 0)
	var chain := chain_for(key)
	var i := int(location.get_slice(":", 1).trim_prefix("belt_"))
	return str(chain[i]) if i >= 0 and i < chain.size() else ""


## Every belt in the factory as "from|port" -> to, with stations named with their pipeline
## ("build:dock"): belts can run from one line into another's machines, dock or bin.
func all_outputs() -> Dictionary:
	var outs := {}
	for q in pipelines:
		var qid := str(q.get("id", ""))
		for b in q.get("belts", []):
			outs["%s|%s" % [node_key(qid, str(b.get("from", ""))), b.get("port", "")]] = node_key(qid, str(b.get("to", "")))
	return outs


## Where a line's crates ship: the dock its last belt runs into ("build:dock"). key: a
## pipeline id or a line key with a route.
func end_of(key: String) -> String:
	var pid := base_of(key)
	var chain: Array = chain_for(key)
	var outs := all_outputs()
	var last := ("%s|%s" % [chain[-1], next_port(str(chain[-1]), route_of_key(key))]) if not chain.is_empty() \
			else ("%s:intake|out" % pid)
	var at := str(outs.get(last, ""))
	return at if is_dock(at) or is_bin(at) else "%s:dock" % pid


# Ports -----------------------------------------------------------------------

## The footprint key for a belt end ("intake"/"dock" mean this pipeline's stations).
func node_key(pid: String, node: String) -> String:
	if ":" in node:
		return node  # already names its pipeline: a belt into another line's dock or bin
	return "%s:%s" % [pid, node] if node in STATIONS or is_bin(node) or is_dock(node) or is_storage(node) else node


## The cell just outside a node's output port, where a belt starts.
func out_cell(key: String, port: String) -> Vector2i:
	var tl: Vector2i = footprints.get(key, Vector2i.ZERO)
	if SORTER_PORTS.has(port):
		return tl + SORTER_PORTS[port][0]
	return tl + Vector2i(2, 1 if port == "fail" else 0)


## The way a belt leaves this port (index into DIR_NAMES): east, or a sorter's side.
func port_dir(port: String) -> int:
	return int(SORTER_PORTS[port][1]) if SORTER_PORTS.has(port) else 1


## True if the cell is under a machine, station, bin or storage.
func under_node(c: Vector2i) -> bool:
	for key in footprints:
		if Rect2i(footprints[key], Vector2i(2, 2)).has_point(c):
			return true
	return false


## The cell just outside a node's input, where a belt ends.
func in_cell(key: String, kind: String) -> Vector2i:
	var tl: Vector2i = footprints.get(key, Vector2i.ZERO)
	return tl + Vector2i(-1, 1 if kind == "fail" else 0)


## World positions of a node's output ports: {port: Vector2}. Used to draw and to grab them.
func output_ports(key: String) -> Dictionary:
	if not footprints.has(key):
		return {}
	var tl := Vector2(footprints[key]) * CELL
	if key.ends_with(":intake") or is_storage(key):
		return {"out": tl + Vector2(2 * CELL, 0.5 * CELL)}
	if is_dock(key) or is_bin(key):
		return {}
	if is_splitter(key):
		return {"out_3": tl + Vector2(2 * CELL, 0.5 * CELL), "out_4": tl + Vector2(2 * CELL, 1.5 * CELL)}
	if is_merger(key):
		return {"pass": tl + Vector2(2 * CELL, 0.5 * CELL)}
	if is_sorter(key):
		return {"out_1": tl + Vector2(0.5 * CELL, 0), "out_2": tl + Vector2(1.5 * CELL, 0),
				"out_3": tl + Vector2(2 * CELL, 0.5 * CELL), "out_4": tl + Vector2(2 * CELL, 1.5 * CELL),
				"out_5": tl + Vector2(0.5 * CELL, 2 * CELL), "out_6": tl + Vector2(1.5 * CELL, 2 * CELL)}
	return {"pass": tl + Vector2(2 * CELL, 0.5 * CELL), "fail": tl + Vector2(2 * CELL, 1.5 * CELL)}


# Routing ---------------------------------------------------------------------

func _route_all() -> void:
	routes.clear()
	belt_cells.clear()
	var solid := {}  # cells under machines and stations
	var first := true
	for key in footprints:
		for c in _footprint_cells(footprints[key]):
			solid[c] = true
		var r := Rect2i(footprints[key], Vector2i(2, 2))
		bounds = r if first else bounds.merge(r)
		first = false
	if first:
		bounds = Rect2i(0, 0, 12, 10)
	for c in room_walls:
		bounds = bounds.merge(Rect2i(c, Vector2i.ONE))
		if str(room_walls[c].get("kind", "")) != "port":
			solid[c] = true
	if room_painter and room_painter.has_method("floor_rect"):
		var fr: Rect2i = room_painter.floor_rect()
		if fr.has_area():
			bounds = bounds.merge(fr)

	# Free conveyors laid by hand first: they sit where you put them, and links route round
	# (or over) them.
	for joining in [false, true]:  # ones that run into another's side after the rest
		for p in pipelines:
			for conv in conveyors_of(p):
				if bool(conv.get("join", false)) == joining:
					_lay_conveyor(str(p.get("id", "")), conv, solid)

	for p in pipelines:
		var pid := str(p.get("id", ""))
		# Port cells in use are kept free for their own belts.
		var reserved := {}
		var belts: Array = p.get("belts", [])
		for b in belts:
			var kind := "fail" if str(b.get("port", "")) == "fail" else "pass"
			reserved[out_cell(node_key(pid, str(b.get("from", ""))), str(b.get("port", "")))] = true
			reserved[in_cell(node_key(pid, str(b.get("to", ""))), kind)] = true
		# Pass belts first, in travel order, then the rework belts, then the belts into bins.
		var sorted := belts.duplicate()
		sorted.sort_custom(func(a, b) -> bool: return _belt_rank(pid, a) < _belt_rank(pid, b))
		for b in sorted:
			_route_belt(pid, b, solid, reserved)


## A pipeline's free conveyors: [{"id": "conv_1", "cells": [[x, y], ...], "loop": bool}]. Belt
## laid by hand that doesn't (yet) join an output to an input: it runs, but no crate rides it.
static func conveyors_of(p: Dictionary) -> Array:
	var raw = p.get("conveyors", [])
	return (raw as Array).filter(func(c) -> bool: return c is Dictionary) if raw is Array else []


func _lay_conveyor(pid: String, conv: Dictionary, solid: Dictionary) -> void:
	var cells: Array = []
	for c in conv.get("cells", []):
		if c is Array and c.size() == 2:
			var cell := Vector2i(int(c[0]), int(c[1]))
			if solid.has(cell) or (cells.size() > 0 and (cell - cells[-1]).length_squared() != 1):
				break  # a machine or wall landed on it since: it stops short
			cells.append(cell)
	# It may bridge straight over another conveyor; anywhere else on one, it stops short.
	var joins := bool(conv.get("join", false))
	for i in cells.size():
		if joins and i == cells.size() - 1 and i > 0 and belt_cells.has(cells[i]):
			break  # runs into another conveyor's side there
		if belt_cells.has(cells[i]) and not can_cross(cells, i):
			cells = cells.slice(0, i)
			break
	if cells.is_empty():
		return
	var loop: bool = bool(conv.get("loop", false)) and cells.size() >= 4 \
			and (cells[0] - cells[-1]).length_squared() == 1
	var route := {"pid": pid, "from": "", "port": "", "to": "", "kind": "pass", "cells": cells, "ok": true,
			"belt": conv, "conveyor": str(conv.get("id", "")), "dir": 1}
	if loop:
		route["entry"] = _side(cells[0], cells[-1])
		route["exit"] = _side(cells[-1], cells[0])
	elif cells.size() > 1:
		route["entry"] = _side(cells[0], cells[0] - (cells[1] - cells[0]))
		route["exit"] = _side(cells[-1], cells[-1] + (cells[-1] - cells[-2]))
	routes.append(route)
	_mark_cells(routes.size() - 1, route)
	for c in cells:
		bounds = bounds.merge(Rect2i(c, Vector2i.ONE))


func _belt_rank(pid: String, b: Dictionary) -> int:
	var from := str(b.get("from", ""))
	if is_storage(from):
		return -2  # into the intake, before the line's own belts
	var index := -1 if from == "intake" else int(depth.get(from, 900))
	if b.get("join", false) and b.get("manual", false):
		index += 5000  # laid by hand into another belt's side: after that belt
	if str(b.get("port", "")) != "fail":
		return index
	# Rework belts before the ones into a scrap bin: those can join each other on the way to
	# the bin, while a rework belt needs its own way in to the machine it goes back to.
	return index + (2000 if is_bin(str(b.get("to", ""))) else 1000)


func _route_belt(pid: String, b: Dictionary, solid: Dictionary, reserved: Dictionary) -> void:
	var from_key := node_key(pid, str(b.get("from", "")))
	var to_key := node_key(pid, str(b.get("to", "")))
	var port := str(b.get("port", "pass"))
	var kind := "fail" if port == "fail" else "pass"
	var route := {"pid": pid, "from": str(b.get("from", "")), "port": port, "to": str(b.get("to", "")),
			"kind": kind, "cells": [], "ok": false, "belt": b, "dir": port_dir(port)}
	if not footprints.has(from_key) or not footprints.has(to_key):
		routes.append(route)
		return
	var start := out_cell(from_key, port)
	var in_kind := _in_kind(pid, b, to_key, kind)
	route["in_kind"] = in_kind
	var goal := in_cell(to_key, in_kind)
	# A rework belt may end by joining another one that goes to the same machine.
	# A belt may end by joining another one that goes into the same place: several rework belts
	# into one machine, or several lines feeding one machine or dock.
	var joins := {}
	for other in routes:
		if other["ok"] and str(other.get("in_kind", other["kind"])) == in_kind and node_key(other["pid"], other["to"]) == to_key:
			for c in other["cells"]:
				joins[c] = true
	var cells := _stored_path(b)
	if not _path_fits(cells, start, goal, joins, solid, reserved):
		b.erase("manual")  # a belt laid by hand that no longer fits is routed like any other
		cells = _find_path(start, goal, joins, solid, reserved, ROUTE_MARGIN, route["dir"])
		if cells.is_empty():  # boxed in nearby: look further out before giving up
			cells = _find_path(start, goal, joins, solid, reserved, ROUTE_MARGIN * 4, route["dir"])
	if not cells.is_empty():
		route["cells"] = cells
		route["ok"] = true
		var saved: Array = []
		for c in cells:
			saved.append([c.x, c.y])
		b["path"] = saved
		_mark_cells(routes.size(), route)
		for c in cells:
			bounds = bounds.merge(Rect2i(c, Vector2i.ONE))
	else:
		b.erase("path")
	routes.append(route)


## Which input a belt goes into: a rework belt into the lower one, the rest into the upper.
## A belt merger has two inputs alike: a belt laid by hand keeps the one it was laid to, and
## the others share them out.
func _in_kind(_pid: String, b: Dictionary, to_key: String, kind: String) -> String:
	if not is_merger(to_key):
		return kind
	var path := _stored_path(b)
	if not path.is_empty():
		var owner := int(belt_cells.get(path[-1], {}).get("route", -1))
		if owner >= 0 and node_key(routes[owner]["pid"], routes[owner]["to"]) == to_key:
			return str(routes[owner].get("in_kind", routes[owner]["kind"]))  # joins that belt
		if path[-1] == in_cell(to_key, "fail"):
			return "fail"
		if path[-1] == in_cell(to_key, "pass"):
			return "pass"
	var upper := 0
	var lower := 0
	for other in routes:
		if other["ok"] and node_key(other["pid"], other["to"]) == to_key:
			if str(other.get("in_kind", other["kind"])) == "fail":
				lower += 1
			else:
				upper += 1
	return "fail" if upper > lower else "pass"


func _stored_path(b: Dictionary) -> Array:
	var out: Array = []
	var raw = b.get("path", [])
	if raw is Array:
		for c in raw:
			if c is Array and c.size() == 2:
				out.append(Vector2i(int(c[0]), int(c[1])))
	return out


func _path_fits(cells: Array, start: Vector2i, goal: Vector2i, joins: Dictionary, solid: Dictionary,
		reserved: Dictionary) -> bool:
	if cells.is_empty() or cells[0] != start:
		return false
	var last: Vector2i = cells[-1]
	if last != goal and not joins.has(last):
		return false
	for i in cells.size():
		var c: Vector2i = cells[i]
		if i > 0 and (c - cells[i - 1]).length_squared() != 1:
			return false
		if room_walls.has(c) and (i == 0 or not _port_ok(c, c - cells[i - 1])
				or (i < cells.size() - 1 and cells[i + 1] - c != c - cells[i - 1])):
			return false
		var is_join := i == cells.size() - 1 and joins.has(c)
		var bridges: bool = belt_cells.has(c) and not is_join and i > 0 and i < cells.size() - 1 \
				and (cells[i + 1] - c) == (c - cells[i - 1]) and _can_bridge(c, _side(cells[i - 1], c))
		if solid.has(c) or (belt_cells.has(c) and not is_join and not bridges):
			return false
		if reserved.has(c) and c != start and c != goal:
			return false
	return true


func _blocked(c: Vector2i, start: Vector2i, goal: Vector2i, joins: Dictionary, solid: Dictionary,
		reserved: Dictionary) -> bool:
	if solid.has(c):
		return true
	if belt_cells.has(c) and not joins.has(c):
		return true
	return reserved.has(c) and c != start and c != goal


## Cheapest path from start to goal (or onto a joinable belt): each step costs 1 and each
## turn 1 more, so belts run straight where they can. Returns cells, or [] if there is no room.
## A state is Vector3i(x, y, d): the cell and the way it was entered (index into DIR_NAMES);
## d + 4 marks "at the goal, turned to face the machine".
func _find_path(start: Vector2i, goal: Vector2i, joins: Dictionary, solid: Dictionary,
		reserved: Dictionary, margin := ROUTE_MARGIN, dir := 1) -> Array:
	if _blocked(start, start, goal, joins, solid, reserved):
		return []
	var area := bounds.merge(Rect2i(start, Vector2i.ONE)).merge(Rect2i(goal, Vector2i.ONE)).grow(margin)
	var first := Vector3i(start.x, start.y, dir)  # out of the port (east, or a sorter's side)
	var buckets := {0: [first]}  # Dial's algorithm: states grouped by cost
	var best := {first: 0}
	var came := {}
	var max_cost := area.size.x * area.size.y * 2 + 4
	var found = null
	var cost := 0
	while cost <= max_cost and found == null:
		var bucket: Array = buckets.get(cost, [])
		buckets.erase(cost)
		for state in bucket:
			var c := Vector2i(state.x, state.y)
			if state.z >= 4:
				found = Vector3i(state.x, state.y, state.z - 4)
				break
			if int(best.get(state, -1)) != cost:
				continue
			if c == goal:
				if state.z == 1:  # already facing the machine
					found = state
					break
				_push(buckets, cost + 1, Vector3i(c.x, c.y, state.z + 4))
				continue
			if joins.has(c) and c != start:
				found = state
				break
			var on_bridge := belt_cells.has(c) and c != start
			var in_port := room_walls.has(c) and c != start
			for nd in 4:
				if nd == (state.z + 2) % 4 or ((on_bridge or in_port) and nd != state.z):
					continue  # no reversing; over a bridge or through a belt port, straight on
				var n: Vector2i = c + DIRS[DIR_NAMES[nd]]
				if not area.has_point(n):
					continue
				if room_walls.has(n) and not _port_ok(n, DIRS[DIR_NAMES[nd]]):
					continue  # a wall, or a belt port crossed the wrong way
				var extra := 0
				if _blocked(n, start, goal, joins, solid, reserved):
					# Another belt in the way: cross it on a bridge if it runs straight across.
					if solid.has(n) or n == goal or (reserved.has(n) and n != start) or not belt_cells.has(n) \
							or joins.has(n) or not _can_bridge(n, DIR_NAMES[nd]):
						continue
					extra = BRIDGE_COST
				var step := cost + 1 + extra + (0 if nd == state.z else 1)
				var key := Vector3i(n.x, n.y, nd)
				if best.has(key) and int(best[key]) <= step:
					continue
				best[key] = step
				came[key] = state
				_push(buckets, step, key)
		cost += 1
	if found == null:
		return []
	var cells: Array = []
	var at = found
	while at != null:
		cells.push_front(Vector2i(at.x, at.y))
		at = came.get(at, null)
	return cells


## Whether a belt laid along `cells` can go over the belt on cells[i] on a conveyor bridge: it
## runs straight through that cell (not its first or last) across a straight stretch.
func can_cross(cells: Array, i: int) -> bool:
	if i <= 0 or i >= cells.size() - 1:
		return false
	var c: Vector2i = cells[i]
	if cells[i + 1] - c != c - cells[i - 1]:
		return false
	return _can_bridge(c, _side(c, cells[i + 1]))


## Whether a belt moving `dir` can cross the belt on cell c on a bridge: that belt runs
## straight across this way (not a corner or merge) and isn't bridged already.
func _can_bridge(c: Vector2i, dir: String) -> bool:
	var cell: Dictionary = belt_cells.get(c, {})
	if cell.is_empty() or cell.has("over") or (cell["in"] as Array).size() != 1 or room_walls.has(c):
		return false
	var out := str(cell["out"])
	if str(cell["in"][0]) != OPPOSITE[out]:
		return false
	return dir != out and dir != OPPOSITE[out]


func _push(buckets: Dictionary, cost: int, state: Vector3i) -> void:
	if not buckets.has(cost):
		buckets[cost] = []
	buckets[cost].append(state)


func _mark_cells(index: int, route: Dictionary) -> void:
	var cells: Array = route["cells"]
	for i in cells.size():
		var c: Vector2i = cells[i]
		var entry: String = OPPOSITE[DIR_NAMES[int(route.get("dir", 1))]] if i == 0 else _side(c, cells[i - 1])
		if i == 0 and route.has("entry"):
			entry = str(route["entry"])  # a free conveyor: straight in, or round from its own end
		var exit := "e"
		if i < cells.size() - 1:
			exit = _side(c, cells[i + 1])
		elif route.has("exit"):
			exit = str(route["exit"])
		if belt_cells.has(c):
			var existing: Dictionary = belt_cells[c]
			if i < cells.size() - 1 and _can_bridge(c, exit):
				# Crossing another belt: this one goes over it on a bridge.
				existing["over"] = {"in": entry, "out": exit, "kind": route["kind"], "route": index}
				continue
			# Joining another belt to the same place: this cell has its piece; add the side.
			if not entry in existing["in"]:
				existing["in"].append(entry)
			continue
		belt_cells[c] = {"in": [entry], "out": exit, "kind": route["kind"], "route": index}


## The side of cell a that cell b is on.
func _side(a: Vector2i, b: Vector2i) -> String:
	for name in DIRS:
		if a + DIRS[name] == b:
			return name
	return "e"


## Room walls changed: route the belts again round them (and through belt ports).
func set_room_walls(walls: Dictionary) -> void:
	room_walls = walls
	_route_all()
	_place_points()
	_redraw()


## True when a room block (wall, window, door or belt port) stands on cell c.
func is_wall(c: Vector2i) -> bool:
	return room_walls.has(c)


## Whether a belt may step onto cell c moving `step`: anywhere but a room block, and through a
## belt port only across its wall.
func _port_ok(c: Vector2i, step: Vector2i) -> bool:
	var w: Dictionary = room_walls.get(c, {})
	if w.is_empty():
		return true
	if str(w.get("kind", "")) != "port":
		return false
	return step.x == 0 if str(w.get("axis", "h")) == "h" else step.y == 0


func _footprint_cells(tl: Vector2i) -> Array:
	return [tl, tl + Vector2i(1, 0), tl + Vector2i(0, 1), tl + Vector2i(1, 1)]


## Belts that couldn't be laid (no room): routes with ok == false.
func broken_belts() -> Array:
	return routes.filter(func(r) -> bool: return not r["ok"])


## The route index of the belt on this cell, or -1.
func route_at(c: Vector2i) -> int:
	return int(belt_cells[c]["route"]) if belt_cells.has(c) else -1


# Locations -------------------------------------------------------------------

func _place_points() -> void:
	for p in pipelines:
		var pid := str(p.get("id", ""))
		var chain: Array = order.get(pid, [])
		if footprints.has("%s:intake" % pid):
			points["%s:intake" % pid] = _center(footprints["%s:intake" % pid])
		# Shipped crates stack up just past the dock, scrapped ones beside the bin.
		for dock in docks_of(p):
			if footprints.has("%s:%s" % [pid, dock]):
				points["%s:%s" % [pid, dock]] = cell_center(footprints["%s:%s" % [pid, dock]] + Vector2i(2, 1))
		for bin in bins_of(p):
			if footprints.has("%s:%s" % [pid, bin]):
				points["%s:%s" % [pid, bin]] = cell_center(footprints["%s:%s" % [pid, bin]] + Vector2i(2, 1))
		for id in chain:
			points[id] = _center(footprints[id])
		for id in machines:
			if not points.has(id):
				points[id] = _center(footprints[id])  # not connected yet
		_place_line_points(pid, chain)


## Where crates sit on a line's belts: "<key>:belt_<i>", "<key>:return_<i>" and
## "<key>:rework_in_<i>" (key: a pipeline id, or one with a route through its sorters).
func _place_line_points(key: String, chain: Array) -> void:
	var pid := base_of(key)
	for i in chain.size() + 1:
		var to := end_of(key) if i == chain.size() else str(chain[i])
		var src := _belt_source(key, chain, i)
		var cells := _cells_of(pid, src[0], src[1])
		points["%s:belt_%d" % [key, i]] = (cell_center(cells[-1]) if not cells.is_empty()
				else _center(footprints.get(node_key(pid, to), Vector2i.ZERO)) - Vector2(CELL, 0))
	for i in chain.size():
		var path := belt_points(pid, str(chain[i]), "fail")
		if path.is_empty():
			continue
		points["%s:return_%d" % [key, i]] = path[0]
		var target := chain.find(str(_route_of(pid, str(chain[i]), "fail").get("to", "")))
		if target >= 0 and target <= i:  # back for rework (a red belt that leads on is belt_<i+1>)
			points["%s:return_%d" % [key, target]] = path[-1]
			points["%s:rework_in_%d" % [key, target]] = path[-1]


func _route_of(pid: String, from: String, port: String) -> Dictionary:
	for r in routes:
		# Machine ids are unique across lines: a line's chain can run on through another's.
		if (r["pid"] == pid or machines.has(from)) and r["from"] == from and r["port"] == port:
			return r
	return {}


func _cells_of(pid: String, from: String, port: String) -> Array:
	var r := _route_of(pid, from, port)
	return r.get("cells", []) if r.get("ok", false) else []


## World points along a belt, from its first cell to where it ends (following a join).
func belt_points(pid: String, from: String, port: String) -> Array:
	var r := _route_of(pid, from, port)
	var out: Array = []
	if not r.get("ok", false):
		return out
	var cells: Array = r["cells"]
	for c in cells:
		out.append(cell_center(c))
	# Joined another belt (maybe one that joins a third): carry on along it to where it ends.
	var used := [r]
	var last: Vector2i = cells[-1]
	for _hop in 8:
		var next: Dictionary = {}
		var owner := int(belt_cells.get(last, {}).get("route", -1))
		if owner >= 0 and not routes[owner] in used and routes[owner]["cells"].find(last) < routes[owner]["cells"].size() - 1:
			next = routes[owner]
		else:
			for other in routes:  # the cell's owner may be this belt: look for the one it runs on
				if other in used or not other.get("ok", false):
					continue
				var at: int = other["cells"].find(last)
				if at >= 0 and at < other["cells"].size() - 1:
					next = other
					break
		if next.is_empty():
			break
		used.append(next)
		var more: Array = next["cells"]
		for k in range(more.find(last) + 1, more.size()):
			out.append(cell_center(more[k]))
		last = more[-1]
	return out


## The pipeline a location belongs to, and the place in it ("belt_2", "coder"...).
func split_location(location: String) -> Array:
	if ":" in location:
		return [location.get_slice(":", 0), location.get_slice(":", 1)]
	return [str(machines.get(location, {}).get("pipeline", "")), location]


## Where a crate moving from one location to another goes, as waypoints. A crate leaving a
## machine slides (hidden inside it) to the row of the port it leaves by, so it comes out of
## that port; one going into a machine goes in at its input, then to the middle.
func waypoints(from: String, to: String, at := Vector2.INF) -> Array:
	var path := _waypoints(from, to)
	if path.is_empty():
		return path
	var out_of := _footprint_key(from)
	if not out_of.is_empty():
		var box := Rect2(Vector2(footprints[out_of]) * CELL, Vector2(2, 2) * CELL)
		var first: Vector2 = path[0]
		if not box.has_point(first):
			var mid := box.get_center()
			# Straight out of the side the port is on: east or west, or a sorter's north or south.
			var inside := Vector2(mid.x, first.y) if absf(first.x - mid.x) >= absf(first.y - mid.y) \
					else Vector2(first.x, mid.y)
			path.push_front(inside.clamp(box.position, box.end))
	var into := _footprint_key(to)
	if not into.is_empty() and path.size() == 1 and at != Vector2.INF:
		# Straight in from where it waits (the input, or the rework belt's end), then to the middle.
		var box := Rect2(Vector2(footprints[into]) * CELL, Vector2(2, 2) * CELL).grow(-CELL * 0.25)
		if not box.has_point(at):
			path.push_front(at.clamp(box.position, box.end))
	return path


## The footprint key of a machine or station location, or "" (belts and the like).
func _footprint_key(location: String) -> String:
	if footprints.has(location):
		return location
	return ""


func _waypoints(from: String, to: String) -> Array:
	var a := split_location(from)
	var b := split_location(to)
	var key: String = b[0]
	var pid := base_of(key)
	var chain: Array = chain_for(key)
	var place: String = b[1]
	var prev: String = a[1]
	if place.begins_with("belt_"):
		if prev.begins_with("return_"):
			return [point("%s:rework_in_%s" % [key, place.trim_prefix("belt_")])]
		var i := int(place.trim_prefix("belt_"))
		var src := _belt_source(key, chain, i)
		var path := belt_points(pid, src[0], src[1])
		if not path.is_empty():
			return path
	if is_bin(place) and prev.begins_with("return_"):
		# Along a red belt into the scrap bin (maybe another line's: the crate's line is the one
		# it came from).
		chain = chain_for(a[0]) if a[0] != "" else chain
		var j := int(prev.trim_prefix("return_"))
		if j < chain.size():
			var path := belt_points(pid, str(chain[j]), "fail")
			if not path.is_empty():
				return path
	if place.begins_with("return_"):
		if machines.has(prev):
			var path := belt_points(pid, prev, "fail")
			if not path.is_empty():
				return [path[0]]  # out of the fail port onto the belt
		if prev.begins_with("return_"):
			var j := int(prev.trim_prefix("return_"))
			if j < chain.size():
				var path := belt_points(pid, str(chain[j]), "fail")
				if not path.is_empty():
					return path
	return [point(to)]


## A point on the belt of location ("<key>:belt_<i>"), dist pixels back from where it ends:
## where the dist/spacing-th crate in a queue on that belt waits. Its end when there's no belt.
func back_along(location: String, dist: float) -> Vector2:
	var key := location.get_slice(":", 0)
	var place := location.get_slice(":", 1)
	var end := point(location)
	if not place.begins_with("belt_"):
		return end
	var chain: Array = chain_for(key)
	var src := _belt_source(key, chain, int(place.trim_prefix("belt_")))
	var path := belt_points(base_of(key), src[0], src[1])
	if path.is_empty():
		return end
	var at: Vector2 = path[-1]
	var left := dist
	for k in range(path.size() - 2, -1, -1):
		var step: float = at.distance_to(path[k])
		if step >= left:
			return at.move_toward(path[k], left)
		left -= step
		at = path[k]
	return at


## Whether a location has a place on the floor (placing a sorter route's belts the first time).
func has_point(location: String) -> bool:
	if not points.has(location) and "@" in location.get_slice(":", 0):
		chain_for(location.get_slice(":", 0))
	return points.has(location)


func cell_center(c: Vector2i) -> Vector2:
	return Vector2((c.x + 0.5) * CELL, (c.y + 0.5) * CELL)


func cell_at(world_point: Vector2) -> Vector2i:
	return Vector2i(floori(world_point.x / CELL), floori(world_point.y / CELL))


func point(location: String) -> Vector2:
	if has_point(location):
		return points[location]
	# A crate from a pipeline that no longer exists: show it at the first one's same place.
	if ":" in location and not pipelines.is_empty():
		var fallback := "%s:%s" % [pipelines[0].get("id", ""), location.get_slice(":", 1)]
		if points.has(fallback):
			return points[fallback]
	if not pipelines.is_empty():
		return points.get("%s:intake" % pipelines[0].get("id", ""), Vector2.ZERO)
	return Vector2.ZERO


## The world rectangle that holds every machine, station and belt, with room for labels.
func world_rect() -> Rect2:
	var r := Rect2(Vector2(bounds.position) * CELL, Vector2(bounds.size) * CELL)
	return r.grow_individual(CELL * 0.5, CELL * 0.6, CELL * 1.5, CELL * 0.9)


## The machine or station (footprint key) under a world point, or "".
func node_at(world_point: Vector2) -> String:
	var c := cell_at(world_point)
	for key in footprints:
		if Rect2i(footprints[key], Vector2i(2, 2)).has_point(c):
			return key
	return ""


## The machine id whose footprint contains a world point, or "".
func machine_at(world_point: Vector2) -> String:
	var key := node_at(world_point)
	return key if machines.has(key) else ""


## True when a 2x2 footprint at tl overlaps nothing but `ignore` (and no room wall).
func is_free(tl: Vector2i, ignore := "") -> bool:
	for c in _footprint_cells(tl):
		if room_walls.has(c):
			return false
	for key in footprints:
		if key == ignore:
			continue
		if Rect2i(footprints[key], Vector2i(2, 2)).intersects(Rect2i(tl, Vector2i(2, 2))):
			return false
	return true


## Where this machine's red belt takes failed crates: a machine id, "scrap" (the scrap bin),
## or "" (no red belt: they wait at the machine).
func on_fail(id: String) -> String:
	var pid := str(machines.get(id, {}).get("pipeline", ""))
	for p in pipelines:
		if str(p.get("id", "")) == pid:
			for b in p.get("belts", []):
				if str(b.get("from", "")) == id and str(b.get("port", "")) == "fail":
					return str(b.get("to", ""))
	return ""


func set_machine_state(id: String, state: String, progress: float, step := "") -> void:
	if not machines.has(id):
		return
	var m: Dictionary = machines[id]
	m["state"] = state
	m["progress"] = clampf(progress, 0.0, 1.0)
	if not step.is_empty() or state != "running":
		m["step"] = step
	if state == "running":  # a new job: forget the last result
		m["result"] = ""
	_redraw()


## The last job's result for machines whose art shows it, e.g. the Risk analyser's
## "low" / "medium" / "high". Kept until the machine's next job starts.
func set_machine_result(id: String, result: String) -> void:
	if not machines.has(id):
		return
	machines[id]["result"] = result
	_redraw()


## The result art to show for a finished machine: its own result if it has one,
## else pass / fail from its state (the CI monitor's green / red light).
func _machine_result(m: Dictionary, state: String) -> String:
	if state != "done" and state != "failed":
		return ""
	var result := str(m.get("result", ""))
	return result if not result.is_empty() else str(STATE_RESULTS[state])


## counts: machine id -> number of crates waiting there for approval.
func set_waiting(counts: Dictionary) -> void:
	for id in machines:
		machines[id]["waiting"] = int(counts.get(id, 0))
	_redraw()


## held: machine id -> reasons its crates are waiting on something outside (GitHub).
func set_held(held: Dictionary) -> void:
	var changed := false
	for id in machines:
		var reasons: Array = held.get(id, [])
		if machines[id].get("held", []) != reasons:
			machines[id]["held"] = reasons
			changed = true
	if changed:
		_redraw()


func set_pauses(value: Dictionary) -> void:
	pauses = value
	_redraw()


## Why no crate goes into this machine now because you paused something ("" if nothing is).
func machine_paused_why(id: String) -> String:
	if bool(pauses.get("factory", false)):
		return "Factory paused"
	if str(machines.get(id, {}).get("pipeline", "")) in pauses.get("lines", []):
		return "Line paused"
	if id in pauses.get("machines", []):
		return "Paused"
	return ""


func set_agent_down(down: bool) -> void:
	agent_down = down
	_redraw()


## counts: storage key -> how many of its issues or PRs aren't crates yet.
func set_storage_counts(counts: Dictionary) -> void:
	if counts != storage_counts:
		storage_counts = counts
		_redraw()


func set_bin_counts(counts: Dictionary) -> void:
	if counts == bin_counts:
		return
	var now := Time.get_ticks_msec() / 1000.0
	for key in counts:
		if is_bin(str(key)) and int(counts[key]) > int(bin_counts.get(key, 0)) and _bins_seen:
			_bin_drops[key] = now
	bin_counts = counts
	_bins_seen = true
	_redraw()


func set_highlight(key: String, route := -1) -> void:
	highlighted = key
	highlighted_route = route
	_redraw()


func _line_of_key(key: String) -> String:
	return key.get_slice(":", 0) if ":" in key else str(machines.get(key, {}).get("pipeline", ""))


## The cells a pipeline covers (its machines, stations and belts), as a set.
func line_cells(pid: String) -> Dictionary:
	var out := {}
	for key in footprints:
		var line_id := _line_of_key(key)
		if line_id == pid:
			for c in _footprint_cells(footprints[key]):
				out[c] = true
	for r in routes:
		if r["pid"] == pid:
			for c in r["cells"]:
				out[c] = true
	return out


## Whether a pipeline's machines and stations could sit `delta` cells away without landing on
## another pipeline's.
func line_fits(pid: String, delta: Vector2i) -> bool:
	var others := {}
	for key in footprints:
		var line_id := _line_of_key(key)
		if line_id != pid:
			for c in _footprint_cells(footprints[key]):
				others[c] = true
	for key in footprints:
		var line_id := _line_of_key(key)
		if line_id == pid:
			for c in _footprint_cells(footprints[key] + delta):
				if others.has(c) or room_walls.has(c):
					return false
	return true


func set_group(keys: Dictionary, cells: Dictionary = {}) -> void:
	if keys != group or cells != group_cells:
		group = keys.duplicate()
		group_cells = cells.duplicate()
		_redraw()


func set_select_box(box: Rect2) -> void:
	select_box = box
	_redraw()


func set_ghost(value: Dictionary) -> void:
	ghost = value
	_redraw()


func set_wire(value: Dictionary) -> void:
	wire = value
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
	var area := bounds.grow(12 if editing else 2)
	var tile := _texture("res://assets/floor/floor_plain.svg")
	for x in range(area.position.x, area.end.x):
		for y in range(area.position.y, area.end.y):
			var r := Rect2(Vector2(x, y) * CELL, Vector2(CELL, CELL))
			if tile:
				draw_texture_rect(tile, r, false)
			else:
				draw_rect(r, Color("292B30"))
	if room_painter:
		room_painter.draw_floor(self, area)
	if editing:
		var grid := Color(1, 1, 1, 0.05)
		for x in range(area.position.x, area.end.x + 1):
			draw_line(Vector2(x * CELL, area.position.y * CELL), Vector2(x * CELL, area.end.y * CELL), grid)
		for y in range(area.position.y, area.end.y + 1):
			draw_line(Vector2(area.position.x * CELL, y * CELL), Vector2(area.end.x * CELL, y * CELL), grid)

	for c in belt_cells:
		var cell: Dictionary = belt_cells[c]
		var frame := _return_frame if cell["kind"] == "fail" else _belt_frame
		_draw_belt_cell(c, cell, frame)
		if editing and int(cell["route"]) == highlighted_route:
			draw_rect(Rect2(Vector2(c) * CELL, Vector2(CELL, CELL)).grow(-2), COLOR_AMBER, false, 3.0)

	# Belts with no room to run: a dashed line between the ports.
	for r in routes:
		if r["ok"]:
			continue
		var from_key := node_key(r["pid"], r["from"])
		var to_key := node_key(r["pid"], r["to"])
		if footprints.has(from_key) and footprints.has(to_key):
			var a := cell_center(out_cell(from_key, r["port"]))
			var b := cell_center(in_cell(to_key, r["kind"]))
			draw_dashed_line(a, b, COLOR_FAIL, 3.0, 10.0)


func _draw_belt_cell(c: Vector2i, cell: Dictionary, frame: int) -> void:
	var r := Rect2(Vector2(c) * CELL, Vector2(CELL, CELL))
	if cell.has("over"):
		# Two belts crossing: the art has the bridge as one piece (lower belt, then upper).
		var over: Dictionary = cell["over"]
		var cross := _belt_texture("cross_%s_%s" % [cell["out"], over["out"]], frame)
		if cross:
			draw_texture_rect(cross, r, false)
			return
		var lower := _belt_texture("straight_%s" % cell["out"], frame)
		if lower:
			draw_texture_rect(lower, r, false)
		_draw_plain_belt(r, str(over["in"]), str(over["out"]))
		return
	var ins: Array = cell["in"]
	var out: String = cell["out"]
	var main_in: String = OPPOSITE[out] if OPPOSITE[out] in ins else str(ins[0])
	var piece := ("straight_%s" % out) if main_in == OPPOSITE[out] else ("corner_%s_%s" % [main_in, out])
	var extra: Array = ins.filter(func(s) -> bool: return s != main_in)
	if not extra.is_empty() and main_in == OPPOSITE[out]:
		var merged := _belt_texture("merge_%s_from_%s" % [out, extra[0]], frame)
		if merged:
			draw_texture_rect(merged, r, false)
			return
	var tex := _belt_texture(piece, frame)
	if tex:
		draw_texture_rect(tex, r, false)
	else:
		_draw_plain_belt(r, main_in, out)
	for side in extra:
		_draw_plain_belt(r, side, out, true)


## A belt drawn without art (for pieces the art set doesn't have yet).
func _draw_plain_belt(r: Rect2, entry: String, exit: String, stub := false) -> void:
	var mid := r.get_center()
	var a := mid + Vector2(DIRS[entry]) * CELL * 0.5
	var b := mid + Vector2(DIRS[exit]) * CELL * 0.5
	var base := Color("1C1F24")
	if stub:
		draw_line(a, mid, base, 40.0)
		draw_line(a, mid, Color("3A3F48"), 4.0)
		return
	draw_polyline(PackedVector2Array([a, mid, b]), base, 40.0)
	draw_polyline(PackedVector2Array([a, mid, b]), Color("3A3F48"), 4.0)
	# A small arrow pointing the way the belt runs.
	var d := Vector2(DIRS[exit])
	var tip := mid + d * 10.0
	var side := Vector2(-d.y, d.x) * 6.0
	draw_colored_polygon(PackedVector2Array([tip, mid - d * 2.0 + side, mid - d * 2.0 - side]), Color("80858F"))


## One frame of an animated belt piece, or the static sprite if the frames are missing.
func _belt_texture(piece: String, frame: int) -> Texture2D:
	var tex := _texture("res://assets/belts/anim/belt_%s_f%d.svg" % [piece, frame])
	if tex == null:
		tex = _texture("res://assets/belts/belt_%s.svg" % piece)
	return tex


# Machines (drawn on the layer above the crates) ------------------------------

func _draw_machines() -> void:
	var canvas := _machine_layer
	for p in pipelines:
		var pid := str(p.get("id", ""))
		var title := str(p.get("name", pid))
		for station in STATIONS:
			var label: String = STATION_NAMES[station]
			if station == "intake" and pipelines.size() > 1:
				label = "%s intake" % title
			_draw_station(canvas, "%s:%s" % [pid, station], STATION_SPRITES[station], label)
		for b in p.get("bins", []):
			_draw_station(canvas, "%s:%s" % [pid, str(b.get("id", ""))], "scrap_bin", "Scrap bin")
		for d in p.get("docks", []):
			_draw_station(canvas, "%s:%s" % [pid, str(d.get("id", ""))], "shipping_dock", "Shipping dock")
		for st in storages_of(p):
			_draw_storage(canvas, "%s:%s" % [pid, str(st.get("id", ""))], st)
		var intake := "%s:intake" % pid
		if footprints.has(intake):
			# Where this line's crates come from, under its intake.
			var names := PackedStringArray()
			for src in line_sources(p):
				names.append(str(SOURCE_SHORT.get(src, src)))
			var text := " · ".join(names) if not names.is_empty() else "No sources"
			_label(canvas, Vector2(footprints[intake]) * CELL + Vector2(CELL, 2 * CELL + 16), text, 12, COLOR_QUIET)
			var you_paused: bool = bool(pauses.get("factory", false)) or pid in (pauses.get("lines", []) as Array)
			if you_paused and not editing:
				var pbadge := Rect2(Vector2(footprints[intake]) * CELL + Vector2(6, 6), Vector2(2 * CELL - 12, 24))
				canvas.draw_rect(pbadge, COLOR_PAUSED)
				canvas.draw_rect(pbadge, Color("16181C"), false, 2.0)
				canvas.draw_string(ThemeDB.fallback_font, pbadge.position + Vector2(0, 17),
						"Factory paused" if bool(pauses.get("factory", false)) else "Line paused",
						HORIZONTAL_ALIGNMENT_CENTER, pbadge.size.x, 14, Color("16181C"))
			if p.get("paused", false):
				# Saved but can't run: a badge on the intake itself (it fits whatever is laid
				# round it); build mode says why.
				var badge := Rect2(Vector2(footprints[intake]) * CELL + Vector2(6, 2 * CELL - 30), Vector2(2 * CELL - 12, 24))
				canvas.draw_rect(badge, COLOR_AMBER)
				canvas.draw_rect(badge, Color("16181C"), false, 2.0)
				canvas.draw_string(ThemeDB.fallback_font, badge.position + Vector2(0, 17), "⚠ Paused",
						HORIZONTAL_ALIGNMENT_CENTER, badge.size.x, 14, Color("16181C"))
	for id in machines:
		_draw_machine(canvas, id)
	if editing:
		_draw_ports(canvas)
		for k in group:
			if footprints.has(k):
				var r := Rect2(Vector2(footprints[k]) * CELL - Vector2(5, 5), Vector2(2 * CELL + 10, 2 * CELL + 10))
				canvas.draw_rect(r, Color("5AB0FF"), false, 3.0)
		for c in group_cells:
			var cr := Rect2(Vector2(c) * CELL, Vector2(CELL, CELL)).grow(-3)
			canvas.draw_rect(cr, Color(0.35, 0.69, 1.0, 0.18))
			canvas.draw_rect(cr, Color("5AB0FF"), false, 2.0)
		if select_box.size != Vector2.ZERO:
			canvas.draw_rect(select_box, Color(0.35, 0.69, 1.0, 0.15))
			canvas.draw_rect(select_box, Color("5AB0FF"), false, 2.0)
		if not ghost.is_empty():
			var g := Rect2(Vector2(ghost["pos"]) * CELL, Vector2(ghost.get("size", Vector2i(2, 2))) * CELL)
			var col := Color(0.35, 0.75, 0.4, 0.35) if ghost.get("ok", false) else Color(0.9, 0.28, 0.3, 0.35)
			canvas.draw_rect(g, col)
			canvas.draw_rect(g, col.lightened(0.3), false, 2.0)
		if not wire.is_empty() and wire.has("cells"):
			# A belt being laid by hand: the cells so far, with a line through them.
			var col: Color = COLOR_FAIL if wire.get("kind", "") == "fail" else COLOR_PASS
			var line := PackedVector2Array()
			for c in wire["cells"]:
				var r := Rect2(Vector2(c) * CELL, Vector2(CELL, CELL))
				var free := not belt_cells.has(c) and not under_node(c)
				canvas.draw_rect(r.grow(-4), Color(col, 0.25) if free else Color(0.9, 0.28, 0.3, 0.35))
				line.append(r.get_center())
			if line.size() > 1:
				canvas.draw_polyline(line, col, 4.0)
			if not line.is_empty():
				canvas.draw_circle(line[-1], 6.0, col)
		elif not wire.is_empty():
			var col := COLOR_FAIL if wire.get("kind", "") == "fail" else COLOR_PASS
			canvas.draw_line(wire["from"], wire["to"], col, 4.0)
			canvas.draw_circle(wire["to"], 6.0, col)


func _draw_ports(canvas: CanvasItem) -> void:
	for key in footprints:
		var ports := output_ports(key)
		for port in ports:
			var col := COLOR_FAIL if port == "fail" else COLOR_PASS
			canvas.draw_circle(ports[port], 9.0, Color("16181C"))
			canvas.draw_circle(ports[port], 6.0, col)
			if SORTER_PORTS.has(port) and not is_splitter(key):  # a sorter's outputs are numbered, as in its settings
				canvas.draw_string(ThemeDB.fallback_font, ports[port] + Vector2(-3.5, 4.0), port.trim_prefix("out_"),
						HORIZONTAL_ALIGNMENT_LEFT, -1, 11, Color("16181C"))


## The machine sprite for this moment: the running loop, the idle blink, or the static file.
func _machine_texture(sprite: String, running: bool) -> Texture2D:
	var tex: Texture2D
	if running:
		tex = _texture("res://assets/machines/anim/%s_run_f%d.svg" % [sprite, _machine_tick % RUN_FRAMES])
	else:
		var idle := int(_machine_tick * IDLE_FPS / RUN_FPS) % IDLE_FRAMES
		tex = _texture("res://assets/machines/anim/%s_idle_f%d.svg" % [sprite, idle])
	if not tex:
		tex = _texture("res://assets/machines/%s.svg" % sprite)
	if not tex and FALLBACK_SPRITES.has(sprite):  # newer machine, art not there yet
		return _machine_texture(FALLBACK_SPRITES[sprite], running)
	return tex


func _draw_station(canvas: CanvasItem, key: String, sprite: String, label: String) -> void:
	if not footprints.has(key):
		return
	var origin := Vector2(footprints[key]) * CELL
	var count := int(bin_counts.get(key, 0))
	if is_bin(key):
		var variant := "scrap_bin_empty"
		if count > 0:
			variant = "scrap_bin_some" if count <= 3 and _texture("res://assets/machines/scrap_bin_some.svg") \
					else "scrap_bin_full"
		if _texture("res://assets/machines/%s.svg" % variant):
			sprite = variant
	_draw_body(canvas, origin, sprite, Color("80858F"), false)
	if _bin_drops.has(key):  # a crate dropping in: the one-shot animation over the full bin
		var frame := int((Time.get_ticks_msec() / 1000.0 - float(_bin_drops[key])) * DROP_FPS)
		var drop := _texture("res://assets/machines/anim/scrap_bin_drop_f%d.svg" % frame) \
				if frame < DROP_FRAMES else null
		if drop:
			canvas.draw_texture_rect(drop, Rect2(origin, Vector2(128, 128)), false)
		else:
			_bin_drops.erase(key)
	if count > 0:  # how many crates are in the bin
		var text := str(count) if count < 1000 else "999+"
		var w := maxf(28.0, 12.0 + text.length() * 11.0)
		var badge := Rect2(origin + Vector2(126.0 - w, -8.0), Vector2(w, 26.0))
		canvas.draw_rect(badge, Color("E5484D") if is_bin(key) else Color("3E9A4C"))
		canvas.draw_rect(badge, Color("16181C"), false, 2.0)
		canvas.draw_string(ThemeDB.fallback_font, badge.position + Vector2(0, 20), text,
				HORIZONTAL_ALIGNMENT_CENTER, badge.size.x, 19, Color.WHITE)
	var below := is_bin(key)
	_label(canvas, origin + (Vector2(CELL, 2 * CELL + 18) if below else Vector2(CELL, -8)), label, 14, COLOR_TEXT)
	if key == highlighted:
		canvas.draw_rect(Rect2(origin, Vector2(128, 128)).grow(4), COLOR_AMBER, false, 3.0)


## A storage: its rack (fuller as more is waiting), its source's icon in the corner, its
## name above and its filter below.
func _draw_storage(canvas: CanvasItem, key: String, st: Dictionary) -> void:
	if not footprints.has(key):
		return
	var origin := Vector2(footprints[key]) * CELL
	var count := int(storage_counts.get(key, -1))
	var sprite := "storage"
	if count >= 0:
		sprite = "storage_empty" if count == 0 else ("storage_some" if count <= 5 else "storage_full")
	if not _texture("res://assets/machines/%s.svg" % sprite):
		sprite = "storage" if _texture("res://assets/machines/storage.svg") else "intake"  # art not there yet
	_draw_body(canvas, origin, sprite, Color("80858F"), false)
	var source := str(st.get("source", ""))
	var icon := _texture("res://assets/ui/%s.svg" % str(STORAGE_ICONS.get(source, "icon_crate")))
	if icon == null and source == "pr_review":
		icon = _texture("res://assets/ui/icon_pr.svg")
	if icon:
		var badge := Rect2(origin + Vector2(98, 2), Vector2(28, 28))
		canvas.draw_rect(badge, Color("16181C"))
		canvas.draw_texture_rect(icon, badge.grow(-3), false)
	if count > 0:  # how many are waiting to come in
		var text := str(count) if count < 100 else "99+"
		var w := maxf(24.0, 10.0 + text.length() * 9.0)
		var tag := Rect2(origin + Vector2(2, 2), Vector2(w, 22.0))
		canvas.draw_rect(tag, Color("CC8A14"))
		canvas.draw_rect(tag, Color("16181C"), false, 2.0)
		canvas.draw_string(ThemeDB.fallback_font, tag.position + Vector2(0, 17), text,
				HORIZONTAL_ALIGNMENT_CENTER, tag.size.x, 15, Color("16181C"))
	_label(canvas, origin + Vector2(CELL, -8), str(STORAGE_NAMES.get(source, "Storage")), 14, COLOR_TEXT)
	var query := str(st.get("query", "")).strip_edges()
	_label(canvas, origin + Vector2(CELL, 2 * CELL + 16), query.left(36) + ("…" if query.length() > 36 else "")
			if not query.is_empty() else str(STORAGE_DEFAULT_FILTERS.get(source, "")), 12, COLOR_QUIET)
	var repo := str(st.get("repo", "")).strip_edges().trim_prefix("https://").trim_prefix("github.com/")
	if not repo.is_empty():  # its own repo: crates from it are worked on there
		_label(canvas, origin + Vector2(CELL, 2 * CELL + 32), repo.left(36), 12, COLOR_TEXT)
	if key == highlighted:
		canvas.draw_rect(Rect2(origin, Vector2(128, 128)).grow(4), COLOR_AMBER, false, 3.0)


func _draw_machine(canvas: CanvasItem, id: String) -> void:
	var m: Dictionary = machines[id]
	var origin := Vector2(footprints[id]) * CELL
	var state := "idle" if editing else str(m["state"])
	var col: Color = STATE_COLORS.get(state, STATE_COLORS["idle"])
	_draw_body(canvas, origin, str(m["sprite"]), col, state == "running",
			not editing and not (m.get("held", []) as Array).is_empty(),
			"" if editing else _machine_result(m, state))
	if OVERLAYS.has(state):
		var overlay := _texture("res://assets/machines/%s.svg" % OVERLAYS[state])
		if overlay:
			canvas.draw_texture_rect(overlay, Rect2(origin, Vector2(128, 128)), false)
	if id == highlighted:
		canvas.draw_rect(Rect2(origin, Vector2(128, 128)).grow(4), COLOR_AMBER, false, 3.0)
	var paused_why := "" if editing else machine_paused_why(id)
	if not paused_why.is_empty():
		# Paused by you: dimmed, with a blue pause sign and what's paused.
		var body := Rect2(origin, Vector2(128, 128))
		canvas.draw_rect(body.grow(-4), Color(0.08, 0.09, 0.11, 0.62))
		canvas.draw_rect(body.grow(-4), COLOR_PAUSED, false, 2.0)
		var mid := body.get_center()
		canvas.draw_rect(Rect2(mid + Vector2(-13, -18), Vector2(9, 28)), COLOR_PAUSED)
		canvas.draw_rect(Rect2(mid + Vector2(4, -18), Vector2(9, 28)), COLOR_PAUSED)
		_label(canvas, mid + Vector2(0, 30), paused_why, 12, COLOR_PAUSED, 128.0)
	elif agent_down and bool(m.get("uses_agent", false)) and not editing:
		# Paused until Claude is back: dimmed, with a pause sign across the middle.
		var body := Rect2(origin, Vector2(128, 128))
		canvas.draw_rect(body.grow(-4), Color(0.08, 0.09, 0.11, 0.62))
		canvas.draw_rect(body.grow(-4), COLOR_FAIL, false, 2.0)
		var mid := body.get_center()
		canvas.draw_rect(Rect2(mid + Vector2(-13, -18), Vector2(9, 28)), COLOR_AMBER)
		canvas.draw_rect(Rect2(mid + Vector2(4, -18), Vector2(9, 28)), COLOR_AMBER)
		_label(canvas, mid + Vector2(0, 30), "Waiting for Claude", 12, COLOR_AMBER, 128.0)

	var top := origin + Vector2(CELL, -8)
	_label(canvas, top, str(m["name"]), 15, COLOR_TEXT)
	if bool(m.get("gate", false)):
		# A gate: a small amber "pause" mark next to the name instead of a line of text.
		var icon := _texture("res://assets/ui/icon_pause.svg")
		var at := origin + Vector2(CELL + _text_width(str(m["name"]), 15) / 2.0 + 4.0, -24)
		if icon:
			canvas.draw_texture_rect(icon, Rect2(at, Vector2(18, 18)), false, COLOR_AMBER)
		else:
			canvas.draw_rect(Rect2(at + Vector2(4, 3), Vector2(4, 12)), COLOR_AMBER)
			canvas.draw_rect(Rect2(at + Vector2(10, 3), Vector2(4, 12)), COLOR_AMBER)
	var waiting := int(m.get("waiting", 0))
	if waiting > 0 and not editing:
		# Badge on the top-left corner: how many crates wait here for you.
		var badge := origin + Vector2(10, 10)
		canvas.draw_circle(badge, 11.0, COLOR_AMBER)
		canvas.draw_string(ThemeDB.fallback_font, badge + Vector2(-11, 5), str(waiting),
				HORIZONTAL_ALIGNMENT_CENTER, 22, 14, Color("16181C"))
	var held: Array = m.get("held", [])
	if not held.is_empty() and not editing:
		# Crates waiting on GitHub (a review, checks): a count, and why under the machine.
		var badge := origin + Vector2(10, 10) if waiting == 0 else origin + Vector2(34, 10)
		canvas.draw_circle(badge, 11.0, COLOR_HELD)
		canvas.draw_string(ThemeDB.fallback_font, badge + Vector2(-11, 5), str(held.size()),
				HORIZONTAL_ALIGNMENT_CENTER, 22, 14, Color("16181C"))

	# Progress and the current step only while there is something to say.
	if state == "running" or state == "failed":
		var below := origin + Vector2(0, 2 * CELL + 6)
		var bar := Rect2(below + Vector2(16, 0), Vector2(96, 5))
		canvas.draw_rect(bar, Color(0, 0, 0, 0.5))
		canvas.draw_rect(Rect2(bar.position, Vector2(bar.size.x * float(m["progress"]), bar.size.y)), col)
		var step := str(m["step"])
		if not held.is_empty():  # say what it's waiting for rather than the step's name
			step = str(held[0]) if held.size() == 1 else "%d waiting on GitHub" % held.size()
		if not step.is_empty():
			_label(canvas, below + Vector2(CELL, 19), step, 11, COLOR_HELD if not held.is_empty() else COLOR_QUIET, 150.0)
	elif not held.is_empty():
		_label(canvas, origin + Vector2(CELL, 2 * CELL + 25), str(held[0]) if held.size() == 1
				else "%d waiting on GitHub" % held.size(), 11, COLOR_HELD, 150.0)


func _text_width(text: String, size: int) -> float:
	return ThemeDB.fallback_font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, size).x


func _draw_body(canvas: CanvasItem, origin: Vector2, sprite: String, lamp: Color, running: bool,
		waiting := false, result := "") -> void:
	var tex: Texture2D = null
	if waiting:  # crates held here waiting on GitHub: the machine's "waiting" loop, if it has one
		var idle := int(_machine_tick * IDLE_FPS / RUN_FPS) % IDLE_FRAMES
		tex = _texture("res://assets/machines/anim/%s_wait_f%d.svg" % [sprite, idle])
	if not tex and not running and not result.is_empty():  # finished: its result art, if it has one
		var idle := int(_machine_tick * IDLE_FPS / RUN_FPS) % IDLE_FRAMES
		tex = _texture("res://assets/machines/anim/%s_%s_f%d.svg" % [sprite, result, idle])
	if not tex:
		tex = _machine_texture(sprite, running)
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
