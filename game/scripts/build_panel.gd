extends PanelContainer
## Build mode: lay out your own pipelines on the floor.
##
## Pick a machine on the right and click the floor to place it. Drag machines (and the
## intake, dock and scrap bin) to move them. Drag from a machine's green port to the next
## machine or the dock to lay a belt. Failed crates leave by the red port: run its belt back
## to an earlier machine for rework, or into the scrap bin. A machine only scraps crates when
## its red belt runs to the bin, and only for the reasons ticked in its settings; anything
## else it can't send on waits at the machine for you. Click a machine for its settings, or
## a belt to remove it.
##
## Changes are only a draft until you press Save; the backend checks every pipeline and
## refuses it with a reason if, say, the Tester comes before the Coder.

signal save_requested(pipelines: Array)
signal cancelled
signal settings_opened(world: Vector2)  # the settings window opened for something at this point
signal check_repo_requested(pipeline: String, repo: Dictionary)  # the intake's Check button

const FactoryFloor := preload("res://scripts/factory_floor.gd")
# Choices for a reviewer's auto-approve setting, and what the backend calls each one.
const AUTO_APPROVE_WORDS := ["Never: always ask me", "Low", "Low or medium"]
const AUTO_APPROVE_LEVELS := ["", "low", "medium"]
const SCRAP_KINDS := ["needs_info", "already_done", "blocked", "work_failed", "rejected"]
const SCRAP_WORDS := {"needs_info": "Needs info", "already_done": "Already done", "blocked": "Blocked",
		"work_failed": "Work failed", "rejected": "Rejected by you"}
const SCRAP_HINTS := {
	"needs_info": "Claude changed nothing: the spec is unclear or missing something",
	"already_done": "Claude changed nothing: the code already does it",
	"blocked": "Claude changed nothing: something outside the repo stops it",
	"work_failed": "Tests, CI, a review or a step kept failing (or rework ran out)",
	"rejected": "You rejected it at this gate with nowhere to send it back",
}
const SOURCE_NAMES := {
	"game": "Specs typed in the game",
	"github": "GitHub issues",
	"jira": "Jira issues",
	"pr_review": "PRs waiting for my review",
	"prd": "PRDs (GitHub issues labelled prd)",
}
# Stations you can place from the list: "@intake" starts a new line there (just its intake);
# "@dock" and "@scrap" place the factory's one shipping dock and scrap bin, which every line's
# belts can run into.
const STATION_TILES := {
	"@intake": {"name": "Intake (new line)", "sprite": "intake",
			"hint": "Starts a new line here. Pick where its crates come from in the intake's settings, then run belts to the shipping dock (or join another line's machines)."},
	"@dock": {"name": "Shipping dock", "sprite": "shipping_dock",
			"hint": "Where finished crates end up. The factory has one: run every line's last belt into it."},
	"@scrap": {"name": "Scrap bin", "sprite": "scrap_bin",
			"hint": "Where failed crates go, from machines whose red belt runs into it. The factory has one."},
}
const STATION_HINTS := {
	"dock": "The shipping dock: crates that make it through every machine end here.",
	"scrap": "The scrap bin: crates land here from machines whose red belt runs into it, for the reasons ticked on each machine.",
}
# A sorter's outputs, and the rules you can give each one (graph.RULE_KINDS in the backend).
const SORTER_OUTPUTS := ["out_1", "out_2", "out_3", "out_4", "out_5", "out_6"]
const SORTER_SIDES := {"out_1": "north, left", "out_2": "north, right", "out_3": "east, top",
		"out_4": "east, bottom", "out_5": "south, left", "out_6": "south, right"}
const RULE_KINDS := ["", "risk", "source", "text", "files", "size", "otherwise"]
const RULE_WORDS := {"": "No rule (not used)", "risk": "Risk is", "source": "Came from",
		"text": "Spec mentions", "files": "Changes files", "size": "Changes at least (lines)",
		"otherwise": "Everything else"}
const RISK_CHOICES := ["low", "medium", "high", "medium+"]
const RISK_WORDS := {"low": "Low", "medium": "Medium", "high": "High", "medium+": "Medium or high"}
const SETTINGS_WIDTH := 360.0  # the left dock
const PORT_GRAB := 14.0
const DRAG_THRESHOLD := 8.0  # screen pixels the mouse must move before a press becomes a drag  # how close (in world pixels) a press must be to grab a port

var floor_view: FactoryFloor
var catalog := {}  # type -> summary from the backend
var catalog_order: Array = []
var sources: Array = []
var default_repo := {}  # the backend's own test project: {github, path, base_branch}
# Sources that come from the line's own repo: lines on different repos can each take them.
const REPO_SOURCES := ["github", "prd"]
var draft: Array = []  # pipelines being edited
var current := ""  # pipeline id being edited

var _sel_key := ""  # footprint key of the selected machine or station
var _sel_belt := {}  # {pid, from, port} of the selected belt
var _placing := ""  # machine type being placed
var _drag := {}
var _loading := false

var _pipeline_picker: OptionButton
var _pipeline_name: LineEdit
var _intake_box: VBoxContainer
var _source_boxes: VBoxContainer
var _source_note: Label
var _repo_github: LineEdit
var _repo_path: LineEdit
var _repo_base: LineEdit
var _repo_result: Label
var settings_window: PanelContainer
var _settings_col: VBoxContainer
var _palette: GridContainer
var _palette_hint: Label
var _machine_box: VBoxContainer
var _title: Label
var _description: Label
var _name_input: LineEdit
var _gate_box: CheckBox
var _timeout_input: SpinBox
var _timeout_label: Label
var _steps_label: Label
var _fail_label: Label
var _auto_box: VBoxContainer
var _auto_picker: OptionButton
var _auto_hint: Label
var _fail_picker: OptionButton
var _scrap_box: VBoxContainer
var _options_box: VBoxContainer  # the machine type's own tick boxes (machines.toml options)
var _rules_box: VBoxContainer  # a sorter's rule for each output
var _scrap_checks := {}  # kind -> CheckBox
var _fail_targets: Array = []
var _prompt_label: Label
var _prompt_input: TextEdit
var _belt_box: VBoxContainer
var _belt_label: Label
var _station_label: Label
var _message: Label


func _ready() -> void:
	custom_minimum_size = Vector2(280, 0)
	theme_type_variation = &"PanelDockRight"
	var outer := VBoxContainer.new()
	add_child(outer)
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	outer.add_child(scroll)
	var col := VBoxContainer.new()
	col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(col)

	var title := Label.new()
	title.text = "Build"
	title.theme_type_variation = &"LabelHeading"
	col.add_child(title)
	_hint(col, "Click anything on the floor to open its settings. Drag to move it; Shift+drag moves its whole line. Drag from a green port to the next machine to lay a belt. Failed crates leave by the red port: run it back to an earlier machine for rework, or into the scrap bin. With no red belt, failed crates wait at the machine for you. Hold Space and drag, or drag empty floor, to pan.")

	_field_label(col, "Lines")
	_pipeline_picker = OptionButton.new()
	_pipeline_picker.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_pipeline_picker.item_selected.connect(_on_pipeline_picked)
	col.add_child(_pipeline_picker)
	var line_row := HBoxContainer.new()
	col.add_child(line_row)
	_button(line_row, "New", _new_pipeline).theme_type_variation = &"ButtonGhost"
	_button(line_row, "Delete", _delete_pipeline).theme_type_variation = &"ButtonGhost"
	_button(line_row, "Clear all", _clear_factory).theme_type_variation = &"ButtonGhost"

	col.add_child(HSeparator.new())
	_field_label(col, "Add a machine")
	_palette = GridContainer.new()
	_palette.columns = 2
	col.add_child(_palette)
	_palette_hint = _hint(col, "")

	_message = Label.new()
	_message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_message.add_theme_color_override("font_color", Color("F2B84C"))
	outer.add_child(_message)
	var actions := HBoxContainer.new()
	actions.alignment = BoxContainer.ALIGNMENT_END
	outer.add_child(actions)
	_button(actions, "Cancel", func() -> void: cancelled.emit()).theme_type_variation = &"ButtonGhost"
	_button(actions, "Save", _save).theme_type_variation = &"ButtonPrimary"

	_build_settings_window()


