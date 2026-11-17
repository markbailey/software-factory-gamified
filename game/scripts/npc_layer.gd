extends Node2D
## The people on the factory floor. Each one stands for something the factory is really doing:
##
## - Agent worker: one per Claude agent session running a machine's job (machines that use
##   Claude only). It sets off as soon as a crate is heading for that machine (the crate is
##   created at the intake, starts at the machine before, or moves onto the belt or rework belt
##   in front of it), so it is usually there, standing ready, when job_started comes; one that
##   is still walking then runs the rest of the way. It works while the job runs and leaves on
##   job_finished or job_failed, or if its crate goes somewhere else instead. A worker waits, arms folded and tapping a foot,
##   at a gate while a crate waits there for you (awaiting_approval), and at a machine that is
##   paused because Claude is unavailable or a tool it needs is broken. Every worker on the
##   floor cheers when a crate ships or its PR merges.
## - Mechanic: comes to a machine whose job failed (runs to the CI monitor) and repairs it
##   for a few seconds; stays at a machine while a crate is held there, or while its step
##   waits for broken tooling (tool_status), until that clears.
## - Janitor: sweeps a scrap bin after a crate lands in it.
## - Carrying: when a machine says a crate isn't its work (item_moved with carried: true), a
##   worker carries it straight across to the right machine for the move's duration: the worker
##   who was at the old machine, or one who steps in. If Claude works the crate at the new
##   machine, that worker stays for the job; otherwise they go back out.
##
## People never walk on conveyors: they cross belts only at footbridges, and crates pass
## underneath. You place footbridges with the Rooms bar in build mode (room_layer.gd's
## `bridges`, read through factory_floor.room_painter); none are added for you. Where a
## machine can't be reached from a door without crossing a belt, its person walks as
## close as they can get, turns to face the machine and works from there (the machine runs
## the same either way). Put a footbridge down to let them all the way in.
##
## People come and go through worker doors. Place as many as you like with the Rooms bar's
## Worker door block (room_layer.gd's `staff_doors`); each person comes in through the door
## nearest their machine by walking path and goes back out through it. Until you place one
## there is one staff door, under the factory's bottom-left corner until you drag it somewhere
## else in build mode (any free floor cell: not under a machine, station or belt); where you
## put it is kept in user://npc_door.cfg. Remove every door (room_layer.gd's staff_door_on is
## false and no worker doors) and people appear at their machines and fade out when done.
##
## This node only listens to backend messages (connect it to BackendClient.message_received
## after main.gd's own handler) and reads the floor's layout; it never changes either.

const FactoryFloor := preload("res://scripts/factory_floor.gd")
const Npc := preload("res://scripts/npc.gd")

const CELL := 64
const MARGIN := 3  # cells around the factory that people may walk through
const REPAIR_SECONDS := 5.0  # a failed machine's repair
const SWEEP_SECONDS := 5.0
const SWEEP_MAX := 12.0
const CHEER_SECONDS := 1.6
const FINISH_GRACE := 1.0  # a finished worker lingers this long in case its crate now waits at the gate
const DOOR_REACH := 90.0  # the door opens while someone is this close
const MAX_WORKERS := 24
const READY_TIMEOUT := 900.0  # a worker waiting for a crate that never comes goes home after this
const DOOR_FILE := "user://npc_door.cfg"
const COLOR_AMBER := Color("F2B84C")
const COLOR_PASS := Color("5ABF66")
const COLOR_FAIL := Color("E5484D")
# Hard hat band colours: the accent of the machine (by sprite), as in assets/README.md.
const ACCENTS := {
	"intake": "8CBFFF", "refiner": "3FB8AF", "coder": "9B7BFF", "tester": "F2B84C",
	"linter": "5FD0D8", "reviewer": "F08A4B", "packager": "C9975B", "ci_monitor": "C4CBD6",
	"shipping_dock": "5ABF66", "splitter": "E8C547", "merger": "E8C547", "scrap_bin": "E5484D",
	"risk_analyser": "E86BB5", "planner": "A8D84E", "pr_merger": "C06CF5", "doc_writer": "F4E3B1",
	"verifier": "6B7CFF", "labeller": "4FE0B0", "approver": "FF7A9E", "storage": "CC8A14",
}

var factory_floor: FactoryFloor

var _frames := {}  # kind -> SpriteFrames
var _band: Texture2D
var _door_textures: Array = []
var _door_open := {}  # door cell -> 0..1
var _door_frame := {}  # door cell -> 0..3
var _door_cell := Vector2i.ZERO  # the one staff door used when no worker doors are placed
var _doors: Array = []  # the door cells in use: placed worker doors, else [_door_cell]
var _using_placed := false  # true when the doors are ones you placed (the staff door is unused)
var _door_saved = null  # Vector2i where you put the door, or null for the default spot
var _door_hover := false
var _door_drag := false
var _door_ghost := Vector2i.ZERO  # where the door would go while you drag it
var _drag_from := Vector2i.ZERO  # the door being dragged
var _hover_cell := Vector2i.ZERO  # the door under the mouse

var _astar := AStarGrid2D.new()
var _bridges := {}  # belt cell -> true where a footbridge you placed (Rooms bar) crosses it
var _bridge_texture: Texture2D
var _was_editing := false
var _grid_sig := ""
var _grid_tick := 0.0

var _last_seq := 0
var _crates := {}  # item id -> {state, location}
var _agent_down := false
var _outages := false

