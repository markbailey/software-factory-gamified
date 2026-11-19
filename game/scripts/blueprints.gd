extends RefCounted
## Copy, paste and blueprints for build mode (build_panel.gd).
##
## A clip is the part of the layout you picked, kept on its own so it can be pasted anywhere:
## machines with all their settings (name, prompt, gate, options, rules, scrap reasons...),
## intakes, storages, shipping docks and scrap bins, the belts that run between two of them,
## and any room blocks picked with them. Positions count from the clip's top-left cell, so it
## can be turned (R) and pasted anywhere. Pasted things get new ids; belts to things outside
## the clip are left out (lay them again to join the copy up).
##
## A line comes along as a new line only when its intake was picked: it keeps the line's
## settings (repo, accounts, check only, crate limit...) and is named "<name> copy". Machines
## picked without their intake go onto the line picked in the build panel, the same as a
## machine placed from the palette.
##
## A blueprint is a clip with a name, kept in user://blueprints.json so it outlives build mode
## and the game (not in the layout the backend keeps).
##
## A clip:
## {"version": 1, "size": [w, h],
##  "parts": [{"ref": "p0", "line": "L0", "kind": "machine"|"intake"|"storage"|"dock"|"bin",
##             "node": {... as in the layout, "pos" from the clip's top-left, no "id"}}],
##  "lines": {"L0": {"intake": bool, "settings": {the line's own settings}}},
##  "belts": [{"from": "p0", "port": "pass", "to": "p1", "path": [[x, y]...], "manual": true}],
##  "rooms": {"walls": [[x, y, kind]], "floors": [...], "labels": [...], "bridges": [[x, y]],
##            "staff_doors": [[x, y]]}}

const FactoryFloor := preload("res://scripts/factory_floor.gd")

const SAVE_FILE := "user://blueprints.json"
## A line's own keys that aren't its settings: what's on it, and what the backend works out.
## "sources" (typed specs) stays behind too: two lines can't both take them.
const NOT_SETTINGS := ["id", "machines", "belts", "storages", "bins", "docks", "intake", "dock", "scrap",
		"conveyors", "paused", "problem", "sources"]
const ROOM_ROWS := ["walls", "floors", "labels", "bridges", "staff_doors"]
const SOLID_ROOM_ROWS := ["walls", "bridges", "staff_doors"]  # can't sit under a machine


