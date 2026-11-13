extends Node2D
## Renders the backend's state and sends it your commands (specs, approve, reject, retry,
## and a new line of machines from build mode).
## Nothing here decides where a crate goes; it only applies backend events.

const BackendClient := preload("res://scripts/backend_client.gd")
const FactoryFloor := preload("res://scripts/factory_floor.gd")
const Crate := preload("res://scripts/crate.gd")
const BuildPanel := preload("res://scripts/build_panel.gd")

const BACKEND_URL := "ws://127.0.0.1:8765/ws"
const LOG_HINT := "Log: click a crate to see its output"
const SCATTERED := ["intake", "dock", "scrap"]
const TOP_BAR := 44.0
const BOTTOM_PANEL := 180.0
const BUILD_PANEL := 360.0

var client: BackendClient
var factory_floor: FactoryFloor
var crates_layer: Node2D
var machines_layer: Node2D
var build_panel: BuildPanel
var build_button: Button
var bottom_panel: PanelContainer
var building := false
var pending_save := false
var last_layout := {}
var crates := {}  # item_id -> Crate
var logs := {}  # item_id -> Array of log lines
var pr_urls := {}  # item_id -> pull request URL
var selected_id := -1
var last_seq := 0

var camera: Camera2D
var spec_input: LineEdit
var status_label: Label
var log_title: Label
var log_view: RichTextLabel
var note_input: LineEdit
var approve_button: Button
var reject_button: Button
var retry_button: Button
var pr_button: Button


func _ready() -> void:
	factory_floor = FactoryFloor.new()
	add_child(factory_floor)
	crates_layer = Node2D.new()
	add_child(crates_layer)
	# Machines draw above the crates, so a crate inside a machine is hidden.
	machines_layer = Node2D.new()
	add_child(machines_layer)
	factory_floor.attach_machine_layer(machines_layer)

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

	build_button = Button.new()
	build_button.text = "Build line"
	build_button.pressed.connect(_toggle_build)
	row.add_child(build_button)

	status_label = Label.new()
	status_label.custom_minimum_size = Vector2(240, 0)
	status_label.clip_text = true
	row.add_child(status_label)

	bottom_panel = PanelContainer.new()
	ui.add_child(bottom_panel)
	bottom_panel.set_anchors_and_offsets_preset(Control.PRESET_BOTTOM_WIDE)
	bottom_panel.offset_top = -BOTTOM_PANEL
	var col := VBoxContainer.new()
	bottom_panel.add_child(col)

	var header := HBoxContainer.new()
	col.add_child(header)
	log_title = Label.new()
	log_title.text = LOG_HINT
	log_title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	log_title.clip_text = true
	header.add_child(log_title)

	note_input = LineEdit.new()
	note_input.placeholder_text = "Notes for the Coder (optional)"
	note_input.custom_minimum_size = Vector2(260, 0)
	header.add_child(note_input)
	approve_button = _action_button(header, "Approve", func() -> void: _send_action("approve"))
	reject_button = _action_button(header, "Reject", func() -> void: _send_action("reject"))
	retry_button = _action_button(header, "Retry", func() -> void: _send_action("retry"))
	pr_button = _action_button(header, "Open PR", _open_pr)
	_refresh_actions()

	log_view = RichTextLabel.new()
	log_view.scroll_following = true
	log_view.selection_enabled = true
	log_view.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(log_view)

	build_panel = BuildPanel.new()
	ui.add_child(build_panel)
	build_panel.set_anchors_and_offsets_preset(Control.PRESET_RIGHT_WIDE)
	build_panel.offset_left = -BUILD_PANEL
	build_panel.offset_top = TOP_BAR
	build_panel.visible = false
	build_panel.draft_changed.connect(_on_draft_changed)
	build_panel.save_requested.connect(_on_save_requested)
	build_panel.cancelled.connect(func() -> void: _close_build())


func _action_button(parent: Control, label: String, on_press: Callable) -> Button:
	var button := Button.new()
	button.text = label
	button.pressed.connect(on_press)
	parent.add_child(button)
	return button


## Show only the buttons that make sense for the selected crate.
func _refresh_actions() -> void:
	var state := ""
	if crates.has(selected_id):
		var crate: Crate = crates[selected_id]
		state = crate.state
	approve_button.visible = state == "awaiting_approval"
	reject_button.visible = state == "awaiting_approval"
	retry_button.visible = state == "scrapped"
	note_input.visible = state == "awaiting_approval" or state == "scrapped"
	pr_button.visible = str(pr_urls.get(selected_id, "")) != ""


func _send_action(kind: String) -> void:
	if not crates.has(selected_id):
		return
	var msg := {"type": kind, "item_id": selected_id, "note": note_input.text.strip_edges()}
	if client.send(msg):
		note_input.clear()
		_set_status("Sent %s for crate #%d" % [kind, selected_id])
	else:
		_set_status("Not connected. Is the backend running?")


func _open_pr() -> void:
	var url := str(pr_urls.get(selected_id, ""))
	if not url.is_empty():
		OS.shell_open(url)