var _workers := {}  # key ("job:<machine>:<item>" or "gate:<machine>") -> Npc
var _ready_left := {}  # "next:<machine>:<item>" -> seconds a ready worker still waits
var _finishing := {}  # job key -> seconds left before the worker leaves
var _mech_jobs: Array = []  # [{machine, why, item, left, run}] left < 0 = until cleared
var _mechanic: Npc
var _mech_at := ""  # machine the mechanic is going to or working at
var _bins: Array = []  # scrap bin keys waiting for the janitor
var _janitor: Npc
var _jan_at := ""
var _sweep_left := 0.0


func _init() -> void:
	for kind in ["worker", "mechanic", "janitor"]:
		_frames[kind] = load("res://assets/npcs/%s.tres" % kind)
	_band = load("res://assets/npcs/worker_band.svg")
	_bridge_texture = load("res://assets/npcs/footbridge.svg")
	for i in 4:
		_door_textures.append(load("res://assets/npcs/door_f%d.svg" % i))
	var cfg := ConfigFile.new()
	if cfg.load(DOOR_FILE) == OK and cfg.has_section_key("door", "cell"):
		var c = cfg.get_value("door", "cell")
		if c is Vector2i:
			_door_saved = c


# Backend messages ----------------------------------------------------------------------------

func on_message(msg: Dictionary) -> void:
	var kind := str(msg.get("type", ""))
	match kind:
		"snapshot":
			_apply_snapshot(msg)
			return
		"agent_status":
			_set_agent_down(not bool(msg.get("available", true)))
			return
		"tool_status":
			_set_outages(not (msg.get("outages", []) as Array).is_empty())
			return
	if not msg.has("seq"):
		return
	var seq := int(msg.get("seq", 0))
	if seq <= _last_seq:
		return
	_last_seq = seq
	_ensure_grid()

	var id := int(msg.get("item_id", -1))
	var machine := str(msg.get("machine", ""))
	match kind:
		"item_created":
			var item: Dictionary = msg.get("item", {})
			_crates[int(item.get("id", -1))] = {"state": str(item.get("state", "")),
					"location": str(item.get("location", ""))}
			_send_ahead(int(item.get("id", -1)), _first_machine(str(item.get("location", ""))))
		"job_started":
			_crates[id] = {"state": "running", "location": machine}
			_start_job(machine, id)
			_send_ahead(id, _machine_after(machine))
		"job_progress":
			var step := str(msg.get("step", ""))
			if step.begins_with("Waiting for ") and _outages:
				_add_mech_job(machine, "tool", id, -1.0, false)
				_set_worker_loop(_job_key(machine, id), "wait")
			else:
				_clear_mech_jobs(func(j: Dictionary) -> bool: return j["why"] == "tool" and j["item"] == id)
				_set_worker_loop(_job_key(machine, id), _work_loop())
		"job_finished":
			_clear_mech_jobs(func(j: Dictionary) -> bool: return j["why"] == "tool" and j["item"] == id)
			_finish_job(machine, id)
		"job_failed":
			_clear_mech_jobs(func(j: Dictionary) -> bool: return j["why"] == "tool" and j["item"] == id)
			_send_home(_job_key(machine, id))
			_call_off(id, "")
			var sprite := str(factory_floor.machines.get(machine, {}).get("sprite", ""))
			_add_mech_job(machine, "failed", id, REPAIR_SECONDS, sprite == "ci_monitor")
		"item_held":
			_add_mech_job(machine, "held", id, -1.0, false)
		"item_moved":
			_on_moved(id, msg)
		"pr_state":
			if str(msg.get("state", "")) == "merged":
				_cheer_all()


func _apply_snapshot(snap: Dictionary) -> void:
	for npc in get_children():
		if npc is Npc:
			npc.queue_free()
	_workers.clear()
	_ready_left.clear()
	_finishing.clear()
	_mech_jobs.clear()
	_mechanic = null
	_mech_at = ""
	_janitor = null
	_jan_at = ""
	_bins.clear()
	_crates.clear()
	_last_seq = int(snap.get("seq", 0))
	_agent_down = not bool(snap.get("agent", {}).get("available", true))
	_outages = not (snap.get("tools", []) as Array).is_empty()
	_grid_sig = ""
	_ensure_grid()
	for item in snap.get("items", []):
		var id := int(item.get("id", -1))
		var state := str(item.get("state", ""))
		var location := str(item.get("location", ""))
		_crates[id] = {"state": state, "location": location}
		if ":belt_" in location:
			_send_ahead(id, factory_floor.machine_ahead(location))
		if not factory_floor.machines.has(location):
			continue
		if state == "running":
			if _uses_agent(location):
				_place_worker(_job_key(location, id), location, _work_loop())
			_send_ahead(id, _machine_after(location))
		elif state == "awaiting_approval" and not _workers.has("gate:" + location):
			_place_worker("gate:" + location, location, "wait")
		elif state == "held":
			_add_mech_job(location, "held", id, -1.0, false)