## Take a clip of these footprint keys and room block cells (the build panel's picked group).
## Empty if nothing picked is there any more.
static func capture(panel: Object, keys: Array, room_cells: Array) -> Dictionary:
	var floor_view = panel.floor_view
	var picked: Array = []
	for k in keys:
		if floor_view.footprints.has(k) and not (panel._node(str(k)) as Dictionary).is_empty():
			picked.append(str(k))
	if picked.is_empty() and room_cells.is_empty():
		return {}
	var box := Rect2i()
	var first := true
	for k: String in picked:
		var r := Rect2i(floor_view.footprints[k], Vector2i(2, 2))
		box = r if first else box.merge(r)
		first = false
	for c in room_cells:
		var r := Rect2i(c, Vector2i.ONE)
		box = r if first else box.merge(r)
		first = false
	var origin := box.position

	var parts: Array = []
	var refs := {}  # footprint key -> part ref
	var lines := {}  # pipeline id -> line ref
	for k: String in picked:
		var pid := str(panel._pid_of(k))
		if not lines.has(pid):
			lines[pid] = "L%d" % lines.size()
		var node: Dictionary = (panel._node(k) as Dictionary).duplicate(true)
		node.erase("id")
		var tl: Vector2i = floor_view.footprints[k] - origin
		node["pos"] = [tl.x, tl.y]
		var kind := "machine"
		if ":" in k:
			var place := k.get_slice(":", 1)
			if place == "intake":
				kind = "intake"
			elif FactoryFloor.is_storage(place):
				kind = "storage"
			elif FactoryFloor.is_dock(place):
				kind = "dock"
			else:
				kind = "bin"
		var ref := "p%d" % parts.size()
		refs[k] = ref
		parts.append({"ref": ref, "line": lines[pid], "kind": kind, "node": node})

	var line_info := {}
	for pid in lines:
		var p: Dictionary = panel._pipeline(pid)
		var settings := {}
		for key in p:
			if not str(key) in NOT_SETTINGS:
				settings[key] = p[key].duplicate(true) if (p[key] is Dictionary or p[key] is Array) else p[key]
		line_info[lines[pid]] = {"intake": refs.has("%s:intake" % pid), "settings": settings}

	var belts: Array = []
	for p in panel.draft:
		var pid := str(p.get("id", ""))
		for b in p.get("belts", []):
			var ends: Array = panel._belt_ends(pid, b)
			if not (refs.has(ends[0]) and refs.has(ends[1])):
				continue
			var nb := {"from": refs[ends[0]], "port": str(b.get("port", "")), "to": refs[ends[1]]}
			var path = b.get("path")
			if b.get("manual", false) and path is Array:
				var cells: Array = []
				for c in path:
					cells.append([int(c[0]) - origin.x, int(c[1]) - origin.y])
				nb["path"] = cells
				nb["manual"] = true
			belts.append(nb)

	var rooms := {}
	var layer = panel._rooms()
	if not room_cells.is_empty() and layer and layer.has_method("copy_cells"):
		rooms = layer.copy_cells(room_cells, origin)
	return {"version": 1, "size": [box.size.x, box.size.y], "parts": parts, "lines": line_info,
			"belts": belts, "rooms": rooms}


## The clip turned `step` quarter turns (1 clockwise, -1 back), still counted from its top-left.
static func turned(clip: Dictionary, step: int) -> Dictionary:
	var out := clip.duplicate(true)
	for i in posmod(step, 4):
		var w := int(out["size"][0])
		var h := int(out["size"][1])
		# Clockwise: column x becomes row x, and row y becomes column h - 1 - y.
		for part in out.get("parts", []):
			var n: Dictionary = part["node"]
			n["pos"] = [h - 2 - int(n["pos"][1]), int(n["pos"][0])]  # a 2x2 footprint
			var turns := posmod(FactoryFloor.turns_of(n) + 1, 4)
			if turns == 0:
				n.erase("rot")
			else:
				n["rot"] = turns
		for b in out.get("belts", []):
			if b.get("path") is Array:
				var cells: Array = []
				for c in b["path"]:
					cells.append([h - 1 - int(c[1]), int(c[0])])
				b["path"] = cells
		var rooms: Dictionary = out.get("rooms", {})
		for name in rooms:
			var rows: Array = []
			for row in rooms[name]:
				var moved: Array = (row as Array).duplicate()
				moved[0] = h - 1 - int(row[1])
				moved[1] = int(row[0])
				rows.append(moved)
			rooms[name] = rows
		out["size"] = [h, w]
	return out


static func size_of(clip: Dictionary) -> Vector2i:
	var s = clip.get("size", [2, 2])
	return Vector2i(int(s[0]), int(s[1])) if s is Array and s.size() == 2 else Vector2i(2, 2)


## What the floor draws while the clip follows the mouse: each footprint (and which way it
## faces) and the cells of its belts and room blocks, with `at` its top-left.
static func ghost(panel: Object, clip: Dictionary, at: Vector2i) -> Dictionary:
	var parts: Array = []
	for part in clip.get("parts", []):
		var n: Dictionary = part["node"]
		parts.append({"pos": at + Vector2i(int(n["pos"][0]), int(n["pos"][1])), "rot": FactoryFloor.turns_of(n)})
	var cells: Array = []
	for b in clip.get("belts", []):
		for c in b.get("path", []):
			cells.append(at + Vector2i(int(c[0]), int(c[1])))
	var rooms: Dictionary = clip.get("rooms", {})
	for name in rooms:
		for row in rooms[name]:
			cells.append(at + Vector2i(int(row[0]), int(row[1])))
	return {"pos": at, "size": size_of(clip), "ok": fits(panel, clip, at), "parts": parts, "cells": cells}