## The settings window: a separate panel on the left for whatever is selected on the floor.
func _build_settings_window() -> void:
	settings_window = PanelContainer.new()
	settings_window.visible = false
	settings_window.theme_type_variation = &"PanelDock"
	var outer := VBoxContainer.new()
	settings_window.add_child(outer)
	var header := HBoxContainer.new()
	outer.add_child(header)
	_title = Label.new()
	_title.theme_type_variation = &"LabelHeading"
	_title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_title.clip_text = true
	header.add_child(_title)
	var esc := Label.new()
	esc.text = "Esc"
	esc.theme_type_variation = &"LabelKey"
	esc.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	header.add_child(esc)
	var close_button := _button(header, "", func() -> void: _select(""))
	close_button.theme_type_variation = &"ButtonGhost"
	close_button.icon = load("res://assets/ui/theme/icons/close.svg")
	close_button.tooltip_text = "Close (Esc)"
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	outer.add_child(scroll)
	var col := VBoxContainer.new()
	col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(col)
	_settings_col = col

	_machine_box = VBoxContainer.new()
	col.add_child(_machine_box)
	_description = _hint(_machine_box, "")
	_field_label(_machine_box, "Name")
	_name_input = LineEdit.new()
	_name_input.max_length = 40
	_name_input.text_changed.connect(_on_name_changed)
	_machine_box.add_child(_name_input)
	_gate_box = CheckBox.new()
	_gate_box.text = "Wait for my approval after this machine"
	_gate_box.toggled.connect(_on_gate_toggled)
	_machine_box.add_child(_gate_box)
	# Reviewers: let a good review of a low-risk change through without asking.
	_auto_box = VBoxContainer.new()
	_machine_box.add_child(_auto_box)
	_field_label(_auto_box, "Approve by itself when the review looks good and the risk is")
	_auto_picker = OptionButton.new()
	for i in AUTO_APPROVE_WORDS.size():
		var icon_path := "res://assets/ui/%s.svg" % ("icon_human_approval" if i == 0 else "icon_auto_approve")
		if ResourceLoader.exists(icon_path):
			_auto_picker.add_icon_item(load(icon_path), AUTO_APPROVE_WORDS[i])
		else:
			_auto_picker.add_item(AUTO_APPROVE_WORDS[i])
	_auto_picker.add_theme_constant_override("icon_max_width", 20)
	_auto_picker.item_selected.connect(_on_auto_selected)
	_auto_box.add_child(_auto_picker)
	_auto_hint = _hint(_auto_box, "")
	_options_box = VBoxContainer.new()
	_machine_box.add_child(_options_box)
	_rules_box = VBoxContainer.new()
	_machine_box.add_child(_rules_box)
	_fail_label = _field_label(_machine_box, "If it fails, send the crate to")
	_fail_picker = OptionButton.new()
	_fail_picker.item_selected.connect(_on_fail_selected)
	_machine_box.add_child(_fail_picker)
	# Which reasons this machine scraps for (with its red belt in the bin); the rest wait here.
	_scrap_box = VBoxContainer.new()
	_machine_box.add_child(_scrap_box)
	_field_label(_scrap_box, "Scrap it when the reason is")
	for kind in SCRAP_KINDS:
		var box := CheckBox.new()
		box.text = str(SCRAP_WORDS[kind])
		box.tooltip_text = str(SCRAP_HINTS[kind])
		box.toggled.connect(func(_on: bool) -> void: _on_scrap_kinds_changed())
		_scrap_box.add_child(box)
		_scrap_checks[kind] = box
	_hint(_scrap_box, "Unticked reasons wait at the machine: run them again or scrap them yourself.")
	_timeout_label = _field_label(_machine_box, "Time limit for each step, in minutes (0 keeps the default)")
	_timeout_input = SpinBox.new()
	_timeout_input.min_value = 0
	_timeout_input.max_value = 600
	_timeout_input.step = 1
	_timeout_input.value_changed.connect(_on_timeout_changed)
	_machine_box.add_child(_timeout_input)
	_steps_label = _hint(_machine_box, "")
	_prompt_label = _field_label(_machine_box, "Prompt for Claude")
	_prompt_input = TextEdit.new()
	_prompt_input.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_prompt_input.custom_minimum_size = Vector2(0, 220)
	_prompt_input.text_changed.connect(_on_prompt_changed)
	_machine_box.add_child(_prompt_input)
	_button(_machine_box, "Remove machine", _remove_selected).theme_type_variation = &"ButtonReject"

	_intake_box = VBoxContainer.new()
	col.add_child(_intake_box)
	_hint(_intake_box, "New crates start here. Drag from the green port to the first machine. Shift+drag any part of the line to move all of it.")
	_field_label(_intake_box, "Line name")
	_pipeline_name = LineEdit.new()
	_pipeline_name.max_length = 40
	_pipeline_name.text_changed.connect(_on_pipeline_name)
	_intake_box.add_child(_pipeline_name)
	_field_label(_intake_box, "Repo this line works on")
	_hint(_intake_box, "Branches, pull requests and GitHub issues come from here. Leave it blank for the factory's test project.")
	_repo_github = _repo_field("GitHub repo (owner/name or a link)", "github")
	_repo_path = _repo_field("Local folder (blank: the factory clones it for you)", "path")
	_repo_base = _repo_field("Base branch (blank: the repo's default branch)", "base_branch")
	var check_row := HBoxContainer.new()
	_intake_box.add_child(check_row)
	var check := _button(check_row, "Check repo", _on_check_repo)
	check.theme_type_variation = &"ButtonGhost"
	_repo_result = _hint(_intake_box, "")
	_field_label(_intake_box, "Take crates from")
	_source_boxes = VBoxContainer.new()
	_intake_box.add_child(_source_boxes)
	_source_note = _hint(_intake_box, "")

	_belt_box = VBoxContainer.new()
	col.add_child(_belt_box)
	_belt_label = Label.new()
	_belt_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_belt_box.add_child(_belt_label)
	_button(_belt_box, "Remove belt", _remove_selected).theme_type_variation = &"ButtonReject"

	_station_label = _hint(col, "")
	add_sibling.call_deferred(settings_window)
	_place_settings_window.call_deferred()