func _on_moved(id: int, msg: Dictionary) -> void:
	var before: Dictionary = _crates.get(id, {"state": "", "location": ""})
	var to := str(msg.get("to", before["location"]))
	var state := str(msg.get("state", before["state"]))
	_crates[id] = {"state": state, "location": to}
	var from := str(before["location"])
	if bool(msg.get("carried", false)):
		_carry(id, str(msg.get("from", from)), str(msg.get("machine", "")), to,
				float(msg.get("duration", 2.5)))

	# Where the crate is heading decides who gets ready for it.
	if ":belt_" in to:
		_send_ahead(id, factory_floor.machine_ahead(to))
	elif ":return_" in to:
		_send_ahead(id, factory_floor.on_fail(from))
	elif to.ends_with(":intake"):
		_send_ahead(id, _first_machine(to))
	elif not factory_floor.machines.has(to) or state in ["scrapped", "shipped", "held"]:
		_call_off(id, "")

	# A crate waits for you at a gate: a worker waits there with it.
	if state == "awaiting_approval" and factory_floor.machines.has(to):
		var gate_key := "gate:" + to
		if not _workers.has(gate_key):
			var job_key := _job_key(to, id)
			if _workers.has(job_key) and not _workers[job_key].is_leaving():
				_workers[gate_key] = _workers[job_key]
				_workers.erase(job_key)
				_finishing.erase(job_key)
				_workers[gate_key].key = gate_key
				_workers[gate_key].set_loop("wait")
			else:
				_spawn_worker(gate_key, to, "wait")
	if str(before["state"]) == "awaiting_approval" and (from != to or state != "awaiting_approval"):
		if not _any_crate(func(c: Dictionary) -> bool: return c["state"] == "awaiting_approval" and c["location"] == from):
			_send_home("gate:" + from)

	# A held crate that moves on (retried or scrapped) no longer needs the mechanic there.
	if str(before["state"]) == "held" and (from != to or state != "held"):
		_clear_mech_jobs(func(j: Dictionary) -> bool: return j["why"] == "held" and j["item"] == id)

	if state == "shipped" and str(before["state"]) != "shipped":
		_cheer_all()
	if FactoryFloor.is_bin(to) and not FactoryFloor.is_bin(from) and state == "scrapped":
		_call_janitor(to)


## A worker carries crate `id` from machine `from_m` to `target` (its crate lands on `to`).
func _carry(id: int, from_m: String, target: String, to: String, seconds: float) -> void:
	var npc: Npc = null
	for k in [_job_key(from_m, id), _ready_key(from_m, id)]:
		if _workers.has(k) and is_instance_valid(_workers[k]) and not _workers[k].is_leaving() \
				and not _workers[k].is_carrying():
			npc = _workers[k]
			_workers.erase(k)
			_finishing.erase(k)
			_ready_left.erase(k)
			break
	var crate := _crate_node(id)
	var start := _machine_mid(from_m)
	if crate:
		start = to_local(crate.global_position)
	var end := _machine_mid(target)
	var wp: Array = factory_floor.waypoints(from_m, to)
	if not wp.is_empty() and wp[-1] is Vector2:
		end = wp[-1]
	if npc == null:
		if _workers.size() >= MAX_WORKERS:
			return
		npc = _spawn("worker", target)  # steps in beside the crate
		var dir := (end - start).normalized() if end != start else Vector2.RIGHT
		npc.position = start - dir * Npc.CARRY_GAP
		npc.rotation = dir.angle()
		npc._heading = npc.rotation
	# Stays on for the job when Claude works it at the new machine.
	var key := "carry:%d" % id
	var ready := _ready_key(target, id)
	if _uses_agent(target) and not _workers.has(ready) and not _workers.has(_job_key(target, id)):
		key = ready
		_ready_left[key] = READY_TIMEOUT
	npc.key = key
	_workers[key] = npc
	npc.set_band(_accent(target))
	if npc.has_meta("machine"):
		npc.remove_meta("machine")
	npc.set_meta("carry_to", target)
	npc.set_loop("idle")
	npc.carry(crate, start, end, seconds)
	if not npc.carried.is_connected(_on_carried):
		npc.carried.connect(_on_carried)


## The crate is set down: back out, or on to the job at the new machine.
func _on_carried(npc: Npc) -> void:
	if not is_instance_valid(npc) or npc.is_leaving():
		return
	var key := npc.key
	var target := str(npc.get_meta("carry_to", ""))
	var staying: bool = not key.begins_with("carry:") and _workers.get(key) == npc
	var dest: Vector2i = _stand_spot(target, _free_slot(target))["approach"] if staying else _door_of(npc)
	npc.set_meta("approach", _nearest_walkable(factory_floor.cell_at(npc.position), dest))
	if not staying:
		if _workers.get(key) == npc:
			_workers.erase(key)
		_leave(npc)
		return
	_walk_to_machine(npc, target, _free_slot(target), npc.current_loop(), false)


## The game's own crate node for item `id` (main.gd's `crates`), or null.
func _crate_node(id: int) -> Node2D:
	var main := get_parent()
	if main == null:
		return null
	var crates = main.get("crates")
	if crates is Dictionary and crates.has(id) and crates[id] is Node2D and is_instance_valid(crates[id]):
		return crates[id]
	return null


func _machine_mid(key: String) -> Vector2:
	var tl: Vector2i = factory_floor.footprints.get(key, Vector2i.ZERO)
	return Vector2(tl) * CELL + Vector2(CELL, CELL)


## A cell near `c` that people may stand on and get to from the door, picked so the walk on
## to `dest` is short (the near side of a belt rather than the far one).
func _nearest_walkable(c: Vector2i, dest: Vector2i) -> Vector2i:
	if _reachable(c):
		return c
	for r in range(1, 6):
		var best := Vector2i(-99999, -99999)
		var best_cost := INF
		for dy in range(-r - 1, r + 2):
			for dx in range(-r - 1, r + 2):
				var n := c + Vector2i(dx, dy)
				if not _reachable(n):
					continue
				var cost := Vector2(n - c).length() * 2.0 + float(_astar.get_id_path(n, dest, true).size())
				if cost < best_cost:
					best_cost = cost
					best = n
		if best.x != -99999:
			return best
	return c