## Whether the clip could be pasted with its top-left at `at`: no footprint on anything else
## or on a wall, and no wall, door or footbridge of it under a machine or on another block.
static func fits(panel: Object, clip: Dictionary, at: Vector2i) -> bool:
	var floor_view = panel.floor_view
	var landing := {}
	for part in clip.get("parts", []):
		var n: Dictionary = part["node"]
		var tl := at + Vector2i(int(n["pos"][0]), int(n["pos"][1]))
		if not floor_view.is_free(tl):
			return false
		for c in [tl, tl + Vector2i(1, 0), tl + Vector2i(0, 1), tl + Vector2i(1, 1)]:
			landing[c] = true
	var rooms: Dictionary = clip.get("rooms", {})
	var layer = panel._rooms()
	if not rooms.is_empty() and layer and layer.has_method("can_paste"):
		return layer.can_paste(rooms, at, landing)
	return true


## Paste the clip with its top-left at `at` into the build panel's draft. Returns the footprint
## keys of what was placed. The room blocks are pasted too (the caller records the undo step).
static func paste(panel: Object, clip: Dictionary, at: Vector2i) -> Dictionary:
	var line_for := {}  # line ref -> pipeline id
	var lines: Dictionary = clip.get("lines", {})
	for lref in lines:
		if not lines[lref].get("intake", false):
			line_for[lref] = str(panel._line_for_placing().get("id", ""))  # the line picked
	for lref in lines:
		if lines[lref].get("intake", false):
			line_for[lref] = _new_line(panel, lines[lref].get("settings", {}))

	var ends := {}  # part ref -> [pipeline id, end name, footprint key]
	var keys: Array = []
	for part in clip.get("parts", []):
		var pid := str(line_for.get(str(part.get("line", "")), ""))
		var p: Dictionary = panel._pipeline(pid)
		if p.is_empty():
			continue
		var node: Dictionary = (part["node"] as Dictionary).duplicate(true)
		var tl := at + Vector2i(int(node["pos"][0]), int(node["pos"][1]))
		node["pos"] = [tl.x, tl.y]
		var name := ""
		match str(part.get("kind", "")):
			"machine":
				name = str(panel._new_id(str(node.get("type", "machine"))))
				node["id"] = name
				_append(p, "machines", node)
			"intake":
				name = "intake"
				p["intake"] = node
			"storage":
				name = _free_storage_id(panel)
				node["id"] = name
				_append(p, "storages", node)
			"dock", "bin":
				var dock := str(part["kind"]) == "dock"
				var station := "dock" if dock else "scrap"
				if not p.get(station) is Dictionary:
					name = station
					p[station] = node
				else:
					var taken: Array = FactoryFloor.docks_of(p) if dock else FactoryFloor.bins_of(p)
					var n := 2
					while ("%s_%d" % [station, n]) in taken:
						n += 1
					name = "%s_%d" % [station, n]
					node["id"] = name
					_append(p, "docks" if dock else "bins", node)
		if name.is_empty():
			continue
		var key := name if str(part.get("kind", "")) == "machine" else "%s:%s" % [pid, name]
		ends[str(part["ref"])] = [pid, name, key]
		keys.append(key)

	for b in clip.get("belts", []):
		if not ends.has(str(b.get("from", ""))) or not ends.has(str(b.get("to", ""))):
			continue
		var src: Array = ends[str(b["from"])]
		var dst: Array = ends[str(b["to"])]
		var p: Dictionary = panel._pipeline(src[0])  # a belt belongs to the line it leaves
		var nb := {"from": src[1], "port": str(b.get("port", "")), "to": panel._belt_target(src[0], dst[2])}
		if b.get("path") is Array:
			var cells: Array = []
			for c in b["path"]:
				cells.append([int(c[0]) + at.x, int(c[1]) + at.y])
			nb["path"] = cells
			nb["manual"] = true
		_append(p, "belts", nb)

	var rooms_changed := false
	var rooms: Dictionary = clip.get("rooms", {})
	var layer = panel._rooms()
	if not rooms.is_empty() and layer and layer.has_method("paste_cells"):
		rooms_changed = bool(layer.paste_cells(rooms, at))
	var cells := {}
	for rows in rooms.values():
		for row in rows:
			cells[at + Vector2i(int(row[0]), int(row[1]))] = true
	return {"keys": keys, "room_cells": cells, "rooms_changed": rooms_changed}


