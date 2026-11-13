extends Node2D
## Walking skeleton: renders the backend's state and sends specs to it.
## Nothing here decides where a crate goes; it only applies backend events.

const BackendClient := preload("res://scripts/backend_client.gd")
const FactoryFloor := preload("res://scripts/factory_floor.gd")
const Crate := preload("res://scripts/crate.gd")

const BACKEND_URL := "ws://127.0.0.1:8765/ws"
const LOG_HINT := "Log: click a crate to see its output"
const SCATTERED := ["intake", "dock", "scrap"]

var client: BackendClient
var factory_floor: FactoryFloor
var crates_layer: Node2D
var crates := {}  # item_id -> Crate
var logs := {}  # item_id -> Array of log lines
var selected_id := -1
var last_seq := 0

var camera: Camera2D
var spec_input: LineEdit
var status_label: Label
var log_title: Label
var log_view: RichTextLabel


func _ready() -> void:
	factory_floor = FactoryFloor.new()
	add_child(factory_floor)
	crates_layer = Node2D.new()
	add_child(crates_layer)

	camera = Camera2D.new()
	add_child(camera)
	camera.make_current()
	get_viewport().size_changed.connect(_fit_camera)

	_build_ui()

	client = BackendClient.new()
	client.url = BACKEND_URL
	client.connected.connect(_on_connected)
	client.disconnected.connect(_on_disconnected)
	client.message_received.connect(_on_message)
	add_child(client)
	_set_status("Connecting to backend...")


# UI ------------------------------------------------------------------------

func _build_ui() -> void:
	var ui := CanvasLayer.new()
	add_child(ui)

	var top := PanelContainer.new()
	ui.add_child(top)
	top.set_anchors_and_offsets_preset(Control.PRESET_TOP_WIDE)
	var row := HBoxContainer.new()
	top.add_child(row)

	spec_input = LineEdit.new()
	spec_input.placeholder_text = "Type a spec and press Enter (include \"fail\" to test the scrap bin)"
	spec_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	spec_input.text_submitted.connect(func(_t: String) -> void: _submit())
	row.add_child(spec_input)

	var button := Button.new()
	button.text = "Feed to intake"
	button.pressed.connect(_submit)
	row.add_child(button)

	status_label = Label.new()
	status_label.custom_minimum_size = Vector2(240, 0)
	row.add_child(status_label)

	var bottom := PanelContainer.new()
	ui.add_child(bottom)
	bottom.set_anchors_and_offsets_preset(Control.PRESET_BOTTOM_WIDE)
	bottom.offset_top = -180
	var col := VBoxContainer.new()
	bottom.add_child(col)

	log_title = Label.new()
	log_title.text = LOG_HINT
	col.add_child(log_title)

	log_view = RichTextLabel.new()
	log_view.scroll_following = true
	log_view.selection_enabled = true
	log_view.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(log_view)


## Zoom and centre the camera so the whole line fits between the top bar and the log panel.
func _fit_camera() -> void:
	var view := get_viewport_rect().size
	var top_bar := 44.0
	var bottom_panel := 180.0
	var content := factory_floor.world_size()
	var zoom := minf(1.0, (view.x - 32.0) / content.x)
	camera.zoom = Vector2(zoom, zoom)
	# World rows 2 to 7.2 hold the labels, belts, machines and the scrap bin.
	var world_center_y := (2.0 + 7.2) / 2.0 * FactoryFloor.CELL
	var screen_offset := (top_bar + (view.y - top_bar - bottom_panel) / 2.0) - view.y / 2.0
	camera.position = Vector2(content.x / 2.0, world_center_y - screen_offset / zoom)


func _set_status(text: String) -> void:
	status_label.text = text


func _submit() -> void:
	var text := spec_input.text.strip_edges()
	if text.is_empty():
		return
	if client.send({"type": "submit_work", "text": text}):
		spec_input.clear()
	else:
		_set_status("Not connected. Is the backend running?")


func _unhandled_input(event: InputEvent) -> void:
	var click := event as InputEventMouseButton
	if click == null or not click.pressed or click.button_index != MOUSE_BUTTON_LEFT:
		return
	var p := get_global_mouse_position()
	for id in crates:
		var crate: Crate = crates[id]
		if crate.hit(p):
			_select(id)
			get_viewport().set_input_as_handled()
			return


# Backend events --------------------------------------------------------------