func _set_agent_down(down: bool) -> void:
	_agent_down = down
	for key in _workers:
		if str(key).begins_with("job:"):
			_workers[key].set_loop(_work_loop())


func _set_outages(any: bool) -> void:
	_outages = any
	if not any:
		_clear_mech_jobs(func(j: Dictionary) -> bool: return j["why"] == "tool")
		for key in _workers:
			if str(key).begins_with("job:"):
				_workers[key].set_loop(_work_loop())


# Workers ---------------------------------------------------------------------------------------

func _job_key(machine: String, id: int) -> String:
	return "job:%s:%d" % [machine, id]


func _work_loop() -> String:
	return "wait" if _agent_down else "work"


func _uses_agent(machine: String) -> bool:
	return bool(factory_floor.machines.get(machine, {}).get("uses_agent", false))


func _start_job(machine: String, id: int) -> void:
	if not _uses_agent(machine):
		return  # scripts run by themselves: no agent, no worker
	var key := _job_key(machine, id)
	if _workers.has(key) and not _workers[key].is_leaving():
		_finishing.erase(key)
		_workers[key].set_loop(_work_loop())
		return
	# The worker who set off when the crate was on its way takes the job (running if late).
	var ready_key := _ready_key(machine, id)
	if _workers.has(ready_key) and not _workers[ready_key].is_leaving():
		var ready: Npc = _workers[ready_key]
		_workers.erase(ready_key)
		_ready_left.erase(ready_key)
		ready.key = key
		_workers[key] = ready
		ready.set_loop(_work_loop())
		ready.hurry()
		return
	_call_off(id, machine)
	# The crate came back to work at the gate it was waiting at: that worker takes the job.
	var gate_key := "gate:" + machine
	if _workers.has(gate_key) and not _any_crate(func(c: Dictionary) -> bool:
			return c["state"] == "awaiting_approval" and c["location"] == machine):
		var npc: Npc = _workers[gate_key]
		_workers.erase(gate_key)
		npc.key = key
		_workers[key] = npc
		npc.set_loop(_work_loop())
		return
	_spawn_worker(key, machine, _work_loop())


func _ready_key(machine: String, id: int) -> String:
	return "next:%s:%d" % [machine, id]


## The first machine a crate at an intake ("<line>:intake") goes to, or "".
func _first_machine(location: String) -> String:
	if not location.ends_with(":intake"):
		return ""
	var chain: Array = factory_floor.chain_for(location.get_slice(":", 0))
	return str(chain[0]) if not chain.is_empty() else ""


## The machine after `machine` along its line's pass belts, or "".
func _machine_after(machine: String) -> String:
	var pid := str(factory_floor.machines.get(machine, {}).get("pipeline", ""))
	var chain: Array = factory_floor.chain_for(pid)
	var i := chain.find(machine)
	return str(chain[i + 1]) if i >= 0 and i + 1 < chain.size() else ""


## A crate is on its way to `machine`: if Claude will work it there, a worker sets off now so
## they are there when the job starts. Any worker waiting for this crate elsewhere goes home.
func _send_ahead(id: int, machine: String) -> void:
	if id < 0:
		return
	_call_off(id, machine)
	if machine.is_empty() or not _uses_agent(machine):
		return
	var key := _ready_key(machine, id)
	if _workers.has(key) or _workers.has(_job_key(machine, id)):
		return
	_ready_left[key] = READY_TIMEOUT
	_spawn_worker(key, machine, "idle")


## Send home the workers waiting for crate `id`, except the one at `keep`.
func _call_off(id: int, keep: String) -> void:
	for key in _workers.keys():
		var k := str(key)
		if k.begins_with("next:") and k.ends_with(":%d" % id) and k != _ready_key(keep, id):
			_send_home(k)


func _finish_job(machine: String, id: int) -> void:
	var key := _job_key(machine, id)
	if _workers.has(key):
		_finishing[key] = FINISH_GRACE
		_workers[key].set_loop("idle")


func _spawn_worker(key: String, machine: String, loop: String) -> void:
	if _workers.size() >= MAX_WORKERS:
		return
	var npc := _spawn("worker", machine)
	npc.key = key
	npc.set_band(_accent(machine))
	_workers[key] = npc
	_walk_to_machine(npc, machine, _free_slot(machine), loop, false)


## A worker already at its machine (the game opened while the job was running).
func _place_worker(key: String, machine: String, loop: String) -> void:
	var npc := _spawn("worker", machine)
	npc.key = key
	npc.set_band(_accent(machine))
	_workers[key] = npc
	var spot := _stand_spot(machine, _free_slot(machine))
	npc.position = spot["point"]
	npc.set_meta("approach", spot["approach"])
	npc.walk([], loop, spot["face"])


func _set_worker_loop(key: String, loop: String) -> void:
	if _workers.has(key):
		_workers[key].set_loop(loop)


func _send_home(key: String) -> void:
	if not _workers.has(key):
		return
	var npc: Npc = _workers[key]
	_workers.erase(key)
	_finishing.erase(key)
	_ready_left.erase(key)
	_leave(npc)


func _cheer_all() -> void:
	for npc in _workers.values():
		npc.cheer(CHEER_SECONDS)


func _accent(machine: String) -> Color:
	var sprite := str(factory_floor.machines.get(machine, {}).get("sprite", ""))
	return Color(str(ACCENTS.get(sprite, "E6E9EF")))


## The first worker spot at a machine that no worker is using or walking to.
func _free_slot(machine: String) -> int:
	var used := {}
	for key in _workers:
		var npc: Npc = _workers[key]
		if npc.has_meta("machine") and npc.get_meta("machine") == machine:
			used[int(npc.get_meta("slot", 0))] = true
	var slot := 0
	while used.has(slot):
		slot += 1
	return slot