static func _append(p: Dictionary, list_key: String, item) -> void:
	var list: Array = p.get(list_key, [])
	list.append(item)
	p[list_key] = list


static func _free_storage_id(panel: Object) -> String:
	var taken := {}
	for q in panel.draft:
		for st in FactoryFloor.storages_of(q):
			taken[str(st.get("id", ""))] = true
	var n := 1
	while taken.has("store_%d" % n):
		n += 1
	return "store_%d" % n


## A new, empty line with these settings, named after the one copied.
static func _new_line(panel: Object, settings: Dictionary) -> String:
	var ids := {}
	for q in panel.draft:
		ids[str(q.get("id", ""))] = true
	var n: int = panel.draft.size() + 1
	while ids.has("line-%d" % n):
		n += 1
	var line: Dictionary = settings.duplicate(true)
	line["id"] = "line-%d" % n
	line["name"] = "%s copy" % str(settings.get("name", "Line")) if settings.has("name") else "Line %d" % n
	line["sources"] = []
	line["storages"] = []
	line["machines"] = []
	line["belts"] = []
	panel.draft.append(line)
	return str(line["id"])


## Short words for what a clip holds: "3 machines, 1 intake, 4 belts".
static func words(clip: Dictionary) -> String:
	var counts := {}
	for part in clip.get("parts", []):
		var k := str(part.get("kind", ""))
		counts[k] = int(counts.get(k, 0)) + 1
	var out := PackedStringArray()
	for k in ["machine", "intake", "storage", "dock", "bin"]:
		var n := int(counts.get(k, 0))
		if n > 0:
			var word: String = {"machine": "machine", "intake": "intake", "storage": "storage",
					"dock": "shipping dock", "bin": "scrap bin"}[k]
			out.append("%d %s%s" % [n, word, "" if n == 1 else "s"])
	var belts := (clip.get("belts", []) as Array).size()
	if belts > 0:
		out.append("%d belt%s" % [belts, "" if belts == 1 else "s"])
	var blocks := 0
	var rooms: Dictionary = clip.get("rooms", {})
	for name in rooms:
		blocks += (rooms[name] as Array).size()
	if blocks > 0:
		out.append("%d room block%s" % [blocks, "" if blocks == 1 else "s"])
	return ", ".join(out) if not out.is_empty() else "nothing"


# Blueprints on disk ---------------------------------------------------------------

## [{"name": String, "clip": Dictionary}], as saved.
static func load_all() -> Array:
	if not FileAccess.file_exists(SAVE_FILE):
		return []
	var data = JSON.parse_string(FileAccess.get_file_as_string(SAVE_FILE))
	if not data is Dictionary:
		return []
	var out: Array = []
	for bp in data.get("blueprints", []):
		if bp is Dictionary and bp.get("clip") is Dictionary and not str(bp.get("name", "")).strip_edges().is_empty():
			out.append({"name": str(bp["name"]), "clip": bp["clip"]})
	return out


static func save_all(list: Array) -> bool:
	var file := FileAccess.open(SAVE_FILE, FileAccess.WRITE)
	if file == null:
		return false
	file.store_string(JSON.stringify({"version": 1, "blueprints": list}, "\t"))
	return true