func _place_settings_window() -> void:
	settings_window.set_anchors_and_offsets_preset(Control.PRESET_TOP_LEFT)
	settings_window.position = Vector2(0, offset_top)
	_fit_settings_window()


## As tall as what it shows, up to the bottom of the screen (then it scrolls).
func _fit_settings_window() -> void:
	if settings_window == null or not settings_window.visible:
		return
	# The left dock: full height under the top bar.
	settings_window.size = Vector2(SETTINGS_WIDTH, get_viewport_rect().size.y - settings_window.position.y)


func _repo_field(label: String, key: String) -> LineEdit:
	_field_label(_intake_box, label)
	var edit := LineEdit.new()
	edit.text_changed.connect(func(text: String) -> void: _on_repo_changed(key, text))
	_intake_box.add_child(edit)
	return edit


func _button(parent: Control, label: String, on_press: Callable) -> Button:
	var b := Button.new()
	b.text = label
	b.pressed.connect(on_press)
	parent.add_child(b)
	return b


func _hint(parent: Control, text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	l.theme_type_variation = &"LabelMuted"
	parent.add_child(l)
	return l


func _field_label(parent: Control, text: String) -> Label:
	var l := Label.new()
	l.text = text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	parent.add_child(l)
	return l


## catalog_list: machine types. pipelines: as the backend sent them. floor: where to draw.
func open(catalog_list: Array, pipelines: Array, source_list: Array, floor: FactoryFloor,
		default_repo_info := {}) -> void:
	floor_view = floor
	default_repo = default_repo_info
	catalog.clear()
	catalog_order.clear()
	for child in _palette.get_children():
		child.queue_free()
	# Stations first: a new line (from its intake) and extra scrap bins.
	for kind in STATION_TILES:
		var tile: Dictionary = STATION_TILES[kind]
		var sb := Button.new()
		sb.theme_type_variation = &"ButtonTile"
		sb.alignment = HORIZONTAL_ALIGNMENT_LEFT
		sb.text = str(tile["name"])
		sb.tooltip_text = str(tile["hint"])
		sb.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		sb.clip_text = true
		var station_tex := "res://assets/machines/%s.svg" % str(tile["sprite"])
		if ResourceLoader.exists(station_tex):
			sb.icon = load(station_tex)
		sb.pressed.connect(func() -> void: _start_placing(kind))
		_palette.add_child(sb)
	for t in catalog_list:
		var kind := str(t.get("type", t.get("id", "")))
		catalog[kind] = t
		catalog_order.append(kind)
		var b := Button.new()
		b.theme_type_variation = &"ButtonTile"
		b.alignment = HORIZONTAL_ALIGNMENT_LEFT
		b.text = str(t.get("name", kind))
		b.tooltip_text = str(t.get("description", ""))
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		b.clip_text = true
		var sprite := str(t.get("sprite", ""))
		var tex_path := "res://assets/machines/%s.svg" % sprite
		if not ResourceLoader.exists(tex_path) and FactoryFloor.FALLBACK_SPRITES.has(sprite):
			tex_path = "res://assets/machines/%s.svg" % str(FactoryFloor.FALLBACK_SPRITES[sprite])  # art not there yet
		if ResourceLoader.exists(tex_path):
			b.icon = load(tex_path)
		b.pressed.connect(func() -> void: _start_placing(kind))
		_palette.add_child(b)
	sources = source_list if not source_list.is_empty() else SOURCE_NAMES.keys()
	draft = pipelines.duplicate(true)
	current = str(draft[0].get("id", "")) if not draft.is_empty() else ""
	_placing = ""
	_drag = {}
	_sel_key = ""
	_sel_belt = {}
	show_message("")
	floor_view.editing = true
	_rebuild()


func close() -> void:
	settings_window.visible = false
	if floor_view:
		floor_view.editing = false
		floor_view.set_ghost({})
		floor_view.set_wire({})
		floor_view.set_highlight("")


func show_message(text: String) -> void:
	_message.text = text


# Draft helpers ---------------------------------------------------------------

func _pipeline(pid: String) -> Dictionary:
	for p in draft:
		if str(p.get("id", "")) == pid:
			return p
	return {}


func _machine(id: String) -> Dictionary:
	for p in draft:
		for m in p.get("machines", []):
			if str(m.get("id", "")) == id:
				return m
	return {}


## The dictionary that holds this footprint's "pos": a machine or a station.
func _node(key: String) -> Dictionary:
	if ":" in key:
		var p := _pipeline(key.get_slice(":", 0))
		var place := key.get_slice(":", 1)
		for bin in p.get("bins", []):
			if str(bin.get("id", "")) == place:
				return bin
		return p.get(place, {})
	return _machine(key)


func _pid_of(key: String) -> String:
	return key.get_slice(":", 0) if ":" in key else str(floor_view.owner_of.get(key, ""))


## A belt end as the backend names it: a machine id, "intake" or "dock".
func _end_name(key: String) -> String:
	return key.get_slice(":", 1) if ":" in key else key


## Where a belt from line pid runs, as it's saved: a machine id, a station of its own line
## ("dock"), or another line's station by its full name ("build:dock").
func _belt_target(pid: String, key: String) -> String:
	if ":" in key and key.get_slice(":", 0) == pid:
		return key.get_slice(":", 1)
	return key


static func _is_dock(node: String) -> bool:
	return node == "dock" or node.ends_with(":dock")


func _label_of(node: String) -> String:
	if _is_dock(node):
		return "the shipping dock"
	match node:
		"intake":
			return "the intake"
	if FactoryFloor.is_bin(node):
		return "the scrap bin"
	var m := _machine(node)
	return str(m.get("name", node)) if not m.is_empty() else node


func _rebuild() -> void:
	floor_view.set_pipelines(draft)
	_refresh_ui()


func _refresh_ui() -> void:
	_loading = true
	_pipeline_picker.clear()
	for i in draft.size():
		var p: Dictionary = draft[i]
		_pipeline_picker.add_item(str(p.get("name", p.get("id", ""))))
		if str(p.get("id", "")) == current:
			_pipeline_picker.select(i)
	if _placing.is_empty():
		_palette_hint.text = "Click a machine, then click the floor where it goes."
	else:
		var placing_name: String = str(STATION_TILES[_placing]["name"]) if STATION_TILES.has(_placing) \
				else str(catalog[_placing].get("name", _placing))
		_palette_hint.text = "Click the floor to place the %s. Esc stops." % placing_name

	var machine := {} if ":" in _sel_key else _machine(_sel_key)
	var station := _sel_key.get_slice(":", 1) if ":" in _sel_key else ""
	settings_window.visible = (not _sel_key.is_empty() or not _sel_belt.is_empty()) \
			and not str(_drag.get("mode", "")) in ["move", "line"]  # out of the way while dragging
	_machine_box.visible = not machine.is_empty()
	_intake_box.visible = station == "intake"
	if station == "intake":
		var pipeline := _pipeline(current)
		_title.text = "%s intake" % str(pipeline.get("name", ""))
		_pipeline_name.text = str(pipeline.get("name", ""))
		_fill_sources(pipeline)
		_fill_repo(pipeline)
	elif not station.is_empty():
		_title.text = "Shipping dock" if station == "dock" else "Scrap bin"
		if station != "scrap" and FactoryFloor.is_bin(station):
			station = "scrap"  # same hint; this one can be deleted
			_station_label.text = ""
	if not machine.is_empty():
		var base: Dictionary = catalog.get(str(machine.get("type", "")), {})
		_title.text = str(machine.get("name", base.get("name", "")))
		_description.text = str(base.get("description", ""))
		_name_input.text = str(machine.get("name", ""))
		_gate_box.button_pressed = bool(machine.get("gate", false))
		var auto := str(machine.get("auto_approve", ""))
		_auto_box.visible = bool(base.get("can_auto_approve", false)) and bool(machine.get("gate", false))
		_auto_picker.select(maxi(0, AUTO_APPROVE_LEVELS.find(auto)))
		_auto_hint.text = ("Needs a Risk analyser earlier on the line. Anything rated higher, or not "
				+ "rated, still waits for you.") if auto != "" else "Every crate waits for you here."
		var timeout = machine.get("timeout_seconds", null)
		_timeout_input.set_value_no_signal(roundf(float(timeout) / 60.0) if timeout else 0.0)
		var steps: Array = base.get("steps", [])
		var names := PackedStringArray()
		for s in steps:
			names.append(str(s))
		_steps_label.text = ("Steps: " + ", ".join(names)) if not names.is_empty() else ""
		var has_prompt := bool(base.get("has_prompt", false))
		_prompt_label.visible = has_prompt
		_prompt_input.visible = has_prompt
		if has_prompt and _prompt_input.text != str(machine.get("prompt", "")):
			_prompt_input.text = str(machine.get("prompt", ""))
		_fill_fail_picker(_sel_key)
		_fill_options(machine, base)
		var sorter := bool(base.get("sorter", false))
		_rules_box.visible = sorter
		for part in [_gate_box, _fail_label, _fail_picker, _timeout_input, _timeout_label]:
			part.visible = not sorter
		if sorter:
			_scrap_box.visible = false
			_fill_rules(machine)
	_belt_box.visible = not _sel_belt.is_empty()
	if not _sel_belt.is_empty():
		var b := _find_belt(_sel_belt)
		var kind := "Rework belt" if _sel_belt["port"] == "fail" else "Belt"
		_title.text = kind
		_belt_label.text = "From %s to %s" % [_label_of(_sel_belt["from"]), _label_of(str(b.get("to", "")))]
	_station_label.visible = STATION_HINTS.has(station)
	_station_label.text = str(STATION_HINTS.get(station, ""))
	if FactoryFloor.is_bin(_sel_key) and not _sel_key.ends_with(":scrap"):
		_station_label.text += " Press Delete to remove this one; red belts into it are taken off too."
	_loading = false
	var route := -1
	if not _sel_belt.is_empty():
		for i in floor_view.routes.size():
			var r: Dictionary = floor_view.routes[i]
			if r["pid"] == _sel_belt["pid"] and r["from"] == _sel_belt["from"] and r["port"] == _sel_belt["port"]:
				route = i
	floor_view.set_highlight(_sel_key, route)
	_fit_settings_window.call_deferred()


func _find_belt(ref: Dictionary) -> Dictionary:
	for b in _pipeline(str(ref.get("pid", ""))).get("belts", []):
		if str(b.get("from", "")) == ref["from"] and str(b.get("port", "")) == ref["port"]:
			return b
	return {}


func _select(key: String, belt := {}) -> void:
	var opening := key != _sel_key or belt != _sel_belt
	_sel_key = key
	_sel_belt = belt
	if opening and floor_view.footprints.has(key):
		settings_opened.emit(Vector2(floor_view.footprints[key]) * FactoryFloor.CELL)
	if not key.is_empty():
		current = _pid_of(key)
	elif not belt.is_empty():
		current = belt["pid"]
	_refresh_ui()


## The selected machine's id, or "".
func selected_id() -> String:
	return _sel_key


# Pipelines -------------------------------------------------------------------

func _on_pipeline_picked(index: int) -> void:
	if _loading or index < 0 or index >= draft.size():
		return
	current = str(draft[index].get("id", ""))
	_select("")


func _on_pipeline_name(text: String) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if p.is_empty():
		return
	p["name"] = text
	_title.text = "%s intake" % text
	if _pipeline_picker.selected >= 0:
		_pipeline_picker.set_item_text(_pipeline_picker.selected, text)
	floor_view.set_pipelines(draft)


## One tick box per source. A source another line takes is shown with that line's name.
func _fill_sources(pipeline: Dictionary) -> void:
	for child in _source_boxes.get_children():
		child.queue_free()
	var taken := {}
	for p in draft:
		if p != pipeline:
			for src in _sources_of(p):
				taken[_source_key(src, p)] = str(p.get("name", p.get("id", "")))
	var mine := _sources_of(pipeline)
	for src in sources:
		var box := CheckBox.new()
		box.text = str(SOURCE_NAMES.get(src, src))
		box.button_pressed = src in mine
		var key := _source_key(src, pipeline)
		if taken.has(key):
			box.text += ("  (%s takes these from this repo)" if src in REPO_SOURCES else "  (%s takes these)") \
					% taken[key]
			box.disabled = true
		box.toggled.connect(func(on: bool) -> void: _on_source_toggled(src, on))
		_source_boxes.add_child(box)
	_source_note.text = ("GitHub and Jira issues go to the line that takes typed specs when no line ticks them."
			if not ("github" in mine and "jira" in mine) else "")


## A source as one line at a time can take it: GitHub issues and PRDs once per repo.
func _source_key(src: String, p: Dictionary) -> String:
	return "%s@%s" % [src, repo_key(p)] if src in REPO_SOURCES else src


## Which repo a line is on, as the backend tells them apart ("" is the test project).
func repo_key(p: Dictionary) -> String:
	var repo: Dictionary = p.get("repo", {}) if p.get("repo") is Dictionary else {}
	var github := github_name(str(repo.get("github", "")))
	var path := str(repo.get("path", "")).strip_edges()
	if path.is_empty() and (github.is_empty() or github == github_name(str(default_repo.get("github", "")))):
		return ""
	return github if not github.is_empty() else path.to_lower()


## "owner/name", lower case, from owner/name, a github.com link or a clone URL.
static func github_name(text: String) -> String:
	var t := text.strip_edges().to_lower()
	for prefix in ["https://", "http://", "git@", "ssh://git@", "www."]:
		t = t.trim_prefix(prefix)
	t = t.trim_prefix("github.com/").trim_prefix("github.com:")
	t = t.trim_suffix("/").trim_suffix(".git")
	var parts := t.split("/")
	return "%s/%s" % [parts[0], parts[1]] if parts.size() >= 2 else t


func _fill_repo(pipeline: Dictionary) -> void:
	var repo: Dictionary = pipeline.get("repo", {}) if pipeline.get("repo") is Dictionary else {}
	_repo_github.text = str(repo.get("github", ""))
	_repo_path.text = str(repo.get("path", ""))
	_repo_base.text = str(repo.get("base_branch", ""))
	var test_project := str(default_repo.get("github", ""))
	_repo_github.placeholder_text = ("%s (the test project)" % test_project) if not test_project.is_empty() \
			else "owner/name"
	_repo_path.placeholder_text = "e.g. C:\\code\\my-app"
	_repo_base.placeholder_text = "main"
	_repo_result.remove_theme_color_override("font_color")
	_repo_result.text = _repo_words(pipeline.get("repo_info", {}))


## What a line's saved repo is, in words: "Works on owner/name (main), cloned in <folder>."
func _repo_words(info) -> String:
	if not info is Dictionary or info.is_empty():
		return "Not saved yet: press Check repo to see if it's right."
	var what := str(info.get("github", "")) if not str(info.get("github", "")).is_empty() else "a local repo"
	var base := str(info.get("base_branch", ""))
	return "Works on %s%s, in %s.%s" % [what, (" (from %s)" % base) if not base.is_empty() else "",
			str(info.get("path", "")), "  This is the factory's test project." if info.get("default", false) else ""]


func _on_repo_changed(key: String, text: String) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if p.is_empty():
		return
	var repo: Dictionary = (p.get("repo", {}) if p.get("repo") is Dictionary else {}).duplicate()
	if text.strip_edges().is_empty():
		repo.erase(key)
	else:
		repo[key] = text.strip_edges()
	if repo.is_empty():
		p.erase("repo")
	else:
		p["repo"] = repo
	p.erase("repo_info")  # what the backend said about the old settings
	_repo_result.remove_theme_color_override("font_color")
	_repo_result.text = "Changed: press Check repo, then Save."
	_fill_sources(p)  # which issues this line can take depends on its repo


func _on_check_repo() -> void:
	var p := _pipeline(current)
	if p.is_empty():
		return
	_repo_result.remove_theme_color_override("font_color")
	_repo_result.text = "Checking..."
	check_repo_requested.emit(current, (p.get("repo", {}) if p.get("repo") is Dictionary else {}).duplicate())


## The backend's answer to Check repo.
func show_repo_check(msg: Dictionary) -> void:
	if str(msg.get("pipeline", "")) != current or _repo_result == null:
		return
	var ok: bool = msg.get("ok", false)
	_repo_result.text = ("OK. " if ok else "Problem: ") + str(msg.get("message", ""))
	_repo_result.add_theme_color_override("font_color", Color("3DD68C") if ok else Color("E5484D"))


func _sources_of(p: Dictionary) -> Array:
	if p.get("sources") is Array:
		return p["sources"]
	return [str(p.get("source", "game"))]


func _on_source_toggled(src: String, on: bool) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if p.is_empty():
		return
	var list: Array = _sources_of(p).duplicate()
	if on and not src in list:
		list.append(src)
	elif not on:
		list.erase(src)
	p["sources"] = list
	p.erase("source")


func _new_pipeline(at := Vector2i(-1, -1)) -> void:
	var ids := {}
	var used := {}
	for p in draft:
		ids[str(p.get("id", ""))] = true
		for src in _sources_of(p):
			used[src] = true
	var n := draft.size() + 1
	while ids.has("line-%d" % n):
		n += 1
	var free: Array = []
	for src in sources:
		if not used.has(src):
			free.append(src)
	var x := 1
	var y := floor_view.bounds.end.y + 2
	if at.x != -1 or at.y != -1:
		x = at.x
		y = at.y
	# Just the intake: the line ships into the factory's dock and scraps into its bin (placed
	# from Add a machine if there isn't one yet).
	draft.append({
		"id": "line-%d" % n, "name": "Line %d" % n, "sources": [],
		"intake": {"pos": [x, y]}, "machines": [], "belts": [],
	})
	current = "line-%d" % n
	_sel_key = ""
	_sel_belt = {}
	_rebuild()
	_select("line-%d:intake" % n)
	show_message("New line added. Pick where its crates come from in the intake settings, then add machines and run belts from the intake to the shipping dock (any line's).")


## Remove every belt, in any line, that runs into this machine or station (by its footprint key).
func _drop_belts_into(key: String) -> void:
	for q in draft:
		var qid := str(q.get("id", ""))
		q["belts"] = (q.get("belts", []) as Array).filter(func(b) -> bool:
			return floor_view.node_key(qid, str(b.get("to", ""))) != key)


func _delete_pipeline() -> void:
	if draft.is_empty():
		return
	var gone := _pipeline(current)
	for st in ["intake", "dock", "scrap"]:
		_drop_belts_into("%s:%s" % [current, st])
	for m in gone.get("machines", []):
		_drop_belts_into(str(m.get("id", "")))
	draft.erase(gone)
	current = str(draft[0].get("id", "")) if not draft.is_empty() else ""
	_sel_key = ""
	_sel_belt = {}
	_rebuild()
	if draft.is_empty():
		show_message("No lines left: press Save and the factory stops taking work until you add one.")


## Take every line off the floor (until Save, Cancel brings them back).
func _clear_factory() -> void:
	draft.clear()
	current = ""
	_sel_key = ""
	_sel_belt = {}
	_placing = ""
	_rebuild()
	show_message("Cleared. Press Save to empty the factory (it stops taking work), or Cancel to keep your lines.")


## After a save: carry on editing what the backend saved, keeping the line and thing selected.
func reload(pipelines: Array) -> void:
	draft = pipelines.duplicate(true)
	if _pipeline(current).is_empty():
		current = str(draft[0].get("id", "")) if not draft.is_empty() else ""
	if not _sel_key.is_empty() and not floor_view.footprints.has(_sel_key) and _node(_sel_key).is_empty():
		_sel_key = ""
	_sel_belt = {}
	_rebuild()
	var paused := PackedStringArray()
	for p in draft:
		if p.get("paused", false):
			paused.append(str(p.get("problem", "")))
	for r in floor_view.broken_belts():
		paused.append("There's no room for the belt from %s to %s: move the machines apart so it can get round." % [
				_label_of(r["from"]), _label_of(r["to"])])
	show_message("Saved." if paused.is_empty() else
			"Saved, but paused until fixed:\n" + "\n".join(paused))


# Placing, moving and wiring --------------------------------------------------

func _start_placing(kind: String) -> void:
	_placing = kind
	_drag = {}
	_refresh_ui()


func _new_id(kind: String) -> String:
	var id := kind
	var n := 2
	while not _machine(id).is_empty():
		id = "%s-%d" % [kind, n]
		n += 1
	return id


func _place(kind: String, tl: Vector2i) -> void:
	if kind == "@intake":
		_placing = ""
		_new_pipeline(tl)
		return
	if kind == "@dock" or kind == "@scrap":
		var station := "dock" if kind == "@dock" else "scrap"
		var what := "shipping dock" if kind == "@dock" else "scrap bin"
		_placing = ""
		for q in draft:
			if q.get(station) is Dictionary:
				show_message("The factory already has a %s (on %s). Run belts into it from any line." % [
						what, str(q.get("name", q.get("id", "")))])
				_refresh_ui()
				return
		var owner_line := _pipeline(current)
		if owner_line.is_empty():
			show_message("There's no line to put it on: place an Intake first, which starts a new line.")
			_refresh_ui()
			return
		owner_line[station] = {"pos": [tl.x, tl.y]}
		_sel_key = "%s:%s" % [current, station]
		_sel_belt = {}
		_rebuild()
		show_message("Placed the %s. Run belts into it from any line." % what)
		return
	if kind == "@extra_bin":
		var line := _pipeline(current)
		if line.is_empty():
			_placing = ""
			show_message("There's no line to put it on: place an Intake first, which starts a new line.")
			_refresh_ui()
			return
		var taken := FactoryFloor.bins_of(line)
		var n := 2
		while ("scrap_%d" % n) in taken:
			n += 1
		var bins: Array = line.get("bins", [])
		bins.append({"id": "scrap_%d" % n, "pos": [tl.x, tl.y]})
		line["bins"] = bins
		_placing = ""
		_sel_key = "%s:scrap_%d" % [current, n]
		_sel_belt = {}
		_rebuild()
		show_message("Now drag a red belt from a machine of %s into it, or pick it in the machine's settings." % str(line.get("name", current)))
		return
	var base: Dictionary = catalog.get(kind, {})
	var p := _pipeline(current)
	if p.is_empty():
		_placing = ""
		show_message("There's no line to put it on: place an Intake first, which starts a new line.")
		_refresh_ui()
		return
	var id := _new_id(kind)
	var list: Array = p.get("machines", [])
	list.append({
		"id": id, "type": kind, "name": str(base.get("name", kind)), "pos": [tl.x, tl.y],
		"gate": bool(base.get("gate", false)), "prompt": str(base.get("prompt", "")),
		"timeout_seconds": null, "sprite": str(base.get("sprite", "")), "steps": base.get("steps", []),
	})
	p["machines"] = list
	_placing = ""
	_sel_key = id
	_sel_belt = {}
	_rebuild()
	show_message("Now drag from a green port into the %s, and from its green port on to the next machine." % str(base.get("name", kind)))


func _connect(pid: String, from: String, port: String, to: String) -> void:
	var p := _pipeline(pid)
	var belts: Array = p.get("belts", [])
	for b in belts.duplicate():
		if str(b.get("from", "")) == from and str(b.get("port", "")) == port:
			belts.erase(b)
	belts.append({"from": from, "port": port, "to": to})
	p["belts"] = belts


func _remove_selected() -> void:
	if not _sel_belt.is_empty():
		var p := _pipeline(_sel_belt["pid"])
		(p["belts"] as Array).erase(_find_belt(_sel_belt))
		_sel_belt = {}
		_rebuild()
		return
	if _sel_key.is_empty():
		return
	if ":" in _sel_key and _end_name(_sel_key) != "intake":
		# The dock or a scrap bin: belts from every line that ran into it go with it.
		var line := _pipeline(_pid_of(_sel_key))
		var place := _end_name(_sel_key)
		if FactoryFloor.is_bin(place) and place != "scrap":
			line["bins"] = (line.get("bins", []) as Array).filter(func(b) -> bool: return str(b.get("id", "")) != place)
		else:
			line.erase(place)
		_drop_belts_into(_sel_key)
		_sel_key = ""
		_rebuild()
		show_message("Removed the scrap bin. Machines whose red belt ran into it now hold failed crates for you."
				if FactoryFloor.is_bin(place) else
				"Removed the shipping dock. Lines that shipped into it are paused until you place one and join them to it.")
		return
	if ":" in _sel_key:
		show_message("The intake starts its line: delete the whole line instead.")
		return
	var pid := _pid_of(_sel_key)
	var p := _pipeline(pid)
	var id := _sel_key
	# Keep the line joined: whatever fed this machine now feeds the machine after it.
	var feeder := {}
	var next := ""
	for b in p["belts"]:
		if str(b.get("to", "")) == id and str(b.get("port", "")) != "fail":
			feeder = b
		if str(b.get("from", "")) == id and str(b.get("port", "")) == "pass":
			next = str(b.get("to", ""))
	p["belts"] = (p["belts"] as Array).filter(func(b) -> bool:
		return str(b.get("from", "")) != id and str(b.get("to", "")) != id)
	if not feeder.is_empty() and not next.is_empty():
		_connect(pid, str(feeder["from"]), str(feeder["port"]), next)
	p["machines"] = (p["machines"] as Array).filter(func(m) -> bool: return str(m.get("id", "")) != id)
	_sel_key = ""
	_rebuild()


## Mouse and keys on the floor while building. world: the mouse in world coordinates.
## Returns true when the event was used.
func handle_floor_input(event: InputEvent, world: Vector2) -> bool:
	var key := event as InputEventKey
	if key and key.pressed and not key.echo:
		var focus := get_viewport().gui_get_focus_owner()
		if focus is LineEdit or focus is TextEdit:
			return false
		if key.keycode == KEY_ESCAPE:
			if not _placing.is_empty() or not _drag.is_empty():
				_placing = ""
				_drag = {}
				floor_view.set_ghost({})
				floor_view.set_wire({})
				_refresh_ui()
			else:
				_select("")
			return true
		if key.keycode == KEY_DELETE or key.keycode == KEY_BACKSPACE:
			_remove_selected()
			return true
		return false

	var motion := event as InputEventMouseMotion
	if motion:
		if not _placing.is_empty():
			var tl := floor_view.cell_at(world) - Vector2i(1, 1)
			floor_view.set_ghost({"pos": tl, "ok": floor_view.is_free(tl)})
			return true
		if str(_drag.get("mode", "")) == "press":
			if get_viewport().get_mouse_position().distance_to(_drag["screen"]) < DRAG_THRESHOLD:
				return true
			_drag["mode"] = "line" if _drag.get("whole_line", false) else "move"
			settings_window.visible = false  # out of the way while dragging
		match str(_drag.get("mode", "")):
			"move":
				var tl: Vector2i = floor_view.cell_at(world) - _drag["offset"]
				floor_view.set_ghost({"pos": tl, "ok": floor_view.is_free(tl, _drag["key"])})
				return true
			"line":
				var delta: Vector2i = floor_view.cell_at(world) - _drag["grab"]
				var box: Rect2i = _drag["box"]
				floor_view.set_ghost({"pos": box.position + delta, "size": box.size,
						"ok": floor_view.line_fits(_drag["pid"], delta)})
				return true
			"wire":
				floor_view.set_wire({"from": _drag["from"], "to": world, "kind": _drag["kind"]})
				return true
		return false

	var click := event as InputEventMouseButton
	if click == null or click.button_index != MOUSE_BUTTON_LEFT:
		return false
	if click.pressed:
		return _press(world)
	return _release(world)


## True where a left press would do nothing in build mode (so main can pan instead): not
## placing a machine, and no port, machine, station or belt under the mouse.
func is_empty_floor(world: Vector2) -> bool:
	if not _placing.is_empty():
		return false
	for k in floor_view.footprints:
		var ports: Dictionary = floor_view.output_ports(k)
		for port in ports:
			if world.distance_to(ports[port]) <= PORT_GRAB:
				return false
	if not floor_view.node_at(world).is_empty():
		return false
	return floor_view.route_at(floor_view.cell_at(world)) < 0


## A click (no drag) on empty floor: close the settings.
func click_empty_floor() -> void:
	_select("")


func _press(world: Vector2) -> bool:
	if not _placing.is_empty():
		var tl := floor_view.cell_at(world) - Vector2i(1, 1)
		if floor_view.is_free(tl):
			floor_view.set_ghost({})
			_place(_placing, tl)
		else:
			show_message("Something is already there. Pick an empty spot.")
		return true
	# A port: start laying a belt.
	for k in floor_view.footprints:
		var ports: Dictionary = floor_view.output_ports(k)
		for port in ports:
			if world.distance_to(ports[port]) <= PORT_GRAB:
				_select(k)
				_drag = {"mode": "wire", "key": k, "port": port, "from": ports[port],
						"kind": "fail" if port == "fail" else "pass"}
				return true
	var hit := floor_view.node_at(world)
	if not hit.is_empty():
		_select(hit)
		# Only a move once the mouse travels a little: a plain click just opens the settings.
		_drag = {"mode": "press", "key": hit, "offset": floor_view.cell_at(world) - floor_view.footprints[hit],
				"start": floor_view.footprints[hit], "screen": get_viewport().get_mouse_position()}
		if Input.is_key_pressed(KEY_SHIFT):
			_arm_line_move(_pid_of(hit), world)
		return true
	var route := floor_view.route_at(floor_view.cell_at(world))
	if route >= 0:
		var r: Dictionary = floor_view.routes[route]
		_select("", {"pid": r["pid"], "from": r["from"], "port": r["port"]})
		return true
	_select("")
	return true


func _release(world: Vector2) -> bool:
	var drag := _drag
	_drag = {}
	match str(drag.get("mode", "")):
		"press":
			return true  # a click: the settings are already open
		"move":
			floor_view.set_ghost({})
			var tl: Vector2i = floor_view.cell_at(world) - drag["offset"]
			if tl != drag["start"] and floor_view.is_free(tl, drag["key"]):
				_node(drag["key"])["pos"] = [tl.x, tl.y]
				_rebuild()
			else:
				_refresh_ui()
			return true
		"line":
			floor_view.set_ghost({})
			var delta: Vector2i = floor_view.cell_at(world) - drag["grab"]
			if delta != Vector2i.ZERO and floor_view.line_fits(drag["pid"], delta):
				_shift_line(drag["pid"], delta)
				_rebuild()
			elif delta != Vector2i.ZERO:
				show_message("It would land on another line. Pick an empty spot.")
				_refresh_ui()
			else:
				_refresh_ui()
			return true
		"wire":
			floor_view.set_wire({})
			_finish_wire(drag, world)
			return true
	return false


## Make the press that's starting a drag move every part of this line.
func _arm_line_move(pid: String, world: Vector2) -> void:
	var cells := floor_view.line_cells(pid)
	if cells.is_empty():
		return
	var box := Rect2i(cells.keys()[0], Vector2i.ONE)
	for c in cells:
		box = box.merge(Rect2i(c, Vector2i.ONE))
	_drag["whole_line"] = true
	_drag["pid"] = pid
	_drag["grab"] = floor_view.cell_at(world)
	_drag["box"] = box
	if _drag.get("mode", "") != "press":  # from the Move line button: start straight away
		_drag["mode"] = "line"


## Move every machine, station and belt of a line by delta cells.
func _shift_line(pid: String, delta: Vector2i) -> void:
	var p := _pipeline(pid)
	var places: Array = []
	for st in ["intake", "dock", "scrap"]:
		if p.get(st) is Dictionary:
			places.append(p[st])
	places.append_array(p.get("machines", []))
	places.append_array(p.get("bins", []))
	for place in places:
		var pos = place.get("pos")
		if pos is Array and pos.size() == 2:
			place["pos"] = [int(pos[0]) + delta.x, int(pos[1]) + delta.y]
	for b in p.get("belts", []):
		var path = b.get("path")
		if path is Array:
			var moved: Array = []
			for c in path:
				moved.append([int(c[0]) + delta.x, int(c[1]) + delta.y])
			b["path"] = moved


func _finish_wire(drag: Dictionary, world: Vector2) -> void:
	var target := floor_view.node_at(world)
	if target.is_empty():
		# Dropped just in front of a machine's input: count it.
		target = floor_view.node_at(world + Vector2(FactoryFloor.CELL * 0.5, 0))
	if target.is_empty() or target == drag["key"]:
		return
	var pid := _pid_of(drag["key"])
	var to := _belt_target(pid, target)
	if _end_name(target) == "intake":
		show_message("Belts go into machines, the dock or the scrap bin.")
		return
	if FactoryFloor.is_bin(to) and drag["port"] != "fail":
		show_message("Only a red belt (failed crates) can go into the scrap bin.")
		return
	if drag["port"] == "fail" and _is_dock(to):
		show_message("A red belt carries failed crates: run it back to an earlier machine or into the scrap bin.")
		return
	_connect(pid, _end_name(drag["key"]), drag["port"], to)
	_rebuild()
	show_message("" if _pid_of(target) == pid else
			"Joined to %s: crates from this line carry on along it." % str(_pipeline(_pid_of(target)).get("name", "")))


# Settings ---------------------------------------------------------------------

func _on_name_changed(text: String) -> void:
	if _loading:
		return
	_machine(_sel_key)["name"] = text
	_title.text = text
	floor_view.set_pipelines(draft)


func _on_gate_toggled(on: bool) -> void:
	if _loading:
		return
	_machine(_sel_key)["gate"] = on
	floor_view.set_pipelines(draft)
	_refresh_ui()


func _on_auto_selected(index: int) -> void:
	if _loading:
		return
	_machine(_sel_key)["auto_approve"] = AUTO_APPROVE_LEVELS[index]
	_refresh_ui()


func _on_timeout_changed(minutes: float) -> void:
	if _loading:
		return
	_machine(_sel_key)["timeout_seconds"] = minutes * 60.0 if minutes > 0 else null


func _on_prompt_changed() -> void:
	if _loading:
		return
	_machine(_sel_key)["prompt"] = _prompt_input.text


## Earlier machines on the line (by the pass belts) that a failure here can go back to.
func _fill_fail_picker(id: String) -> void:
	_fail_picker.clear()
	_fail_picker.add_item("Nowhere: it waits here for me")
	var chain: Array = floor_view.order.get(_pid_of(id), [])
	var at := chain.find(id)
	var pid := _pid_of(id)
	var bins: Array = []  # every scrap bin in the factory, as a belt from this line names it
	for q in draft:
		var qid := str(q.get("id", ""))
		for bin in FactoryFloor.bins_of(q):
			if bin == "scrap" and not q.get("scrap") is Dictionary:
				continue
			bins.append(bin if qid == pid else "%s:%s" % [qid, bin])
	_fail_targets = bins + chain.slice(0, maxi(at, 0))
	for target in _fail_targets:
		var t := str(target)
		if FactoryFloor.is_bin(t):
			var place := _end_name(t)
			_fail_picker.add_item("The scrap bin" if place == "scrap" else "Scrap bin %s" % place.trim_prefix("scrap_"))
		else:
			_fail_picker.add_item("Back to %s for rework" % _label_of(t))
	var current_target := floor_view.on_fail(id)
	var index := -1
	for i in _fail_targets.size():
		if floor_view.node_key(pid, str(_fail_targets[i])) == floor_view.node_key(pid, current_target):
			index = i
	_fail_picker.select(index + 1 if index >= 0 else 0)
	_fail_label.text = ("If it fails, the red belt takes the crate to" if at >= 0
			else "If it fails, the red belt takes the crate to (join it to the line first)")
	_scrap_box.visible = FactoryFloor.is_bin(current_target)
	var when = _machine(id).get("scrap_when", null)
	for kind in SCRAP_KINDS:
		(_scrap_checks[kind] as CheckBox).set_pressed_no_signal(when == null or kind in when)
	(_scrap_checks["rejected"] as CheckBox).visible = bool(_machine(id).get("gate", false))


## The type's tick boxes (say, the Reviewer's "comment on the PR"), with this machine's values.
## A sorter's settings: for each output, the rule that sends crates out of it.
func _fill_rules(machine: Dictionary) -> void:
	for child in _rules_box.get_children():
		child.queue_free()
	_hint(_rules_box, "Crates try the outputs in order, 1 to 6. Drag from a numbered port on the sorter to lay that output's belt.")
	var rules := {}
	for r in machine.get("rules", []):
		if r is Dictionary:
			rules[str(r.get("port", ""))] = r
	var p := _pipeline(_pid_of(str(machine.get("id", ""))))
	for port in SORTER_OUTPUTS:
		var to := ""
		for b in p.get("belts", []):
			if str(b.get("from", "")) == str(machine.get("id", "")) and str(b.get("port", "")) == port:
				to = str(b.get("to", ""))
		var rule: Dictionary = rules.get(port, {})
		var kind := str(rule.get("when", ""))
		_field_label(_rules_box, "Output %s (%s): %s" % [port.trim_prefix("out_"), SORTER_SIDES[port],
				("to " + _label_of(to)) if not to.is_empty() else "no belt yet"])
		var row := HBoxContainer.new()
		_rules_box.add_child(row)
		var pick := OptionButton.new()
		pick.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		for k in RULE_KINDS:
			pick.add_item(str(RULE_WORDS[k]))
		pick.select(maxi(0, RULE_KINDS.find(kind)))
		pick.item_selected.connect(func(index: int) -> void:
			var value := ""
			match str(RULE_KINDS[index]):
				"risk":
					value = "high"
				"source":
					value = "game"
				"size":
					value = "200"
			_set_rule(port, str(RULE_KINDS[index]), value)
			_fill_rules.call_deferred(_machine(_sel_key)))
		row.add_child(pick)
		var value := str(rule.get("value", ""))
		match kind:
			"risk", "source":
				var choices: Array = RISK_CHOICES if kind == "risk" else sources
				var values := OptionButton.new()
				values.size_flags_horizontal = Control.SIZE_EXPAND_FILL
				for i in choices.size():
					var c := str(choices[i])
					values.add_item(str(RISK_WORDS.get(c, c)) if kind == "risk" else str(SOURCE_NAMES.get(c, c)))
					if c == value:
						values.select(i)
				values.item_selected.connect(func(index: int) -> void: _set_rule(port, kind, str(choices[index])))
				_rules_box.add_child(values)
			"text", "files":
				var edit := LineEdit.new()
				edit.text = value
				edit.placeholder_text = "words, separated by commas" if kind == "text" else "e.g. docs/, *.md, src/api/*"
				edit.text_changed.connect(func(text: String) -> void: _set_rule(port, kind, text.strip_edges()))
				_rules_box.add_child(edit)
			"size":
				var lines := SpinBox.new()
				lines.min_value = 1
				lines.max_value = 100000
				lines.step = 10
				lines.value = float(value) if value.is_valid_int() else 200.0
				lines.value_changed.connect(func(n: float) -> void: _set_rule(port, kind, str(int(n))))
				_rules_box.add_child(lines)
	_fit_settings_window.call_deferred()


func _set_rule(port: String, kind: String, value: String) -> void:
	if _loading or _sel_key.is_empty():
		return
	var m := _machine(_sel_key)
	var rules: Array = (m.get("rules", []) as Array).filter(func(r) -> bool:
		return r is Dictionary and str(r.get("port", "")) != port)
	if not kind.is_empty():
		rules.append({"port": port, "when": kind, "value": value})
	rules.sort_custom(func(a, b) -> bool: return str(a["port"]) < str(b["port"]))
	m["rules"] = rules


func _fill_options(machine: Dictionary, base: Dictionary) -> void:
	for child in _options_box.get_children():
		child.queue_free()
	var values = machine.get("options", {})
	if not values is Dictionary:
		values = {}
	for spec in base.get("option_specs", []):
		var id := str(spec.get("id", ""))
		if str(spec.get("type", "")) == "choice":
			# One of a few values (e.g. how the Merger merges): a label and a drop-down.
			_field_label(_options_box, str(spec.get("label", id)))
			var pick := OptionButton.new()
			var choices: Array = spec.get("choices", [])
			var current_value := str(values.get(id, spec.get("default", "")))
			for i in choices.size():
				pick.add_item(str(choices[i].get("label", choices[i].get("value", ""))))
				if str(choices[i].get("value", "")) == current_value:
					pick.select(i)
			pick.item_selected.connect(func(index: int) -> void:
				if _loading or _sel_key.is_empty():
					return
				var m := _machine(_sel_key)
				var opts = m.get("options", {})
				if not opts is Dictionary:
					opts = {}
				opts[id] = str(choices[index].get("value", ""))
				m["options"] = opts)
			_options_box.add_child(pick)
			continue
		if str(spec.get("type", "")) == "text":
			# A text setting (e.g. who to ask for reviews): a label and a box to type in.
			_field_label(_options_box, str(spec.get("label", id)))
			var edit := LineEdit.new()
			edit.text = str(values.get(id, spec.get("default", "")))
			edit.placeholder_text = str(spec.get("placeholder", ""))
			edit.text_changed.connect(func(text: String) -> void:
				if _loading or _sel_key.is_empty():
					return
				var m := _machine(_sel_key)
				var opts = m.get("options", {})
				if not opts is Dictionary:
					opts = {}
				opts[id] = text.strip_edges()
				m["options"] = opts)
			_options_box.add_child(edit)
			continue
		var box := CheckBox.new()
		box.text = str(spec.get("label", id))
		box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		box.button_pressed = bool(values.get(id, spec.get("default", false)))
		box.toggled.connect(func(on: bool) -> void:
			if _loading or _sel_key.is_empty():
				return
			var m := _machine(_sel_key)
			var opts = m.get("options", {})
			if not opts is Dictionary:
				opts = {}
			opts[id] = on
			m["options"] = opts)
		_options_box.add_child(box)


func _on_scrap_kinds_changed() -> void:
	if _loading or _sel_key.is_empty():
		return
	var kinds: Array = []
	for kind in SCRAP_KINDS:
		var box: CheckBox = _scrap_checks[kind]
		if box.button_pressed or not box.visible:
			kinds.append(kind)
	if kinds.size() == SCRAP_KINDS.size():
		_machine(_sel_key).erase("scrap_when")  # all of them: the default
	else:
		_machine(_sel_key)["scrap_when"] = kinds


func _on_fail_selected(picked: int) -> void:
	if _loading or _sel_key.is_empty():
		return
	var pid := _pid_of(_sel_key)
	var p := _pipeline(pid)
	var id := _sel_key
	if picked <= 0:
		p["belts"] = (p["belts"] as Array).filter(func(b) -> bool:
			return not (str(b.get("from", "")) == id and str(b.get("port", "")) == "fail"))
	else:
		_connect(pid, id, "fail", str(_fail_targets[picked - 1]))
	_rebuild()


## Saves even a line that can't run yet: the backend pauses it and says why.
func _save() -> void:
	save_requested.emit(draft.duplicate(true))