# Mechanic --------------------------------------------------------------------------------------

func _add_mech_job(machine: String, why: String, id: int, seconds: float, run: bool) -> void:
	if not factory_floor.machines.has(machine):
		return
	for j in _mech_jobs:
		if j["machine"] == machine and j["why"] == why and j["item"] == id:
			j["left"] = seconds if seconds < 0.0 else maxf(float(j["left"]), seconds)
			return
	_mech_jobs.append({"machine": machine, "why": why, "item": id, "left": seconds, "run": run})


func _clear_mech_jobs(match_job: Callable) -> void:
	_mech_jobs = _mech_jobs.filter(func(j: Dictionary) -> bool: return not match_job.call(j))


func _update_mechanic(delta: float) -> void:
	# Count down timed repairs only while the mechanic is at that machine working.
	var working := _mechanic != null and not _mechanic.is_walking() and _mech_at != ""
	for j in _mech_jobs:
		if working and j["machine"] == _mech_at and float(j["left"]) > 0.0:
			j["left"] = float(j["left"]) - delta
	_mech_jobs = _mech_jobs.filter(func(j: Dictionary) -> bool:
		return float(j["left"]) < 0.0 or float(j["left"]) > 0.0)
	var still_here := _mech_jobs.any(func(j: Dictionary) -> bool: return j["machine"] == _mech_at)
	if _mechanic != null and not is_instance_valid(_mechanic):
		_mechanic = null
	if _mech_jobs.is_empty():
		if _mechanic != null:
			_leave(_mechanic)
			_mechanic = null
			_mech_at = ""
		return
	if _mechanic != null and still_here:
		return
	var next: Dictionary = _mech_jobs[0]
	if _mechanic == null:
		_mechanic = _spawn("mechanic", str(next["machine"]))
	_mech_at = str(next["machine"])
	_walk_to_machine(_mechanic, _mech_at, -1, "repair", bool(next["run"]))


# Janitor ---------------------------------------------------------------------------------------

func _call_janitor(bin: String) -> void:
	if not factory_floor.footprints.has(bin):
		return
	if bin == _jan_at and _janitor != null:
		_sweep_left = minf(_sweep_left + SWEEP_SECONDS * 0.6, SWEEP_MAX)
		return
	if not _bins.has(bin):
		_bins.append(bin)


func _update_janitor(delta: float) -> void:
	if _janitor != null and not is_instance_valid(_janitor):
		_janitor = null
	if _janitor != null and _jan_at != "":
		if _janitor.is_walking():
			return
		_sweep_left -= delta
		if _sweep_left > 0.0:
			return
		_jan_at = ""
	if _bins.is_empty():
		if _janitor != null:
			_leave(_janitor)
			_janitor = null
		return
	if _janitor == null:
		_janitor = _spawn("janitor", str(_bins[0]))
	_jan_at = str(_bins.pop_front())
	_sweep_left = SWEEP_SECONDS
	_walk_to_machine(_janitor, _jan_at, -2, "sweep", false)


# Walking ---------------------------------------------------------------------------------------

## Someone comes in through the door nearest `machine` (by walking path).
func _spawn(kind: String, machine := "") -> Npc:
	var npc := Npc.new()
	npc.setup(kind, _frames[kind], _band if kind == "worker" else null)
	npc.rotation = -PI / 2.0
	if _doors.is_empty():
		# No doors: they appear (fading in) where they will work.
		var spot := _stand_spot(machine, 0) if factory_floor.footprints.has(machine) else {}
		var at: Vector2i = spot.get("approach", factory_floor.cell_at(_machine_mid(machine)))
		npc.position = factory_floor.cell_center(at)
		npc.set_meta("approach", at)
	else:
		var door := _nearest_door(machine)
		npc.position = factory_floor.cell_center(door)
		npc.set_meta("door", door)
		npc.set_meta("approach", door)
	add_child(npc)
	return npc


## Back out through the door they came in by (or the nearest one, if that door is gone).
func _leave(npc: Npc) -> void:
	if npc == null or not is_instance_valid(npc) or npc.is_leaving():
		return
	npc.remove_meta("machine")
	if _doors.is_empty():
		npc.leave([])  # no doors: they fade out where they stand
		return
	var door := _door_of(npc)
	var from: Vector2i = npc.get_meta("approach", door)
	var points: Array = []
	if npc.is_walking():
		from = factory_floor.cell_at(npc.position)
	else:
		points.append(factory_floor.cell_center(from))
	if not _doors.has(door):
		door = _nearest_door_from(from)
	var cells := _path_cells(from, door)
	points.append_array(_centres(cells))
	if cells.is_empty() or cells[-1] == door:
		points.append(factory_floor.cell_center(door))
	npc.leave(points)


func _door_of(npc: Npc) -> Vector2i:
	var d = npc.get_meta("door") if npc.has_meta("door") else null
	return d if d is Vector2i else (_doors[0] if not _doors.is_empty() else _door_cell)