## Tell each machine how many crates are waiting at it for you.
func _refresh_waiting() -> void:
	var counts := {}
	for id in crates:
		var crate: Crate = crates[id]
		if crate.state == "awaiting_approval":
			counts[crate.location] = int(counts.get(crate.location, 0)) + 1
	factory_floor.set_waiting(counts)


## Zoom and centre the camera so the whole line fits between the top bar, the log panel
## and (in build mode) the build panel on the right.
func _fit_camera() -> void:
	var view := get_viewport_rect().size
	var right := BUILD_PANEL if building else 0.0
	var bottom := 0.0 if building else BOTTOM_PANEL
	var content := factory_floor.world_size()
	# World rows 1.9 to 8.1 hold the labels, belts, machines and the scrap bin.
	var top_row := 1.9
	var bottom_row := 8.1
	var height := (bottom_row - top_row) * FactoryFloor.CELL
	var zoom := minf(1.0, minf((view.x - right - 32.0) / content.x,
			(view.y - TOP_BAR - bottom - 16.0) / height))
	zoom = maxf(zoom, 0.2)
	camera.zoom = Vector2(zoom, zoom)
	var world_center_y := (top_row + bottom_row) / 2.0 * FactoryFloor.CELL
	var screen_dx := (view.x - right) / 2.0 - view.x / 2.0
	var screen_dy := (TOP_BAR + (view.y - TOP_BAR - bottom) / 2.0) - view.y / 2.0
	camera.position = Vector2(content.x / 2.0 - screen_dx / zoom, world_center_y - screen_dy / zoom)


# Build mode ------------------------------------------------------------------

func _toggle_build() -> void:
	if building:
		_close_build()
		return
	if last_layout.is_empty():
		_set_status("Not connected. Is the backend running?")
		return
	building = true
	pending_save = false
	build_button.text = "Close build"
	build_panel.visible = true
	bottom_panel.visible = false
	crates_layer.visible = false
	build_panel.open(last_layout.get("catalog", []), last_layout.get("machines", []))
	_fit_camera()


func _close_build() -> void:
	building = false
	pending_save = false
	build_button.text = "Build line"
	build_panel.visible = false
	bottom_panel.visible = true
	crates_layer.visible = true
	factory_floor.set_highlight("")
	factory_floor.set_layout(last_layout.get("machines", []))
	_fit_camera()


func _on_draft_changed(preview: Array) -> void:
	factory_floor.set_layout(preview)
	factory_floor.set_highlight(build_panel.selected_id())
	_fit_camera()


func _on_save_requested(machines: Array) -> void:
	if client.send({"type": "save_line", "machines": machines}):
		pending_save = true
		build_panel.show_message("Saving...")
	else:
		build_panel.show_message("Not connected. Is the backend running?")


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
	if building:
		var machine_id := factory_floor.machine_at(p)
		if not machine_id.is_empty():
			build_panel.select_machine(machine_id)
			get_viewport().set_input_as_handled()
		return
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
		var text := str(msg.get("message", ""))
		_set_status("Backend: %s" % text)
		if building and pending_save:
			pending_save = false
			build_panel.show_message("Not saved: %s" % text)
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
			pr_urls[int(msg["item_id"])] = str(msg.get("url", ""))
			_set_status("PR opened for crate #%d" % int(msg["item_id"]))
			_refresh_actions()


func _apply_snapshot(snap: Dictionary) -> void:
	for crate in crates.values():
		crate.queue_free()
	crates.clear()
	logs.clear()
	pr_urls.clear()
	last_seq = int(snap.get("seq", 0))

	var layout: Dictionary = snap.get("layout", {})
	last_layout = layout
	if building and pending_save:
		_close_build()  # the backend accepted the line and sent it back
		_set_status("Line saved")
	elif building:
		pass  # keep showing the draft
	else:
		factory_floor.set_layout(layout.get("machines", []))
		_fit_camera()

	for item in snap.get("items", []):
		_add_item(item)
		if str(item.get("state", "")) == "running":
			factory_floor.set_machine_state(str(item.get("location", "")), "running", 0.0)
	_select(selected_id if crates.has(selected_id) else -1)
	_refresh_waiting()


func _add_item(item: Dictionary) -> void:
	var id := int(item["id"])
	var crate := Crate.new()
	crate.item_id = id
	crate.text = str(item.get("text", ""))
	crate.source = _source_label(item)
	crate.set_state(str(item.get("state", "queued")))
	crate.location = str(item.get("location", "intake"))
	crate.position = _target_for(id, crate.location, crate.state)
	if str(item.get("pr_url", "")) != "":
		pr_urls[id] = str(item["pr_url"])
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
	crate.location = str(msg.get("to", ""))
	crate.move_to(_target_for(id, crate.location, crate.state), float(msg.get("duration", 0.0)))
	_refresh_waiting()
	if id == selected_id:
		_refresh_actions()


func _target_for(id: int, location: String, state := "") -> Vector2:
	var base := factory_floor.point(location)
	if state == "awaiting_approval":
		# Crates waiting for you queue up in rows under the gate machine (below its step text).
		return base + Vector2((float(id % 4) - 1.5) * 34.0, 104.0 + float((id / 4) % 2) * 34.0)
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
	_refresh_actions()
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