func _on_connected() -> void:
	_set_status("Connected")


func _on_disconnected() -> void:
	_set_status("Backend disconnected, retrying...")


func _on_message(msg: Dictionary) -> void:
	var kind: String = msg.get("type", "")
	if kind == "snapshot":
		_apply_snapshot(msg)
		return
	if kind == "error":
		_set_status("Backend: %s" % msg.get("message", ""))
		return

	var seq := int(msg.get("seq", 0))
	if seq <= last_seq:
		return
	last_seq = seq

	match kind:
		"item_created":
			_add_item(msg["item"])
		"item_moved":
			_on_item_moved(msg)
		"job_started":
			factory_floor.set_machine_state(str(msg.get("machine", "")), "running", 0.0)
			_select(int(msg["item_id"]))
		"job_log":
			_append_log(int(msg["item_id"]), str(msg.get("line", "")))
		"job_progress":
			factory_floor.set_machine_state(str(msg.get("machine", "")), "running",
					float(msg.get("progress", 0.0)), str(msg.get("step", "")))
		"job_finished":
			factory_floor.set_machine_state(str(msg.get("machine", "")), "done", 1.0)
		"job_failed":
			var machine_id := str(msg.get("machine", ""))
			factory_floor.set_machine_state(machine_id, "failed",
					factory_floor.machine_progress(machine_id), str(msg.get("step", "")))
		"pr_opened":
			_set_status("PR opened for crate #%d" % int(msg["item_id"]))


func _apply_snapshot(snap: Dictionary) -> void:
	for crate in crates.values():
		crate.queue_free()
	crates.clear()
	logs.clear()
	last_seq = int(snap.get("seq", 0))

	var layout: Dictionary = snap.get("layout", {})
	var machines: Array = layout.get("machines", [])
	factory_floor.set_layout(machines)
	_fit_camera()

	for item in snap.get("items", []):
		_add_item(item)
		if str(item.get("state", "")) == "running":
			factory_floor.set_machine_state(str(item.get("location", "")), "running", 0.0)
	_select(selected_id if crates.has(selected_id) else -1)


func _add_item(item: Dictionary) -> void:
	var id := int(item["id"])
	var crate := Crate.new()
	crate.item_id = id
	crate.text = str(item.get("text", ""))
	crate.source = _source_label(item)
	crate.set_state(str(item.get("state", "queued")))
	crate.position = _target_for(id, str(item.get("location", "intake")))
	crates_layer.add_child(crate)
	crates[id] = crate
	var lines: Array = []
	for line in item.get("log", []):
		lines.append(str(line))
	logs[id] = lines


func _on_item_moved(msg: Dictionary) -> void:
	var id := int(msg["item_id"])
	if not crates.has(id):
		return
	var crate: Crate = crates[id]
	if msg.has("state"):
		crate.set_state(str(msg["state"]))
	crate.move_to(_target_for(id, str(msg.get("to", ""))), float(msg.get("duration", 0.0)))


func _target_for(id: int, location: String) -> Vector2:
	var base := factory_floor.point(location)
	if location in SCATTERED or location.begins_with("belt_"):
		# Spread crates that share a spot so they don't hide each other.
		base += Vector2(float((id * 37) % 29) - 14.0, float((id * 53) % 29) - 14.0)
	return base


func _append_log(id: int, line: String) -> void:
	if not logs.has(id):
		logs[id] = []
	logs[id].append(line)
	if id == selected_id:
		log_view.add_text(line + "\n")


func _select(id: int) -> void:
	if crates.has(selected_id):
		var old: Crate = crates[selected_id]
		old.selected = false
	selected_id = id
	log_view.clear()
	if not crates.has(id):
		log_title.text = LOG_HINT
		return
	var crate: Crate = crates[id]
	crate.selected = true
	var title := crate.text.get_slice("\n", 0)
	if crate.source.is_empty():
		log_title.text = "Crate #%d: %s" % [id, title]
	else:
		log_title.text = "Crate #%d (%s): %s" % [id, crate.source, title]
	for line in logs.get(id, []):
		log_view.add_text(str(line) + "\n")


func _source_label(item: Dictionary) -> String:
	var ref := str(item.get("source_ref", ""))
	match str(item.get("source", "")):
		"github":
			return "GitHub #" + ref
		"jira":
			return "Jira " + ref
	return ""