## The door with the shortest walk to `machine`'s stand spot (any door when "" or none can reach).
func _nearest_door(machine: String) -> Vector2i:
	if _doors.is_empty():
		return _door_cell
	if _doors.size() == 1 or machine == "" or not factory_floor.footprints.has(machine):
		return _doors[0]
	var tl: Vector2i = factory_floor.footprints[machine]
	var goals: Array = []
	for c in [tl + Vector2i(0, 2), tl + Vector2i(1, 2), tl + Vector2i(0, -1), tl + Vector2i(1, -1),
			tl + Vector2i(-1, 0), tl + Vector2i(-1, 1), tl + Vector2i(2, 0), tl + Vector2i(2, 1)]:
		if _walkable(c):
			goals.append(c)
	var best: Vector2i = _doors[0]
	var best_len := INF
	var mid := Vector2(tl) + Vector2.ONE
	for d in _doors:
		var length := INF
		for g in goals:
			var path := _astar.get_id_path(d, g, false)
			if not path.is_empty():
				length = minf(length, float(path.size()))
		if is_inf(length):
			length = 100000.0 + Vector2(d).distance_to(mid)  # can't get there: straight line decides
		if length < best_len:
			best_len = length
			best = d
	return best


## The door nearest cell `c` by walking path.
func _nearest_door_from(c: Vector2i) -> Vector2i:
	var best: Vector2i = _doors[0] if not _doors.is_empty() else _door_cell
	var best_len := INF
	for d in _doors:
		var path: Array[Vector2i] = []
		if _astar.is_in_boundsv(c):
			path = _astar.get_id_path(c, d, false)
		var length := float(path.size()) if not path.is_empty() else 100000.0 + Vector2(d).distance_to(Vector2(c))
		if length < best_len:
			best_len = length
			best = d
	return best


## slot >= 0: a worker spot; -1: the mechanic's spot; -2: the janitor's (a bin).
func _walk_to_machine(npc: Npc, key: String, slot: int, loop: String, run: bool) -> void:
	var spot := _stand_spot(key, slot)
	var from: Vector2i = npc.get_meta("approach", _door_of(npc))
	var points: Array = []
	if npc.is_walking():
		from = factory_floor.cell_at(npc.position)
		if _astar.is_in_boundsv(from) and _astar.is_point_solid(from):
			from = npc.get_meta("approach", _door_of(npc))
	else:
		points.append(factory_floor.cell_center(from))
	var cells := _path_cells(from, spot["approach"])
	points.append_array(_centres(cells))
	var face: float = spot["face"]
	var stop: Vector2i = spot["approach"]
	if cells.is_empty() or cells[-1] == spot["approach"]:
		points.append(spot["point"])
	else:
		# No way in without crossing a belt: stop as close as we can get and work from there.
		stop = cells[-1]
		var tl: Vector2i = factory_floor.footprints.get(key, Vector2i.ZERO)
		var mid := Vector2(tl) * CELL + Vector2(CELL, CELL)
		face = (mid - factory_floor.cell_center(stop)).angle()
	npc.set_meta("approach", stop)
	npc.set_meta("machine", key)
	npc.set_meta("slot", slot)
	npc.walk(points, loop, face, run)


## Where someone stands to work at a machine or station (2x2 footprint): just outside one of
## its corners, facing it, clear of the name above and the progress bar below. Workers and the
## janitor prefer the bottom-left corner, the mechanic the bottom-right; a corner that can be
## reached from the staff door comes first, then one whose floor cells hold no belt. Also the
## free floor cell they walk
## to first. slot >= 0: a worker (more workers line up further out); -1: the mechanic;
## -2: the janitor.
func _stand_spot(key: String, slot: int) -> Dictionary:
	var tl: Vector2i = factory_floor.footprints.get(key, Vector2i.ZERO)
	var origin := Vector2(tl) * CELL
	var out := 36.0 * float(maxi(slot, 0) % 3)
	var order := ["se", "sw", "ne", "nw"] if slot == -1 else ["sw", "se", "nw", "ne"]
	var best := {}
	var best_rank := 99
	for corner in order:
		var east: bool = corner.ends_with("e")
		var south: bool = corner.begins_with("s")
		var x := (2 * CELL + 6.0 + out) if east else (-6.0 - out)
		var y := (2 * CELL + 14.0) if south else -14.0
		var row := tl.y + 2 if south else tl.y - 1
		var cells := [Vector2i(tl.x + (1 if east else 0), row), Vector2i(tl.x + (0 if east else 1), row)]
		var point := origin + Vector2(x, y)
		var face := (origin + Vector2(CELL, CELL) - point).angle()
		for c in cells:
			if not _walkable(c):
				continue
			var stands_on := factory_floor.cell_at(point)
			var rank := 0
			if not _reachable(c):
				rank += 2
			if factory_floor.belt_cells.has(c) or factory_floor.belt_cells.has(stands_on):
				rank += 1
			if rank < best_rank:
				best_rank = rank
				best = {"point": point, "approach": c, "face": face}
			if rank == 0:
				return best
	if not best.is_empty():
		return best
	return {"point": origin + Vector2(-6.0, 2 * CELL + 14.0), "approach": Vector2i(tl.x, tl.y + 2),
			"face": -PI * 0.25}


func _walkable(c: Vector2i) -> bool:
	return _astar.is_in_boundsv(c) and not _astar.is_point_solid(c)


## Whether people can walk from the staff door to cell `c` without crossing a belt.
func _reachable(c: Vector2i) -> bool:
	if not _walkable(c):
		return false
	if _doors.is_empty():
		return true  # no doors: people appear where they work
	for d in _doors:
		if _astar.is_in_boundsv(d) and not _astar.get_id_path(d, c, false).is_empty():
			return true
	return false


## The cells to walk from `from` towards `to` (leaving out `from`). When `to` can't be reached,
## it ends at the reachable cell closest to it; empty when out of bounds or already there.
func _path_cells(from: Vector2i, to: Vector2i) -> Array:
	if not (_astar.is_in_boundsv(from) and _astar.is_in_boundsv(to)):
		return []
	var cells: Array = Array(_astar.get_id_path(from, to, true))
	if not cells.is_empty():
		cells.pop_front()
	return cells


func _centres(cells: Array) -> Array:
	var out: Array = []
	for c in cells:
		out.append(factory_floor.cell_center(c))
	return out




## Rebuild the walking grid when the layout changed (machines, stations or belts moved).
func _ensure_grid() -> void:
	if factory_floor == null:
		return
	var sig := "%s|%s|%d|%s|%d|%d|%d|%s" % [factory_floor.bounds, factory_floor.footprints, hash(factory_floor.belt_cells.keys()), _door_saved,
			hash(factory_floor.room_walls), hash(_room_bridges().keys()), hash(_room_doors().keys()), _staff_door_wanted()]
	if sig == _grid_sig:
		return
	_grid_sig = sig
	var b: Rect2i = factory_floor.bounds
	_door_cell = Vector2i(b.position.x, b.end.y)
	if _door_saved is Vector2i and _door_spot_free(_door_saved):
		_door_cell = _door_saved
	_doors = []
	for c in _room_doors():
		if c is Vector2i and _door_spot_free(c):
			_doors.append(c)
	_using_placed = not _doors.is_empty()
	if _doors.is_empty() and _staff_door_wanted():
		_doors = [_door_cell]
	var region := b.grow(MARGIN)
	for c in _doors:
		region = region.merge(Rect2i(c, Vector2i.ONE).grow(1))
	_astar.region = region
	queue_redraw()
	_astar.cell_size = Vector2(CELL, CELL)
	_astar.diagonal_mode = AStarGrid2D.DIAGONAL_MODE_ONLY_IF_NO_OBSTACLES
	_astar.default_compute_heuristic = AStarGrid2D.HEURISTIC_OCTILE
	_astar.default_estimate_heuristic = AStarGrid2D.HEURISTIC_OCTILE
	_astar.update()
	# update() keeps the old points when the region didn't change: start every cell afresh.
	_astar.fill_solid_region(_astar.region, false)
	_astar.fill_weight_scale_region(_astar.region, 1.0)
	for key in factory_floor.footprints:
		var tl: Vector2i = factory_floor.footprints[key]
		for dx in 2:
			for dy in 2:
				var c := tl + Vector2i(dx, dy)
				if _astar.is_in_boundsv(c):
					_astar.set_point_solid(c, true)
	# Room walls, windows and belt ports stop people; they go through doors.
	for c in factory_floor.room_walls:
		if _astar.is_in_boundsv(c) and str(factory_floor.room_walls[c].get("kind", "")) != "door":
			_astar.set_point_solid(c, true)
	_place_bridges()
	for c in _doors:
		if _astar.is_in_boundsv(c):
			_astar.set_point_solid(c, false)


## Close every belt cell to people except where you placed a footbridge (only on a straight
## belt cell: corners, junctions and crossovers stay closed). None are added for you.
func _place_bridges() -> void:
	_bridges.clear()
	var belts: Dictionary = factory_floor.belt_cells
	var placed := _room_bridges()
	for c in belts:
		if not _astar.is_in_boundsv(c) or _astar.is_point_solid(c):
			continue
		if placed.has(c) and _straight(belts[c]):
			_bridges[c] = true
			_astar.set_point_weight_scale(c, 1.0)
		else:
			_astar.set_point_solid(c, true)


## The worker doors placed with the Rooms bar: cell -> true (none without room_layer.gd).
func _room_doors() -> Dictionary:
	var rooms = factory_floor.get("room_painter")
	if rooms is Object and is_instance_valid(rooms):
		var d = rooms.get("staff_doors")
		if d is Dictionary:
			return d
	return {}


## True while the first staff door is in use (no worker doors placed, and not removed).
func _placed_doors() -> bool:
	return _using_placed or _doors.is_empty()


func _staff_door_wanted() -> bool:
	var rooms = factory_floor.get("room_painter")
	if rooms is Object and is_instance_valid(rooms):
		var on = rooms.get("staff_door_on")
		if on is bool:
			return on
	return true


## Where the first staff door stands (room_layer.gd erases it there).
func staff_door_cell() -> Vector2i:
	_ensure_grid()
	return _door_cell


## The footbridges placed with the Rooms bar: cell -> true (none without room_layer.gd).
func _room_bridges() -> Dictionary:
	var rooms = factory_floor.get("room_painter")
	if rooms is Object and is_instance_valid(rooms):
		var b = rooms.get("bridges")
		if b is Dictionary:
			return b
	return {}


## A belt cell that runs straight through (one way in, out the opposite side).
func _straight(cell: Dictionary) -> bool:
	var ins: Array = cell.get("in", [])
	const OPP := {"n": "s", "s": "n", "e": "w", "w": "e"}
	return ins.size() == 1 and OPP.get(str(ins[0]), "") == str(cell.get("out", ""))


func _any_crate(test: Callable) -> bool:
	for c in _crates.values():
		if test.call(c):
			return true
	return false


func _process(delta: float) -> void:
	if factory_floor == null:
		return
	if factory_floor.editing != _was_editing:
		_was_editing = factory_floor.editing
		queue_redraw()
	# In build mode the people step out of the way; the door stays so you can move it.
	for npc in get_children():
		if npc is Npc:
			npc.visible = not factory_floor.editing
	if not factory_floor.editing and (_door_drag or _door_hover):
		_door_drag = false
		_door_hover = false
		queue_redraw()
	_grid_tick += delta
	if _grid_tick >= 0.5:
		_grid_tick = 0.0
		_ensure_grid()

	for key in _ready_left.keys():
		_ready_left[key] = float(_ready_left[key]) - delta
		if float(_ready_left[key]) <= 0.0:
			_send_home(key)
	for key in _finishing.keys():
		_finishing[key] = float(_finishing[key]) - delta
		if float(_finishing[key]) <= 0.0:
			_send_home(key)
	for key in _workers.keys():
		if not is_instance_valid(_workers[key]):
			_workers.erase(key)
	_update_mechanic(delta)
	_update_janitor(delta)

	# Each door slides open while someone is near it.
	for d in _doors:
		var near := false
		var door := factory_floor.cell_center(d)
		for npc in get_children():
			if npc is Npc and npc.position.distance_to(door) < DOOR_REACH:
				near = true
				break
		_door_open[d] = move_toward(float(_door_open.get(d, 0.0)), 1.0 if near else 0.0, delta * 4.0)
		var frame := clampi(roundi(float(_door_open[d]) * 3.0), 0, 3)
		if frame != int(_door_frame.get(d, 0)):
			_door_frame[d] = frame
			queue_redraw()


# The staff door ---------------------------------------------------------------------------------

## True when the door may stand on cell c: no machine, station or belt there.
func _door_spot_free(c: Vector2i) -> bool:
	if factory_floor.belt_cells.has(c) or factory_floor.room_walls.has(c):
		return false
	for key in factory_floor.footprints:
		if Rect2i(factory_floor.footprints[key], Vector2i(2, 2)).has_point(c):
			return false
	return true


## Build mode: drag the door to another free floor cell. Runs before main.gd's own input
## (children first), and only takes clicks that land on the door.
func _unhandled_input(event: InputEvent) -> void:
	if factory_floor == null or not factory_floor.editing:
		return
	var mouse := event as InputEventMouse
	if mouse == null:
		return
	var at: Vector2 = (make_input_local(mouse) as InputEventMouse).position
	var cell := factory_floor.cell_at(at)
	var on_door := _doors.has(cell)
	var motion := event as InputEventMouseMotion
	if motion:
		if _door_drag:
			_door_ghost = cell
			queue_redraw()
			get_viewport().set_input_as_handled()
		elif on_door != _door_hover or (on_door and cell != _hover_cell):
			_door_hover = on_door
			_hover_cell = cell
			queue_redraw()
		return
	var button := event as InputEventMouseButton
	if button == null or button.button_index != MOUSE_BUTTON_LEFT:
		return
	if button.pressed and on_door and not Input.is_physical_key_pressed(KEY_SPACE) \
			and not Input.is_key_pressed(KEY_SHIFT):
		_door_drag = true
		_drag_from = cell
		_door_ghost = cell
		queue_redraw()
		get_viewport().set_input_as_handled()
	elif not button.pressed and _door_drag:
		_door_drag = false
		var picked := _drag_from
		if _door_ghost != _drag_from and _door_spot_free(_door_ghost) and not _doors.has(_door_ghost):
			if _using_placed:
				# One of your worker doors: the Rooms bar keeps it (an undoable edit).
				var rooms = factory_floor.get("room_painter")
				if rooms is Object and rooms.has_method("move_door") and rooms.move_door(_drag_from, _door_ghost):
					picked = _door_ghost
			else:
				_door_saved = _door_ghost
				var cfg := ConfigFile.new()
				cfg.set_value("door", "cell", _door_ghost)
				cfg.save(DOOR_FILE)
				picked = _door_ghost
			_ensure_grid()
		# Clicked or dragged: the door stays picked in the build panel, so Delete removes it.
		var panel = get_parent().get("build_panel") if get_parent() else null
		if panel is Object and panel.has_method("pick_room_cell"):
			panel.pick_room_cell(picked)
		queue_redraw()
		get_viewport().set_input_as_handled()


func _draw() -> void:
	if factory_floor == null:
		return
	if _bridge_texture:
		for c in _bridges:
			var cell: Dictionary = factory_floor.belt_cells.get(c, {})
			var mid := factory_floor.cell_center(c)
			# The sprite's deck runs north-south (for an east-west belt); turn it for the others.
			var turn := str(cell.get("out", "e")) in ["n", "s"]
			draw_set_transform(mid, PI / 2.0 if turn else 0.0)
			draw_texture(_bridge_texture, Vector2(-CELL / 2.0, -CELL / 2.0))
		draw_set_transform(Vector2.ZERO)
	for d in _doors:
		draw_texture(_door_textures[int(_door_frame.get(d, 0))], Vector2(d) * CELL)
	var origin := Vector2(_hover_cell) * CELL
	if not factory_floor.editing or _doors.is_empty():
		return
	if _door_drag:
		var ok := _door_spot_free(_door_ghost) and (_door_ghost == _drag_from or not _doors.has(_door_ghost))
		var r := Rect2(Vector2(_door_ghost) * CELL, Vector2(CELL, CELL))
		draw_texture_rect(_door_textures[0], r, false, Color(1, 1, 1, 0.55))
		draw_rect(r.grow(2), COLOR_PASS if ok else COLOR_FAIL, false, 3.0)
	elif _door_hover:
		draw_rect(Rect2(origin, Vector2(CELL, CELL)).grow(3), COLOR_AMBER, false, 3.0)
		draw_string(ThemeDB.fallback_font, origin + Vector2(-120, CELL + 18), "Door: drag to move, click then Delete to remove",
				HORIZONTAL_ALIGNMENT_CENTER, CELL + 240, 13, COLOR_AMBER)
