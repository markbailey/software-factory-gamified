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
signal belt_speed_changed(cells_per_second: float)  # applies at once, not on Save
var belt_speed_input: SpinBox

const FactoryFloor := preload("res://scripts/factory_floor.gd")
const Blueprints := preload("res://scripts/blueprints.gd")
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
	"github_pr": "Open PRs (mine, or the ones a search finds)",
}
# What a storage can hold, and its filter (blank: the default in STORE_FILTERS' second item).
const STORE_SOURCES := ["custom", "jira", "github", "github_pr", "pr_review", "prd"]
const STORE_WORDS := {"custom": "Custom (changes you describe)", "jira": "Jira issues", "github": "GitHub issues", "github_pr": "Open PRs",
		"pr_review": "PRs waiting for my review", "prd": "PRDs (GitHub issues labelled prd)"}
const STORE_FILTERS := {
	"jira": ["JQL", "e.g. project = APP AND status = Backlog", "Blank: the JQL in factory.toml."],
	"github": ["GitHub issue search", "e.g. is:open label:backlog", "Blank: open issues labelled factory."],
	"github_pr": ["GitHub PR search", "e.g. author:app/dependabot", "Blank: author:@me (your own open PRs)."],
	"pr_review": ["Extra search terms", "e.g. org:my-company -repo:owner/noisy", "Blank: every open PR waiting for your review."],
	"prd": ["", "", "Issues labelled prd in the line's repo. No filter."],
	"custom": ["", "", "Click it while the factory runs and describe a change: it becomes a crate on the line it feeds."],
}
# Stations you can place from the list: "@intake" starts a new line there (just its intake);
# "@dock" and "@scrap" place the factory's one shipping dock and scrap bin, which every line's
# belts can run into.
const STATION_TILES := {
	"@intake": {"name": "Intake (new line)", "sprite": "intake",
			"hint": "Starts a new line here. Belt Storages into it for issues and PRs (or tick typed specs in its settings), then run belts to the shipping dock (or join another line's machines)."},
	"@dock": {"name": "Shipping dock", "sprite": "shipping_dock",
			"hint": "Where finished crates end up. Place as many as you like: each line ships into whichever dock its last belt runs into."},
	"@scrap": {"name": "Scrap bin", "sprite": "scrap_bin",
			"hint": "Where failed crates go, from machines whose red belt runs into it. Place as many as you like, each with its own rules."},
	"@storage": {"name": "Storage", "sprite": "storage",
			"hint": "Holds one source's work (changes you describe, Jira issues, GitHub issues, open PRs, PRs waiting for your review or PRDs), with its own filter. Belt it into an intake and that line takes them."},
}
const STATION_HINTS := {
	"dock": "The shipping dock: crates that make it through every machine end here.",
	"scrap": "The scrap bin: crates land here from machines whose red belt runs into it, for the reasons ticked on each machine.",
}
# A sorter's outputs, and the rules you can give each one (graph.RULE_KINDS in the backend).
const SORTER_OUTPUTS := ["out_1", "out_2", "out_3", "out_4", "out_5", "out_6"]
const SORTER_SIDES := {"out_1": "north, left", "out_2": "north, right", "out_3": "east, top",
		"out_4": "east, bottom", "out_5": "south, left", "out_6": "south, right"}
const RULE_KINDS := ["", "risk", "source", "repo", "result", "text", "files", "size", "otherwise"]
const RULE_WORDS := {"": "No rule (not used)", "risk": "Risk is", "source": "Came from",
		"repo": "Repo is", "result": "Arrived as (passed / failed)",
		"text": "Spec mentions", "files": "Changes files", "size": "Changes at least (lines)",
		"otherwise": "Everything else"}
const RISK_CHOICES := ["low", "medium", "high", "medium+"]
const RISK_WORDS := {"low": "Low", "medium": "Medium", "high": "High", "medium+": "Medium or high"}
const SETTINGS_WIDTH := 360.0  # the left dock
const PORT_GRAB := 14.0
const PORT_GRAB_HAND := 26.0  # laying conveyor by hand: a press this close to a port starts there
const DRAG_THRESHOLD := 8.0  # screen pixels the mouse must move before a press becomes a drag  # how close (in world pixels) a press must be to grab a port

var floor_view: FactoryFloor
var catalog := {}  # type -> summary from the backend
var catalog_order: Array = []
var sources: Array = []
var accounts := {}  # the backend's "accounts" message: {github: [...], jira: [...], defaults}
var _line_accounts := {}  # service -> OptionButton in the intake settings
var _store_account_label: Label
var _store_account: OptionButton
var default_repo := {}  # factory.toml's [workspace] repo, as the backend says (not a default: lines pick their own)
var known_repos: Array = []  # the factory's repo list (gear, Repos): [{github, saved, in_use, default}]
# Sources that come from the line's own repo: lines on different repos can each take them.
const REPO_SOURCES := ["github", "prd", "github_pr"]
var draft: Array = []  # pipelines being edited
var current := ""  # pipeline id being edited

var _sel_key := ""  # footprint key of the selected machine or station
var _place_rot := 0  # quarter turns clockwise for the next thing placed (R turns it)
const CROSS_HINT := "To bridge another belt, go straight over a straight stretch of it (not a bend, a join, or a bridge already there)."
var _sel_belt := {}  # {pid, from, port} of the selected belt
var _placing := ""  # machine type being placed
var _hand_belt := false  # the Conveyor belt tile is on: belts dragged from a port are laid cell by cell
var _belt_tile: Button
var _drag := {}
var _loading := false

# Undo: one history for every build edit, oldest first. {"who": "build", "draft", "current"}
# holds the layout before a build edit; {"who": "rooms"} marks a room edit, which the room
# layer undoes itself (rooms_undo).
const UNDO_MAX := 100
var _history: Array = []
var _last: Array = []  # the draft as of the last entry, to spot the next change
var _restoring := false
var rooms_undo := Callable()  # set by main: undoes the room layer's latest edit
var rooms_tracked := false  # the room layer tells us about its edits (else Undo falls back to it)
signal undo_changed  # the history grew or shrank (the room bar's Undo button follows it)
var _undo_button: Button

# Several things picked at once (Shift+click, or Shift+drag a box on empty floor): footprint
# keys. Dragging any of them moves them all; Delete removes them.
var _group := {}
var _pending_rooms_step := false  # the room layer took an undo step the history must undo too
var _room_cells := {}  # room blocks picked with the group (Vector2i -> true), moved and removed with it

# Copy and paste (blueprints.gd): Ctrl+C copies what's picked, Ctrl+V (or a blueprint) puts a
# ghost of it on the mouse; R turns it, a click pastes it, Esc stops.
var _clip := {}  # what Ctrl+C copied last
var _paste := {}  # the clip following the mouse, turned as you turned it
var _paste_name := ""  # what it is, for the messages: "the copy" or the blueprint's name
var _blueprints: Array = []  # [{name, clip}], as in user://blueprints.json
var _bp_name: LineEdit
var _bp_list: VBoxContainer
var _bp_empty: Label
var _copy_button: Button
var _paste_button: Button
var _dup_button: Button
var _bp_save_button: Button

var _pipeline_picker: OptionButton
var _pipeline_name: LineEdit
var _intake_box: VBoxContainer
var _source_boxes: VBoxContainer
var _source_note: Label
var _repo_github: OptionButton
var _repo_path: LineEdit
var _repo_base: LineEdit
var _repo_branch_pattern: LineEdit  # the line's own branch naming pattern (blank: the factory's)
var factory_branch_pattern := ""  # the one under the gear, for the placeholder text
var _repo_result: Label
var _store_box: VBoxContainer  # a storage's settings
var _store_source: OptionButton
var _store_query_label: Label
var _store_query: LineEdit
var _store_hint: Label
var _store_repo_label: Label
var _store_repo: OptionButton
var _store_listed_box: CheckBox  # PRs to review: only the repos in the factory's repo list
var _store_own_prs_box: CheckBox  # Open PRs: also take PRs the factory opened for crates still on a line
var _store_on_box: CheckBox  # switched on (off: it isn't checked and brings nothing in)
var _check_only_box: CheckBox
var _max_crates_input: SpinBox
var _bin_box: VBoxContainer  # the scrap bin's settings
var _bin_close_box: CheckBox
var _bin_close_prs_box: CheckBox
var _bin_jira_status: LineEdit
var _bin_retry_box: CheckBox  # hand the issue back for the factory to try again
var _bin_retries_input: SpinBox
var settings_window: PanelContainer
var _settings_col: VBoxContainer
var _palette: GridContainer
var _palette_hint: Label
var _machine_box: VBoxContainer
var _title: Label
var _description: Label
var _name_input: LineEdit
var _gate_box: CheckBox
var _switch_box: CheckBox  # switched on (off: it runs nothing and crates pass straight through)
var _timeout_input: SpinBox
var _timeout_label: Label
var _retries_input: SpinBox
var _retries_label: Label
var _retries_hint: Label
var _agent_box: VBoxContainer  # Claude model and effort, for machines that run Claude
var _model_picker: OptionButton
var _effort_picker: OptionButton
var _agent_hint: Label
# What the pickers list, and the factory-wide default under the gear (set_agent_settings).
var agent_choices := {"models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"],
		"efforts": ["low", "medium", "high", "xhigh", "max"]}
var agent_defaults := {"model": "", "effort": ""}
var _steps_label: Label
var _fail_label: Label
var _auto_box: VBoxContainer
var _auto_picker: OptionButton
var _auto_hint: Label
var _scrap_box: VBoxContainer
var _options_box: VBoxContainer  # the machine type's own tick boxes (machines.toml options)
# Settings shown only while a choice has some value (show_when = "review_type=security"):
# [{"when": String, "nodes": [Control]}], and the option specs, for their defaults.
var _option_rows: Array = []
var _option_specs: Array = []
var _rules_box: VBoxContainer  # a sorter's rule for each output
var _scrap_checks := {}  # kind -> CheckBox
var _prompt_label: Label
var _prompt_input: TextEdit
var _prompt_note: Label  # in place of the prompt while the machine's settings don't use it
var _prompt_base := {}  # the selected machine's catalogue entry (its prompt_when and notes)
var _belt_box: VBoxContainer
var _belt_label: Label
var _auto_route_button: Button
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
	_hint(col, "Click anything on the floor to open its settings. Drag to move it, R turns it; Shift+drag moves its whole line. Shift+click machines, or Shift+drag a box on empty floor, to pick several (room blocks in the box come too); drag any of them to move them together. Ctrl+Z or Undo steps back through every change, rooms included. Drag from a green port to the next machine to lay a belt. Failed crates leave by the red port, along whatever belt you lay from it: back to an earlier machine for rework, on to any machine or dock, or into a scrap bin. With no red belt, failed crates wait at the machine for you. Hold Space and drag, or drag empty floor, to pan.")

	_field_label(col, "Lines")
	_pipeline_picker = OptionButton.new()
	_pipeline_picker.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_pipeline_picker.item_selected.connect(_on_pipeline_picked)
	col.add_child(_pipeline_picker)
	var line_row := HBoxContainer.new()
	col.add_child(line_row)
	_button(line_row, "New", _new_pipeline, "icon_add", "start a new line").theme_type_variation = &"ButtonGhost"
	_button(line_row, "Delete", _delete_pipeline, "icon_delete", "delete the line picked above").theme_type_variation = &"ButtonGhost"
	_button(line_row, "Clear all", _clear_factory, "icon_clear_all", "remove every line, machine and belt").theme_type_variation = &"ButtonGhost"
	_field_label(col, "Belt speed (cells a second)")
	belt_speed_input = SpinBox.new()
	belt_speed_input.min_value = 0.5
	belt_speed_input.max_value = 6.0
	belt_speed_input.step = 0.1
	belt_speed_input.value = 1.5
	belt_speed_input.tooltip_text = "How fast every belt runs, and so how fast crates ride them. Changes straight away."
	belt_speed_input.value_changed.connect(func(v: float) -> void: belt_speed_changed.emit(v))
	col.add_child(belt_speed_input)

	col.add_child(HSeparator.new())
	_field_label(col, "Add a machine")
	_palette = GridContainer.new()
	_palette.columns = 2
	col.add_child(_palette)
	_palette_hint = _hint(col, "")

	col.add_child(HSeparator.new())
	_build_blueprint_ui(col)

	_message = Label.new()
	_message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_message.add_theme_color_override("font_color", Color("F2B84C"))
	outer.add_child(_message)
	var actions := HBoxContainer.new()
	actions.alignment = BoxContainer.ALIGNMENT_END
	outer.add_child(actions)
	_undo_button = _button(actions, "Undo", undo, "icon_undo", "undo the last change: machines, belts, settings or rooms (Ctrl+Z)")
	_undo_button.theme_type_variation = &"ButtonGhost"
	_undo_button.focus_mode = Control.FOCUS_NONE
	_button(actions, "Cancel", func() -> void: cancelled.emit(), "icon_close", "drop the changes and leave build mode").theme_type_variation = &"ButtonGhost"
	_button(actions, "Save", _save, "icon_save", "keep the changes and leave build mode").theme_type_variation = &"ButtonPrimary"

	_build_settings_window()


## Copy, paste and blueprints: under the palette.
func _build_blueprint_ui(col: VBoxContainer) -> void:
	_field_label(col, "Copy and blueprints")
	var row := HBoxContainer.new()
	col.add_child(row)
	_dup_button = _button(row, "Duplicate", duplicate_picked)
	_dup_button.tooltip_text = "Duplicate: copy what's picked and paste it where you click (R turns it)"
	_copy_button = _button(row, "Copy", copy_picked)
	_copy_button.tooltip_text = "Copy what's picked, with its settings and the belts between (Ctrl+C)"
	_paste_button = _button(row, "Paste", paste_clip)
	_paste_button.tooltip_text = "Paste the last copy where you click; R turns it, Esc stops (Ctrl+V)"
	for b in [_dup_button, _copy_button, _paste_button]:
		b.theme_type_variation = &"ButtonGhost"
		b.focus_mode = Control.FOCUS_NONE
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_hint(col, "Pick machines and belts (click, Shift+click, or Shift+drag a box), then Ctrl+C and Ctrl+V. Pasted machines keep their settings and the belts between them; a line comes along as a new line when its intake is picked too.")
	var save_row := HBoxContainer.new()
	col.add_child(save_row)
	_bp_name = LineEdit.new()
	_bp_name.placeholder_text = "Blueprint name"
	_bp_name.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_bp_name.text_submitted.connect(func(_t: String) -> void: save_blueprint())
	save_row.add_child(_bp_name)
	_bp_save_button = _button(save_row, "Save blueprint", save_blueprint, "icon_save",
			"keep what's picked as a blueprint under this name, to paste whenever you like")
	_bp_save_button.theme_type_variation = &"ButtonGhost"
	_bp_list = VBoxContainer.new()
	col.add_child(_bp_list)
	_bp_empty = _hint(col, "No blueprints yet. Pick some machines, name it and press Save blueprint.")
	_blueprints = Blueprints.load_all()
	_fill_blueprints()


func _fill_blueprints() -> void:
	if _bp_list == null:
		return
	for child in _bp_list.get_children():
		child.queue_free()
	for i in _blueprints.size():
		var bp: Dictionary = _blueprints[i]
		var row := HBoxContainer.new()
		var b := Button.new()
		b.text = str(bp["name"])
		b.alignment = HORIZONTAL_ALIGNMENT_LEFT
		b.clip_text = true
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		b.tooltip_text = "Paste %s (%s): click the floor where it goes, R turns it." % [bp["name"], Blueprints.words(bp["clip"])]
		b.focus_mode = Control.FOCUS_NONE
		b.pressed.connect(paste_blueprint.bind(i))
		row.add_child(b)
		var del := _button(row, "Delete", delete_blueprint.bind(i), "icon_delete", "forget this blueprint")
		del.theme_type_variation = &"ButtonGhost"
		del.focus_mode = Control.FOCUS_NONE
		_bp_list.add_child(row)
	if _bp_empty:
		_bp_empty.visible = _blueprints.is_empty()


## What's picked: the group, or the one thing whose settings are open.
func _picked_keys() -> Array:
	if floor_view == null:
		return []
	if not _group.is_empty():
		return _group.keys()
	if not _sel_key.is_empty() and floor_view.footprints.has(_sel_key):
		return [_sel_key]
	return []


func _capture_picked() -> Dictionary:
	if floor_view == null:
		return {}
	return Blueprints.capture(self, _picked_keys(), _room_cells.keys())


## Ctrl+C: copy what's picked.
func copy_picked() -> bool:
	var clip := _capture_picked()
	if clip.is_empty():
		show_message("Pick something to copy first: click it, Shift+click more, or Shift+drag a box round them.")
		return false
	_clip = clip
	_refresh_ui()
	show_message("Copied %s. Ctrl+V pastes it." % Blueprints.words(clip))
	return true


## Ctrl+V: the last copy follows the mouse until you click where it goes.
func paste_clip() -> void:
	if _clip.is_empty():
		show_message("Nothing copied yet: pick some machines and press Ctrl+C (or Copy).")
		return
	_start_paste(_clip, "the copy")


func duplicate_picked() -> void:
	if copy_picked():
		paste_clip()


func paste_blueprint(index: int) -> void:
	if index < 0 or index >= _blueprints.size():
		return
	_start_paste(_blueprints[index]["clip"], str(_blueprints[index]["name"]))


func save_blueprint() -> void:
	var clip := _capture_picked()
	if clip.is_empty():
		show_message("Pick what goes in the blueprint first: click it, Shift+click more, or Shift+drag a box round them.")
		return
	var bp_name := _bp_name.text.strip_edges()
	if bp_name.is_empty():
		var n := _blueprints.size() + 1
		bp_name = "Blueprint %d" % n
		while _blueprints.any(func(bp) -> bool: return str(bp["name"]) == bp_name):
			n += 1
			bp_name = "Blueprint %d" % n
	var entry := {"name": bp_name, "clip": clip}
	var replaced := false
	for i in _blueprints.size():
		if str(_blueprints[i]["name"]).to_lower() == bp_name.to_lower():
			_blueprints[i] = entry
			replaced = true
	if not replaced:
		_blueprints.append(entry)
	if not Blueprints.save_all(_blueprints):
		show_message("Couldn't write the blueprints file, so %s isn't kept after you quit." % bp_name)
	else:
		show_message("%s blueprint %s (%s). Click it in the list to paste it." % [
				"Updated" if replaced else "Saved", bp_name, Blueprints.words(clip)])
	_bp_name.text = ""
	_bp_name.release_focus()
	_fill_blueprints()


func delete_blueprint(index: int) -> void:
	if index < 0 or index >= _blueprints.size():
		return
	var gone: Dictionary = _blueprints[index]
	_blueprints.remove_at(index)
	Blueprints.save_all(_blueprints)
	_fill_blueprints()
	show_message("Deleted the blueprint %s." % str(gone["name"]))


func _start_paste(clip: Dictionary, what: String) -> void:
	_placing = ""
	_hand_belt = false
	_drag = {}
	_paste = clip.duplicate(true)
	_paste_name = what
	_clear_group()
	_sel_key = ""
	_sel_belt = {}
	_refresh_ui()
	_move_paste_ghost(floor_view.get_global_mouse_position())
	show_message("Click the floor where %s goes. R turns it (Shift+R back), Esc stops." % what)


func _paste_at(world: Vector2) -> Vector2i:
	return floor_view.cell_at(world) - Blueprints.size_of(_paste) / 2


func _move_paste_ghost(world: Vector2) -> void:
	if not _paste.is_empty():
		floor_view.set_ghost(Blueprints.ghost(self, _paste, _paste_at(world)))


func _stop_paste() -> void:
	_paste = {}
	floor_view.set_ghost({})


## A click while pasting: put it down, as one undo step (rooms too), and pick what was pasted
## so a drag moves it straight away.
func _drop_paste(world: Vector2) -> void:
	var at := _paste_at(world)
	if not Blueprints.fits(self, _paste, at):
		show_message("It would land on something else. Pick an empty spot (R turns it).")
		return
	var clip := _paste
	_stop_paste()
	note_edit()
	var steps := _history.size()
	var placed: Dictionary = Blueprints.paste(self, clip, at)
	if placed.get("rooms_changed", false):
		_pending_rooms_step = true
	_rebuild()
	_relink_free_conveyors()
	_push_group_step(steps)
	_clear_group()
	_sel_key = ""
	_sel_belt = {}
	for k in placed.get("keys", []):
		if floor_view.footprints.has(k):
			_group[k] = true
	_room_cells = (placed.get("room_cells", {}) as Dictionary).duplicate()
	if _picked_count() == 1 and _room_cells.is_empty():
		var only := str(_group.keys()[0])
		_clear_group()
		_select(only)
	_refresh_ui()
	show_message("Pasted %s (%s). Drag it to move it, Ctrl+Z takes it back. Belt it to a dock or the next machine to join it up." % [
			_paste_name, Blueprints.words(clip)])


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
	_switch_box = CheckBox.new()
	_switch_box.text = "Switched on"
	_switch_box.tooltip_text = "Untick to switch this machine off: it stays on the floor but runs nothing, and crates go straight out of its green output (with a note in their log) until you tick it again."
	_switch_box.toggled.connect(_on_switch_toggled)
	_machine_box.add_child(_switch_box)
	# Reviewers: let a good review of a low-risk change through without asking.
	_auto_box = VBoxContainer.new()
	_machine_box.add_child(_auto_box)
	_field_label(_auto_box, "Approve by itself when the crate's risk is")
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
	# Where failures go is the red belt you lay from the red port: this only says where it runs.
	_fail_label = _hint(_machine_box, "")
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
	_retries_label = _field_label(_machine_box, "Retries when it fails")
	_retries_hint = _hint(_machine_box, "Runs a failed crate through this machine again by itself, with why it failed, before its red belt (or holding it). A red belt back to an earlier machine goes first.")
	_retries_input = SpinBox.new()
	_retries_input.min_value = 0
	_retries_input.max_value = 5
	_retries_input.step = 1
	_retries_input.value_changed.connect(_on_retries_changed)
	_machine_box.add_child(_retries_input)
	_agent_box = VBoxContainer.new()
	_machine_box.add_child(_agent_box)
	_field_label(_agent_box, "Claude model")
	_model_picker = OptionButton.new()
	_model_picker.clip_text = true
	_model_picker.item_selected.connect(func(i: int) -> void: _on_agent_picked("model", _model_picker, i))
	_agent_box.add_child(_model_picker)
	_field_label(_agent_box, "Effort level")
	_effort_picker = OptionButton.new()
	_effort_picker.clip_text = true
	_effort_picker.item_selected.connect(func(i: int) -> void: _on_agent_picked("effort", _effort_picker, i))
	_agent_box.add_child(_effort_picker)
	_agent_hint = _hint(_agent_box, "Default follows the factory's choice under the gear (Factory settings, Claude). Higher effort thinks longer and costs more.")
	_steps_label = _hint(_machine_box, "")
	_prompt_label = _field_label(_machine_box, "Prompt for Claude")
	_prompt_input = TextEdit.new()
	_prompt_input.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_prompt_input.custom_minimum_size = Vector2(0, 220)
	_prompt_input.text_changed.connect(_on_prompt_changed)
	_machine_box.add_child(_prompt_input)
	_prompt_note = _hint(_machine_box, "")
	_prompt_note.visible = false
	_button(_machine_box, "Turn ↻  (R)", func() -> void: turn_node(_sel_key, 1)).tooltip_text = \
			"Turn it a quarter turn clockwise, so belts go in and out on other sides (R; Shift+R turns it back)"
	_button(_machine_box, "Remove machine", _remove_selected, "icon_delete", "take this machine off the floor (Delete)").theme_type_variation = &"ButtonReject"

	_intake_box = VBoxContainer.new()
	col.add_child(_intake_box)
	_hint(_intake_box, "New crates start here. Drag from the green port to the first machine. Shift+drag any part of the line to move all of it.")
	_field_label(_intake_box, "Line name")
	_pipeline_name = LineEdit.new()
	_pipeline_name.max_length = 40
	_pipeline_name.text_changed.connect(_on_pipeline_name)
	_intake_box.add_child(_pipeline_name)
	_field_label(_intake_box, "Repo this line works on")
	_hint(_intake_box, "The line's own repo: crates typed in the game, Jira issues and storages without a repo of their own are worked on here (branch, PR and merge). There's no default: a line with no repo is paused until you pick one.")
	_field_label(_intake_box, "GitHub repo (add repos under the gear: Factory settings, Repos)")
	_repo_github = OptionButton.new()
	_repo_github.clip_text = true
	_repo_github.item_selected.connect(func(i: int) -> void:
		_on_repo_changed("github", str(_repo_github.get_item_metadata(i))))
	_intake_box.add_child(_repo_github)
	_repo_path = _repo_field("Local folder (blank: the factory clones it for you)", "path")
	_repo_base = _repo_field("Base branch (blank: the repo's default branch)", "base_branch")
	_repo_branch_pattern = _repo_field("Branch naming (blank: the factory's, under the gear)", "branch_pattern")
	_repo_branch_pattern.tooltip_text = "How this line's crates' branches are named, e.g. feature/{issue}-{slug}. Placeholders: {id} {issue} {issue_number} {slug} {line} {machine} {source} {repo} {date}"
	var check_row := HBoxContainer.new()
	_intake_box.add_child(check_row)
	var check := _button(check_row, "Check repo", _on_check_repo, "icon_check_repo", "test that the factory can reach this repo")
	check.theme_type_variation = &"ButtonGhost"
	_repo_result = _hint(_intake_box, "")
	_field_label(_intake_box, "Accounts this line works as")
	_hint(_intake_box, "Its machines (pushes, PRs, issue comments) and its storages work as these, unless a storage picks its own. Add accounts with the gear in the top bar.")
	for service in ["github", "jira"]:
		var pick := OptionButton.new()
		pick.clip_text = true
		pick.item_selected.connect(func(i: int) -> void: _on_line_account_picked(service, i))
		_intake_box.add_child(pick)
		_line_accounts[service] = pick
	_field_label(_intake_box, "Take crates from")
	_source_boxes = VBoxContainer.new()
	_intake_box.add_child(_source_boxes)
	_source_note = _hint(_intake_box, "")
	_check_only_box = CheckBox.new()
	_check_only_box.text = "Only check these issues (Verifier, Labeller): don't assign them or move them to In Progress"
	_check_only_box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_check_only_box.toggled.connect(_on_check_only_toggled)
	_intake_box.add_child(_check_only_box)
	_field_label(_intake_box, "Most crates on the line at once")
	_hint(_intake_box, "To stop jams: more wait at the intake, and its storages bring nothing new in until one leaves. Crates waiting for your approval or held for you don't count. 0: no limit.")
	_max_crates_input = SpinBox.new()
	_max_crates_input.min_value = 0
	_max_crates_input.max_value = 99
	_max_crates_input.step = 1
	_max_crates_input.value_changed.connect(_on_max_crates_changed)
	_intake_box.add_child(_max_crates_input)

	_bin_box = VBoxContainer.new()
	col.add_child(_bin_box)
	_bin_close_box = CheckBox.new()
	_bin_close_box.text = "Close the issue of a crate that lands here (GitHub: closed as not planned)"
	_bin_close_box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bin_close_box.toggled.connect(func(on: bool) -> void: _set_bin("close_issues", on))
	_bin_box.add_child(_bin_close_box)
	_bin_close_prs_box = CheckBox.new()
	_bin_close_prs_box.text = "Close the crate's PR too (its branch is kept; a PR you only review is never closed)"
	_bin_close_prs_box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bin_close_prs_box.toggled.connect(func(on: bool) -> void: _set_bin("close_prs", on))
	_bin_box.add_child(_bin_close_prs_box)
	_field_label(_bin_box, "Jira status to move it to")
	_bin_jira_status = LineEdit.new()
	_bin_jira_status.placeholder_text = "Won't Do"
	_bin_jira_status.text_changed.connect(func(text: String) -> void: _set_bin("jira_status", text.strip_edges()))
	_bin_box.add_child(_bin_jira_status)
	_hint(_bin_box, "With the first box unticked, the issue is handed back instead: unassigned, labelled with why, and moved back to To Do.")
	_bin_retry_box = CheckBox.new()
	_bin_retry_box.text = "Hand back for retry: comment why it failed and leave it labelled for the factory, so the intake picks it up again"
	_bin_retry_box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bin_retry_box.toggled.connect(func(on: bool) -> void: _set_bin("retry_issues", on))
	_bin_box.add_child(_bin_retry_box)
	_field_label(_bin_box, "Most retries per issue")
	_hint(_bin_box, "Only when the issue is handed back (first box unticked). The next crate's spec carries why the last try failed. After this many retries it's handed back as usual.")
	_bin_retries_input = SpinBox.new()
	_bin_retries_input.min_value = 1
	_bin_retries_input.max_value = 10
	_bin_retries_input.step = 1
	_bin_retries_input.value_changed.connect(func(v: float) -> void: _set_bin("max_retries", int(v)))
	_bin_box.add_child(_bin_retries_input)

	_store_box = VBoxContainer.new()
	col.add_child(_store_box)
	_field_label(_store_box, "What it holds")
	_store_source = OptionButton.new()
	for src in STORE_SOURCES:
		_store_source.add_item(str(STORE_WORDS[src]))
	_store_source.item_selected.connect(_on_store_source_picked)
	_store_box.add_child(_store_source)
	_store_on_box = CheckBox.new()
	_store_on_box.text = "Switched on"
	_store_on_box.tooltip_text = "Untick to switch this storage off: it stays where it is, but isn't checked and brings nothing in until you tick it again."
	_store_on_box.toggled.connect(_on_store_on_toggled)
	_store_box.add_child(_store_on_box)
	_store_query_label = _field_label(_store_box, "Filter")
	_store_query = LineEdit.new()
	_store_query.text_changed.connect(_on_store_query_changed)
	_store_box.add_child(_store_query)
	_store_hint = _hint(_store_box, "")
	_store_repo_label = _field_label(_store_box, "GitHub repo it reads")
	_store_repo = OptionButton.new()
	_store_repo.clip_text = true
	_store_repo.item_selected.connect(func(i: int) -> void:
		_on_store_repo_changed(str(_store_repo.get_item_metadata(i))))
	_store_box.add_child(_store_repo)
	_store_listed_box = CheckBox.new()
	_store_listed_box.text = "Only repos in the factory's repo list (gear, Repos)"
	_store_listed_box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_store_listed_box.toggled.connect(_on_store_listed_toggled)
	_store_box.add_child(_store_listed_box)
	_store_own_prs_box = CheckBox.new()
	_store_own_prs_box.text = "Include the factory's own PRs (ones opened for crates still on a line)"
	_store_own_prs_box.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_store_own_prs_box.toggled.connect(_on_store_own_prs_toggled)
	_store_box.add_child(_store_own_prs_box)
	_store_account_label = _field_label(_store_box, "Account it works as")
	_store_account = OptionButton.new()
	_store_account.clip_text = true
	_store_account.item_selected.connect(_on_store_account_picked)
	_store_box.add_child(_store_account)
	_hint(_store_box, "Drag from its port into an intake: that line takes what's in it. Several storages can feed one intake: lay this one's belt into the side of another storage's belt. Crates from a storage with its own repo are coded, PR'd and merged in that repo, so one line can serve several repos. Press Delete to remove it.")

	_belt_box = VBoxContainer.new()
	col.add_child(_belt_box)
	_belt_label = Label.new()
	_belt_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_belt_box.add_child(_belt_label)
	_hint(_belt_box, "To lay it by hand, turn on Conveyor belt in the machine list and drag from the output again.")
	_auto_route_button = _button(_belt_box, "Let it find its own way", _auto_route_selected, "icon_route", "drop the hand-laid path and route the belt automatically")
	_button(_belt_box, "Remove belt", _remove_selected, "icon_delete", "take this belt off the floor (Delete)").theme_type_variation = &"ButtonReject"

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


## With an icon the button shows only the icon; its name and what it does go in the tooltip.
func _button(parent: Control, label: String, on_press: Callable, icon_name := "", what := "") -> Button:
	var b := Button.new()
	if icon_name.is_empty():
		b.text = label
	else:
		b.icon = load("res://assets/ui/%s.svg" % icon_name)
		b.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER  # centred when a container stretches it
		b.tooltip_text = "%s: %s" % [label, what] if not what.is_empty() else label
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
	# The conveyor belt first: lay belts by hand, cell by cell, instead of letting them route.
	_belt_tile = Button.new()
	_belt_tile.theme_type_variation = &"ButtonTile"
	_belt_tile.alignment = HORIZONTAL_ALIGNMENT_LEFT
	_belt_tile.text = "Conveyor belt"
	_belt_tile.tooltip_text = "Lay a belt by hand: drag across the floor, cell by cell, from a machine's output into where it goes."
	_belt_tile.toggle_mode = true
	_belt_tile.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_belt_tile.clip_text = true
	if ResourceLoader.exists("res://assets/belts/belt_straight_e.svg"):
		_belt_tile.icon = load("res://assets/belts/belt_straight_e.svg")
	_belt_tile.toggled.connect(_on_belt_tile_toggled)
	_palette.add_child(_belt_tile)
	_hand_belt = false
	# Stations next: a new line (from its intake) and extra scrap bins.
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
	_paste = {}
	_drag = {}
	_sel_key = ""
	_sel_belt = {}
	var paused := PackedStringArray()
	for p in draft:
		if p.get("paused", false):
			paused.append(str(p.get("problem", "")))
	show_message("" if paused.is_empty() else "Paused until fixed:\n" + "\n".join(paused))
	floor_view.editing = true
	_history.clear()
	_clear_group()
	_last = _snapshot()
	_rebuild()
	_last = _snapshot()
	undo_changed.emit()


func close() -> void:
	settings_window.visible = false
	_paste = {}
	_history.clear()
	_clear_group()
	undo_changed.emit()
	if floor_view:
		floor_view.editing = false
		floor_view.set_ghost({})
		floor_view.set_wire({})
		floor_view.set_highlight("")
		_show_group()


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
		for bin in p.get("bins", []) + p.get("docks", []):
			if str(bin.get("id", "")) == place:
				return bin
		for st in FactoryFloor.storages_of(p):
			if str(st.get("id", "")) == place:
				return st
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
	return FactoryFloor.is_dock(node)


func _label_of(node: String) -> String:
	if _is_dock(node):
		return "the shipping dock"
	match node:
		"intake":
			return "the intake"
	if FactoryFloor.is_bin(node):
		return "the scrap bin"
	if FactoryFloor.is_storage(node):
		var st := _node(node) if ":" in node else {}
		return "the %s storage" % str(STORE_WORDS.get(str(st.get("source", "")), "")) if not st.is_empty() else "the storage"
	var m := _machine(node)
	return str(m.get("name", node)) if not m.is_empty() else node


func _rebuild() -> void:
	floor_view.set_pipelines(draft)
	note_edit()
	_refresh_ui()


# Undo --------------------------------------------------------------------------

## Ctrl+Z while building, before the floor and the room bar see it.
func _input(event: InputEvent) -> void:
	if not visible:
		return
	var key := event as InputEventKey
	if key == null or not key.pressed or key.echo or key.keycode != KEY_Z or not key.is_command_or_control_pressed():
		return
	var focus := get_viewport().gui_get_focus_owner()
	if focus is LineEdit or focus is TextEdit:
		return  # their own undo
	undo()
	get_viewport().set_input_as_handled()


## The draft as it stands (deep), with where we are.
func _snapshot() -> Array:
	return draft.duplicate(true)


## The layout without its belts' routes (the floor routes them again as things move).
static func _shape(pipelines: Array) -> Array:
	var out := pipelines.duplicate(true)
	for p in out:
		if p is Dictionary:
			for b in p.get("belts", []):
				if b is Dictionary:
					b.erase("path")
	return out


## Record the draft's latest change, if it changed since the last entry. Called after every
## edit (settings typed in are recorded when you next click or pick something).
func note_edit() -> void:
	if _restoring or not visible:
		return
	if _shape(draft) == _shape(_last):
		_last = _snapshot()  # only routes moved
		return
	_push({"who": "build", "draft": _last, "current": current})
	_last = _snapshot()


func _push(entry: Dictionary) -> void:
	_history.append(entry)
	if _history.size() > UNDO_MAX:
		_history.pop_front()
	undo_changed.emit()
	if _undo_button:
		_undo_button.disabled = false


## The room layer made an edit: it takes its turn in the history.
func note_room_edit() -> void:
	note_edit()  # settings typed before it come first
	_push({"who": "rooms"})


## Take the room edits out of the history (the room layer dropped its own undo steps).
func forget_room_edits() -> void:
	_history = _history.filter(func(e) -> bool: return str(e.get("who", "")) != "rooms")
	undo_changed.emit()
	_refresh_ui()


func can_undo() -> bool:
	return not _history.is_empty() or (visible and _shape(draft) != _shape(_last))


## Undo the latest edit, whichever kind it was.
func undo() -> void:
	note_edit()
	if _history.is_empty():
		if not rooms_tracked and rooms_undo.is_valid():
			rooms_undo.call()  # an older room layer keeps its own history
			return
		show_message("Nothing to undo.")
		return
	var entry: Dictionary = _history.pop_back()
	if str(entry.get("who", "")) == "rooms":
		if rooms_undo.is_valid():
			rooms_undo.call()
		show_message("Undid the last room change.")
	else:
		if entry.get("rooms", false) and rooms_undo.is_valid():
			rooms_undo.call()  # the room blocks that moved or went with the machines
		draft = (entry["draft"] as Array).duplicate(true)
		if _pipeline(str(entry.get("current", ""))).is_empty():
			current = str(draft[0].get("id", "")) if not draft.is_empty() else ""
		else:
			current = str(entry["current"])
		_placing = ""
		_paste = {}
		_drag = {}
		floor_view.set_ghost({})
		floor_view.set_wire({})
		_restoring = true
		floor_view.set_pipelines(draft)
		_restoring = false
		for k in _group.keys():
			if not floor_view.footprints.has(k):
				_group.erase(k)
		if not _sel_key.is_empty() and not floor_view.footprints.has(_sel_key):
			_sel_key = ""
		if not _sel_belt.is_empty() and _find_belt(_sel_belt).is_empty():
			_sel_belt = {}
		_last = _snapshot()
		_refresh_ui()
		show_message("Undid the last change.")
	undo_changed.emit()


func _refresh_ui() -> void:
	_keep_picks_exclusive()
	if _undo_button:
		_undo_button.disabled = not can_undo()
	_show_group()
	_loading = true
	_pipeline_picker.clear()
	for i in draft.size():
		var p: Dictionary = draft[i]
		_pipeline_picker.add_item(str(p.get("name", p.get("id", ""))))
		if str(p.get("id", "")) == current:
			_pipeline_picker.select(i)
	if _belt_tile:
		_belt_tile.set_pressed_no_signal(_hand_belt)
	for b in [_copy_button, _dup_button, _bp_save_button]:
		if b:
			b.disabled = _picked_keys().is_empty() and _room_cells.is_empty()
	if _paste_button:
		_paste_button.disabled = _clip.is_empty()
	if not _paste.is_empty():
		_palette_hint.text = "Click the floor where %s goes. R turns it, Esc stops." % _paste_name
	elif _hand_belt:
		_palette_hint.text = "Drag across the floor, cell by cell, from a machine's output (or the cell just right of it) into where the belt goes. Esc stops."
	elif _placing.is_empty():
		_palette_hint.text = "Click a machine, then click the floor where it goes. Drag from an output to belt it up."
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
	_bin_box.visible = FactoryFloor.is_bin(station)
	_store_box.visible = FactoryFloor.is_storage(station)
	if FactoryFloor.is_storage(station):
		_fill_storage(_node(_sel_key))
	if FactoryFloor.is_bin(station):
		var bin := _bin_station()
		_bin_close_box.button_pressed = bool(bin.get("close_issues", true))
		_bin_close_prs_box.button_pressed = bool(bin.get("close_prs", true))
		_bin_jira_status.text = str(bin.get("jira_status", ""))
		_bin_retry_box.button_pressed = bool(bin.get("retry_issues", false))
		_bin_retries_input.set_value_no_signal(float(int(bin.get("max_retries", 2))))
	if station == "intake":
		var pipeline := _pipeline(current)
		_title.text = "%s intake" % str(pipeline.get("name", ""))
		_pipeline_name.text = str(pipeline.get("name", ""))
		_fill_sources(pipeline)
		_fill_repo(pipeline)
		_check_only_box.button_pressed = bool(pipeline.get("check_only", false))
		_max_crates_input.set_value_no_signal(float(int(pipeline.get("max_crates", 0))))
	elif FactoryFloor.is_storage(station):
		_title.text = "Storage: %s" % str(STORE_WORDS.get(str(_node(_sel_key).get("source", "")), ""))
	elif not station.is_empty():
		_title.text = "Shipping dock" if FactoryFloor.is_dock(station) else "Scrap bin"
		if station != "scrap" and FactoryFloor.is_bin(station):
			station = "scrap"  # same hint; this one can be deleted
		elif station != "dock" and FactoryFloor.is_dock(station):
			station = "dock"
			_station_label.text = ""
	if not machine.is_empty():
		var base: Dictionary = catalog.get(str(machine.get("type", "")), {})
		_title.text = str(machine.get("name", base.get("name", "")))
		_description.text = str(base.get("description", ""))
		_name_input.text = str(machine.get("name", ""))
		_gate_box.button_pressed = FactoryFloor.gated(machine, base)
		_switch_box.visible = bool(base.get("can_switch_off", false))
		_switch_box.set_pressed_no_signal(machine.get("enabled", true) != false)
		var auto := str(machine.get("auto_approve", ""))
		_auto_box.visible = bool(base.get("can_auto_approve", false)) and FactoryFloor.gated(machine, base)
		_auto_picker.select(maxi(0, AUTO_APPROVE_LEVELS.find(auto)))
		_auto_hint.text = ("Needs a Risk analyser earlier on the line. Anything rated higher, or not "
				+ "rated, still waits for you.") if auto != "" else "Every crate waits for you here."
		var timeout = machine.get("timeout_seconds", null)
		_timeout_input.set_value_no_signal(roundf(float(timeout) / 60.0) if timeout else 0.0)
		_retries_input.set_value_no_signal(float(int(machine.get("retries", 0) if machine.get("retries", 0) != null else 0)))
		_agent_box.visible = bool(base.get("uses_agent", false))
		if _agent_box.visible:
			_fill_agent_picker(_model_picker, "model", machine)
			_fill_agent_picker(_effort_picker, "effort", machine)
		var steps: Array = base.get("steps", [])
		var names := PackedStringArray()
		for s in steps:
			names.append(str(s))
		_steps_label.text = ("Steps: " + ", ".join(names)) if not names.is_empty() else ""
		var has_prompt := bool(base.get("has_prompt", false))
		_prompt_base = base if has_prompt else {}
		_prompt_label.text = str(base.get("prompt_label", "")) if not str(base.get("prompt_label", "")).is_empty() \
				else "Prompt for Claude"
		_prompt_label.visible = has_prompt
		_prompt_input.visible = has_prompt
		_prompt_note.visible = false
		if has_prompt and _prompt_input.text != str(machine.get("prompt", "")):
			_prompt_input.text = str(machine.get("prompt", ""))
		_fill_fail_info(_sel_key)
		_fill_options(machine, base)
		# A splitter or belt merger only passes crates on: no rules, waits, timeouts or red port.
		var kind := str(base.get("kind", ""))
		var sorter := bool(base.get("sorter", false)) or kind == "merger"
		# A sorter (kind "sorter") gets a rule for each output; a splitter or merger has none.
		var has_rules := sorter and not kind in ["splitter", "merger"]
		_rules_box.visible = has_rules
		for part in [_gate_box, _fail_label, _timeout_input, _timeout_label]:
			part.visible = not sorter
		# The Approver always waits and the Reviewer never does: no tick for either.
		_gate_box.visible = not sorter and str(base.get("gate_setting", "choice")) == "choice"
		_timeout_input.visible = not sorter and not (base.get("steps", []) as Array).is_empty()
		_timeout_label.visible = _timeout_input.visible
		for part in [_retries_input, _retries_label, _retries_hint]:
			part.visible = _timeout_input.visible
		if sorter:
			_scrap_box.visible = false
			if has_rules:
				_fill_rules(machine)
	_belt_box.visible = not _sel_belt.is_empty()
	if not _sel_belt.is_empty():
		var b := _find_belt(_sel_belt)
		var kind := "Rework belt" if _sel_belt["port"] == "fail" else "Belt"
		_title.text = kind
		_belt_label.text = "From %s to %s" % [_label_of(_sel_belt["from"]), _label_of(str(b.get("to", "")))]
		if not str(_sel_belt.get("conv", "")).is_empty():
			_title.text = "Conveyor loop" if b.get("loop", false) else "Conveyor"
			_belt_label.text = "%d cells, laid by hand and not linked. Press Delete to take it up." % (b.get("cells", []) as Array).size()
		elif b.get("manual", false):
			_belt_label.text += " (laid by hand)"
		_auto_route_button.visible = b.get("manual", false)
	_station_label.visible = STATION_HINTS.has(station)
	_station_label.text = str(STATION_HINTS.get(station, ""))
	if FactoryFloor.is_bin(_sel_key) and not _sel_key.ends_with(":scrap"):
		_station_label.text += " Press Delete to remove this one; red belts into it are taken off too."
	elif FactoryFloor.is_dock(_sel_key) and not _sel_key.ends_with(":dock"):
		_station_label.text += " Press Delete to remove this one; belts into it are taken off too."
	_loading = false
	var route := -1
	if not _sel_belt.is_empty():
		for i in floor_view.routes.size():
			var r: Dictionary = floor_view.routes[i]
			if r["pid"] == _sel_belt["pid"] and r["from"] == _sel_belt["from"] and r["port"] == _sel_belt["port"] \
					and str(r.get("conveyor", "")) == str(_sel_belt.get("conv", "")):
				route = i
	floor_view.set_highlight(_sel_key, route)
	_fit_settings_window.call_deferred()


func _find_belt(ref: Dictionary) -> Dictionary:
	if not str(ref.get("conv", "")).is_empty():
		for conv in FactoryFloor.conveyors_of(_pipeline(str(ref.get("pid", "")))):
			if str(conv.get("id", "")) == ref["conv"]:
				return conv
		return {}
	for b in _pipeline(str(ref.get("pid", ""))).get("belts", []):
		if str(b.get("from", "")) == ref["from"] and str(b.get("port", "")) == ref["port"]:
			return b
	return {}


func _select(key: String, belt := {}) -> void:
	if not key.is_empty() or not belt.is_empty():
		_clear_group()  # one thing picked: room blocks and any group let go, so keys act on it
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


## A machine, station or belt with its settings open and room blocks picked are never both
## picked: whatever opened last wins, so Delete, R, Esc and Ctrl+C act on what you see picked.
## (Placing a machine sets _sel_key without _select, so this also runs on every refresh.)
func _keep_picks_exclusive() -> void:
	if _room_cells.is_empty() or not _group.is_empty():
		return  # a Shift+box group keeps machines and room blocks together
	if (not _sel_key.is_empty() and floor_view != null and floor_view.footprints.has(_sel_key)) \
			or not _sel_belt.is_empty():
		_room_cells.clear()


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


## The typed-specs tick, and what the storages belted into this intake hold.
func _fill_sources(pipeline: Dictionary) -> void:
	for child in _source_boxes.get_children():
		child.queue_free()
	var box := CheckBox.new()
	box.text = str(SOURCE_NAMES["game"])
	box.button_pressed = "game" in _sources_of(pipeline)
	box.toggled.connect(func(on: bool) -> void: _on_source_toggled("game", on))
	_source_boxes.add_child(box)
	var fed := FactoryFloor.fed_storages(pipeline)
	var names := PackedStringArray()
	for st in fed:
		var q := str(st.get("query", "")).strip_edges()
		var r := github_name(str(st.get("repo", "")))
		var bits := PackedStringArray([r] if not r.is_empty() else []) + PackedStringArray([q] if not q.is_empty() else [])
		names.append(str(STORE_WORDS.get(str(st.get("source", "")), "")) + ((" (%s)" % ", ".join(bits)) if not bits.is_empty() else ""))
	_source_note.text = ("Also from its storages: %s." % ", ".join(names)) if not names.is_empty() else \
			"Issues and PRs come from storages: place a Storage, pick what it holds, and drag from its port into this intake."


## A source as one line at a time can take it: GitHub issues and PRDs once per repo.
func _source_key(src: String, p: Dictionary) -> String:
	var own := ""
	for st in FactoryFloor.fed_storages(p):
		if str(st.get("source", "")) == src and not str(st.get("repo", "")).strip_edges().is_empty():
			own = github_name(str(st["repo"]))
	var key := "%s@%s" % [src, own if not own.is_empty() else repo_key(p)] if src in REPO_SOURCES else src
	var queries := {}
	for st in FactoryFloor.fed_storages(p):
		queries[str(st.get("source", ""))] = str(st.get("query", ""))
	if str(queries.get(src, "")).strip_edges() != "":
		key += "?" + " ".join(str(queries[src]).split(" ", false)).to_lower()  # its own search
	return key


## Which repo a line is on, as the backend tells them apart ("" is none picked).
func repo_key(p: Dictionary) -> String:
	var repo: Dictionary = p.get("repo", {}) if p.get("repo") is Dictionary else {}
	var github := github_name(str(repo.get("github", "")))
	var path := str(repo.get("path", "")).strip_edges()
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


## The backend's "accounts" message: what the account pickers offer.
func set_accounts(msg: Dictionary) -> void:
	accounts = msg
	if _store_account == null:
		return  # not built yet
	_loading = true
	if visible and _store_box.visible and not _sel_key.is_empty():
		var st := _node(_sel_key)
		if st.has("source"):
			_fill_storage(st)
	var p := _pipeline(current)
	if not p.is_empty():
		_fill_line_accounts(p)
	_loading = false


## [[choice, words], ...] for a picker: inherit ("" , the words given), this machine's, each account.
func _account_choices(service: String, inherit: String) -> Array:
	var out: Array = [["", inherit], ["@machine", "This machine's sign-in"]]
	for a in accounts.get(service, []):
		out.append([str(a.get("id", "")), str(a.get("name", a.get("id", "")))])
	return out


func _account_name(service: String, choice: String) -> String:
	if choice == "@machine" or choice.is_empty():
		return "this machine's sign-in"
	for a in accounts.get(service, []):
		if str(a.get("id", "")) == choice:
			return str(a.get("name", choice))
	return "this machine's sign-in"


func _fill_picker(pick: OptionButton, choices: Array, current_choice: String) -> void:
	pick.clear()
	var found := 0
	for i in choices.size():
		pick.add_item(str(choices[i][1]))
		if str(choices[i][0]) == current_choice:
			found = i
	pick.select(found)


func _line_account(p: Dictionary, service: String) -> String:
	var raw: Dictionary = p.get("accounts", {}) if p.get("accounts") is Dictionary else {}
	return str(raw.get(service, ""))


func _fill_line_accounts(p: Dictionary) -> void:
	var defaults: Dictionary = accounts.get("defaults", {}) if accounts.get("defaults") is Dictionary else {}
	for service in _line_accounts:
		var word := "GitHub" if service == "github" else "Jira"
		var inherit := "%s: the default (%s)" % [word, _account_name(service, str(defaults.get(service, "")))]
		var choices := _account_choices(service, inherit)
		for c in choices.slice(1):
			c[1] = "%s: %s" % [word, c[1]]
		_fill_picker(_line_accounts[service], choices, _line_account(p, service))


func _on_line_account_picked(service: String, index: int) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if p.is_empty():
		return
	var choice := str(_account_choices(service, "")[index][0])
	var raw: Dictionary = (p.get("accounts", {}) if p.get("accounts") is Dictionary else {}).duplicate()
	if choice.is_empty():
		raw.erase(service)
	else:
		raw[service] = choice
	if raw.is_empty():
		p.erase("accounts")
	else:
		p["accounts"] = raw


## The service a storage's account is on: Jira for Jira issues, none for Custom, else GitHub.
static func _store_service(source: String) -> String:
	return "jira" if source == "jira" else ("" if source == "custom" else "github")


func _on_store_account_picked(index: int) -> void:
	if _loading or index < 0:
		return
	var st := _node(_sel_key)
	var service := _store_service(str(st.get("source", "")))
	if st.is_empty() or service.is_empty():
		return
	var choice := str(_account_choices(service, "")[index][0])
	if choice.is_empty():
		st.erase("account")
	else:
		st["account"] = choice


func _fill_repo(pipeline: Dictionary) -> void:
	_fill_line_accounts(pipeline)
	var repo: Dictionary = pipeline.get("repo", {}) if pipeline.get("repo") is Dictionary else {}
	_fill_repo_picker(_repo_github, str(repo.get("github", "")), "Pick a repo for this line")
	_repo_path.text = str(repo.get("path", ""))
	_repo_base.text = str(repo.get("base_branch", ""))
	_repo_path.placeholder_text = "e.g. C:\\code\\my-app"
	_repo_base.placeholder_text = "main"
	_repo_branch_pattern.text = str(repo.get("branch_pattern", ""))
	_repo_branch_pattern.placeholder_text = factory_branch_pattern if not factory_branch_pattern.is_empty() else "crate/{id}"
	_repo_result.remove_theme_color_override("font_color")
	_repo_result.text = _repo_words(pipeline.get("repo_info", {}))


## The factory's repo list from the backend (snapshot or "repos" message).
func set_known_repos(repos: Array) -> void:
	known_repos = repos.duplicate(true)
	if visible and settings_window and settings_window.visible:
		_refresh_ui()


func _known_repo_names() -> Array:
	var out: Array = []
	for r in known_repos:
		if r is Dictionary and not str(r.get("github", "")).is_empty():
			out.append(str(r["github"]))
	return out


## Fill a repo drop-down: blank_words first (saved as no repo), then the factory's repos. A
## saved repo that isn't on the list any more still shows, so picking nothing changes nothing.
func _fill_repo_picker(pick: OptionButton, current: String, blank_words: String) -> void:
	pick.clear()
	pick.add_item(blank_words)
	pick.set_item_metadata(0, "")
	var want := github_name(current) if not current.strip_edges().is_empty() else ""
	var chosen := 0
	for repo_name in _known_repo_names():
		pick.add_item(repo_name)
		pick.set_item_metadata(pick.item_count - 1, repo_name)
		if not want.is_empty() and github_name(repo_name) == want:
			chosen = pick.item_count - 1
	if not want.is_empty() and chosen == 0:
		pick.add_item("%s (not on the repo list)" % current.strip_edges())
		pick.set_item_metadata(pick.item_count - 1, current.strip_edges())
		chosen = pick.item_count - 1
	pick.select(chosen)


## What a line's saved repo is, in words: "Works on owner/name (main), cloned in <folder>."
func _repo_words(info) -> String:
	if not info is Dictionary or info.is_empty():
		return "Not saved yet: press Check repo to see if it's right."
	if info.get("unset", false):
		return "No repo picked: this line is paused until you pick one."
	var what := str(info.get("github", "")) if not str(info.get("github", "")).is_empty() else "a local repo"
	var base := str(info.get("base_branch", ""))
	return "Works on %s%s, in %s.%s" % [what, (" (from %s)" % base) if not base.is_empty() else "",
			str(info.get("path", "")), ""]


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
	return FactoryFloor.line_sources(p)


## A storage's settings: what it holds and its filter.
func _fill_storage(st: Dictionary) -> void:
	var source := str(st.get("source", "jira"))
	_store_source.select(maxi(0, STORE_SOURCES.find(source)))
	var f: Array = STORE_FILTERS.get(source, ["", "", ""])
	var has_filter := not str(f[0]).is_empty()
	_store_query_label.visible = has_filter
	_store_query.visible = has_filter
	_store_query_label.text = "Filter (%s)" % f[0] if has_filter else ""
	_store_query.placeholder_text = str(f[1])
	if _store_query.text != str(st.get("query", "")):
		_store_query.text = str(st.get("query", ""))
	var into := ""
	var line_repo := ""
	for p in draft:
		if FactoryFloor.fed_storages(p).has(st):
			into = str(p.get("name", p.get("id", "")))
			var r: Dictionary = p.get("repo", {}) if p.get("repo") is Dictionary else {}
			line_repo = str(r.get("github", ""))
	var reads_repo := source in ["github", "github_pr", "prd", "jira", "custom"]
	_store_repo_label.text = "Repo its crates are worked on" if source in ["jira", "custom"] \
			else "GitHub repo it reads"
	_store_repo_label.visible = reads_repo
	_store_repo.visible = reads_repo
	_fill_repo_picker(_store_repo, str(st.get("repo", "")),
			("The line's repo (%s)" % line_repo) if not line_repo.is_empty() else "The line's repo (none picked yet)")
	var listed := bool(st.get("listed_repos_only", false))
	_store_listed_box.visible = source in ["pr_review", "github_pr"]
	_store_listed_box.set_pressed_no_signal(listed)
	_store_own_prs_box.visible = source == "github_pr"
	_store_own_prs_box.set_pressed_no_signal(bool(st.get("include_factory_prs", false)))
	if source == "github_pr" and listed:  # ticked, it reads every repo in the list instead
		_store_repo_label.visible = false
		_store_repo.visible = false
	_store_on_box.set_pressed_no_signal(FactoryFloor.storage_on(st))
	_store_hint.text = str(f[2]) + ("  It feeds %s." % into if not into.is_empty() else "  Not belted into an intake yet.")
	var service := _store_service(source)
	_store_account_label.visible = not service.is_empty()
	_store_account.visible = not service.is_empty()
	if not service.is_empty():
		var line_choice := ""
		for p in draft:
			if FactoryFloor.fed_storages(p).has(st):
				line_choice = _line_account(p, service)
		if line_choice.is_empty():
			var defaults: Dictionary = accounts.get("defaults", {}) if accounts.get("defaults") is Dictionary else {}
			line_choice = str(defaults.get(service, ""))
		_fill_picker(_store_account, _account_choices(service, "The line's (%s)" % _account_name(service, line_choice)),
				str(st.get("account", "")))


func _on_store_source_picked(index: int) -> void:
	if _loading or index < 0:
		return
	var st := _node(_sel_key)
	if st.is_empty():
		return
	st["source"] = STORE_SOURCES[index]
	st.erase("query")  # a JQL isn't a GitHub search
	st.erase("account")  # nor a Jira account a GitHub one
	st.erase("listed_repos_only")  # PRs to review and Open PRs each have their own tick
	st.erase("include_factory_prs")
	_rebuild()


func _on_store_repo_changed(text: String) -> void:
	if _loading:
		return
	var st := _node(_sel_key)
	if st.is_empty():
		return
	if text.strip_edges().is_empty():
		st.erase("repo")
	else:
		st["repo"] = text.strip_edges()


func _on_store_on_toggled(on: bool) -> void:
	if _loading:
		return
	var st := _node(_sel_key)
	if st.is_empty():
		return
	if on:
		st.erase("enabled")
	else:
		st["enabled"] = false
	floor_view.set_pipelines(draft)


func _on_store_listed_toggled(on: bool) -> void:
	if _loading:
		return
	var st := _node(_sel_key)
	if st.is_empty():
		return
	if on:
		st["listed_repos_only"] = true
		if str(st.get("source", "")) == "github_pr":
			st.erase("repo")  # Open PRs: the list replaces its one repo
	else:
		st.erase("listed_repos_only")
	_rebuild()  # Open PRs shows or hides its repo picker


func _on_store_own_prs_toggled(on: bool) -> void:
	if _loading:
		return
	var st := _node(_sel_key)
	if st.is_empty():
		return
	if on:
		st["include_factory_prs"] = true
	else:
		st.erase("include_factory_prs")


func _on_store_query_changed(text: String) -> void:
	if _loading:
		return
	var st := _node(_sel_key)
	if st.is_empty():
		return
	if text.strip_edges().is_empty():
		st.erase("query")
	else:
		st["query"] = text.strip_edges()
	floor_view.set_pipelines(draft)


func _on_check_only_toggled(on: bool) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if not p.is_empty():
		p["check_only"] = on


func _on_max_crates_changed(value: float) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if p.is_empty():
		return
	if int(value) > 0:
		p["max_crates"] = int(value)
	else:
		p.erase("max_crates")


## The selected scrap bin's saved dictionary (it holds its pos and its rules).
func _bin_station() -> Dictionary:
	if not ":" in _sel_key:
		return {}
	var station = _node(_sel_key)  # "scrap" or an extra bin: each has its own rules
	return station if station is Dictionary and FactoryFloor.is_bin(_sel_key) else {}


func _set_bin(key: String, value) -> void:
	if _loading:
		return
	var bin := _bin_station()
	if not bin.is_empty():
		bin[key] = value


func _on_source_toggled(src: String, on: bool) -> void:
	if _loading:
		return
	var p := _pipeline(current)
	if p.is_empty():
		return
	p["sources"] = ["game"] if on else []
	p.erase("source")
	if not p.has("storages"):
		p["storages"] = []


func _new_pipeline(at := Vector2i(-1, -1), with_intake := true) -> void:
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
	var line := {"id": "line-%d" % n, "name": "Line %d" % n, "sources": [], "storages": [],
			"machines": [], "belts": []}
	if with_intake:
		line["intake"] = {"pos": [x, y]}
	draft.append(line)
	current = "line-%d" % n
	if not with_intake:
		return  # a storage placed before any intake: the caller puts it on this line
	_sel_key = ""
	_sel_belt = {}
	_rebuild()
	_select("line-%d:intake" % n)
	show_message("New line added. Belt a Storage into its intake (or tick typed specs) for its crates, then add machines and run belts from the intake to the shipping dock (any line's).")


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
	for st in ["intake"] + FactoryFloor.docks_of(gone) + FactoryFloor.bins_of(gone):
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
	_last = draft.duplicate(true)  # the saved layout: undo still steps back from it
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
	if not _paste.is_empty():
		_stop_paste()
	_hand_belt = false
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
		var waiting := _pipeline(current)
		if not waiting.is_empty() and not waiting.get("intake") is Dictionary:
			# A line that so far is only storages: this is its intake.
			waiting["intake"] = _turned({"pos": [tl.x, tl.y]})
			_rebuild()
			_select("%s:intake" % current)
			show_message("Placed its intake. Drag from it to the line's first machine, and from each storage's port into it.")
			return
		_new_pipeline(tl)
		var made := _pipeline(current)
		if _place_rot != 0 and made.get("intake") is Dictionary:
			_turned(made["intake"])
			_rebuild()
		return
	if kind == "@dock" or kind == "@scrap":
		var station := "dock" if kind == "@dock" else "scrap"
		var what := "shipping dock" if kind == "@dock" else "scrap bin"
		_placing = ""
		var owner_line := _line_for_placing()
		var place := station
		if owner_line.get(station) is Dictionary:
			# This line has one already: another one, as an extra ("dock_2", "scrap_2").
			var list_key := "docks" if station == "dock" else "bins"
			var taken: Array = FactoryFloor.docks_of(owner_line) if station == "dock" else FactoryFloor.bins_of(owner_line)
			var n := 2
			while ("%s_%d" % [station, n]) in taken:
				n += 1
			place = "%s_%d" % [station, n]
			var extras: Array = owner_line.get(list_key, [])
			extras.append(_turned({"id": place, "pos": [tl.x, tl.y]}))
			owner_line[list_key] = extras
		else:
			owner_line[station] = _turned({"pos": [tl.x, tl.y]})
		_sel_key = "%s:%s" % [current, place]
		_sel_belt = {}
		_rebuild()
		show_message("Placed the %s. Run belts into it from any line." % what)
		return
	if kind == "@storage":
		_placing = ""
		var line := _pipeline(current)
		if line.is_empty():
			# No line yet: the storage starts one, with no intake until you place it.
			_new_pipeline(Vector2i.ZERO, false)
			line = _pipeline(current)
		var taken := {}
		for q in draft:
			for st in FactoryFloor.storages_of(q):
				taken[str(st.get("id", ""))] = true
		var n := 1
		while taken.has("store_%d" % n):
			n += 1
		var stores: Array = FactoryFloor.storages_of(line)
		stores.append(_turned({"id": "store_%d" % n, "source": "jira", "pos": [tl.x, tl.y]}))
		line["storages"] = stores
		_sel_key = "%s:store_%d" % [current, n]
		_sel_belt = {}
		_rebuild()
		show_message("Pick what it holds and its filter, then drag from its port into an intake." if line.get("intake") is Dictionary
				else "Pick what it holds and its filter. Then place an Intake and drag from the storage's port into it.")
		return
	if kind == "@extra_bin":
		var line := _line_for_placing()
		var taken := FactoryFloor.bins_of(line)
		var n := 2
		while ("scrap_%d" % n) in taken:
			n += 1
		var bins: Array = line.get("bins", [])
		bins.append(_turned({"id": "scrap_%d" % n, "pos": [tl.x, tl.y]}))
		line["bins"] = bins
		_placing = ""
		_sel_key = "%s:scrap_%d" % [current, n]
		_sel_belt = {}
		_rebuild()
		show_message("Now drag a belt from a machine of %s into it (usually from its red port)." % str(line.get("name", current)))
		return
	var base: Dictionary = catalog.get(kind, {})
	var p := _line_for_placing()
	var id := _new_id(kind)
	var list: Array = p.get("machines", [])
	list.append(_turned({
		"id": id, "type": kind, "name": str(base.get("name", kind)), "pos": [tl.x, tl.y],
		"gate": bool(base.get("gate", false)), "prompt": str(base.get("prompt", "")),
		"timeout_seconds": null, "sprite": str(base.get("sprite", "")), "steps": base.get("steps", []),
	}))
	p["machines"] = list
	_placing = ""
	_sel_key = id
	_sel_belt = {}
	_rebuild()
	show_message("Now drag from a green port into the %s, and from its green port on to the next machine." % str(base.get("name", kind)))


## A node about to be placed, turned the way the ghost faced (R while placing).
func _turned(node: Dictionary) -> Dictionary:
	if _place_rot != 0:
		node["rot"] = _place_rot
	return node


## R (Shift+R the other way): turn what's about to be placed, or the machine or station picked
## (or being dragged), a quarter turn. Its belts are routed again to the ports' new sides.
func _turn_key(step: int) -> bool:
	if not _paste.is_empty():
		_paste = Blueprints.turned(_paste, step)
		_move_paste_ghost(floor_view.get_global_mouse_position())
		return true
	if not _placing.is_empty():
		_place_rot = posmod(_place_rot + step, 4)
		if not floor_view.ghost.is_empty():
			var g: Dictionary = floor_view.ghost.duplicate()
			g["rot"] = _place_rot
			floor_view.set_ghost(g)
		show_message("Facing %s: belts go in on its %s side. R turns it again." % [_facing_words(_place_rot),
				_side_words(_place_rot)])
		return true
	var key := str(_drag.get("key", "")) if str(_drag.get("mode", "")) == "move" else _sel_key
	if key.is_empty() and _picked_count() == 1 and not _group.is_empty():
		key = str(_group.keys()[0])
	if key.is_empty() or not floor_view.footprints.has(key):
		return false
	turn_node(key, step)
	if str(_drag.get("mode", "")) == "move" and not floor_view.ghost.is_empty():
		var g: Dictionary = floor_view.ghost.duplicate()
		g["rot"] = floor_view.rot_of(key)
		floor_view.set_ghost(g)
	return true


## Turn a machine or station a quarter turn clockwise (step 1) or back (-1).
func turn_node(key: String, step := 1) -> void:
	var node := _node(key)
	if node.is_empty():
		return
	var turns := posmod(FactoryFloor.turns_of(node) + step, 4)
	if turns == 0:
		node.erase("rot")
	else:
		node["rot"] = turns
	_place_rot = turns  # the next one placed faces the same way
	_rebuild()
	_relink_free_conveyors()  # a conveyor now ending at its turned input links up
	show_message("Turned: belts now go in on its %s side and come out on the %s. R turns it again, Shift+R back." % [
			_side_words(turns), _side_words(turns + 2)])


static func _facing_words(turns: int) -> String:
	return ["east", "south", "west", "north"][posmod(turns, 4)]


## The side a node with these turns takes belts in on.
static func _side_words(turns: int) -> String:
	return ["left", "top", "right", "bottom"][posmod(turns, 4)]


## The line something placed goes on: the one picked, or with none yet, a new line with no
## intake (place an Intake later and it becomes that line's; belt into its machines from another
## line and they join that one).
func _line_for_placing() -> Dictionary:
	var p := _pipeline(current)
	if p.is_empty():
		_new_pipeline(Vector2i.ZERO, false)
		p = _pipeline(current)
	return p


## A line with nothing on it any more (all its machines joined another): taken away.
func _drop_if_empty(pid: String) -> void:
	var p := _pipeline(pid)
	if p.is_empty() or p.get("intake") is Dictionary or p.get("dock") is Dictionary or p.get("scrap") is Dictionary:
		return
	for key in ["machines", "storages", "bins", "docks", "conveyors"]:
		if not (p.get(key, []) as Array).is_empty():
			return
	draft.erase(p)
	if current == pid:
		current = str(draft[0].get("id", "")) if not draft.is_empty() else ""


func _connect(pid: String, from: String, port: String, to: String, path: Array = []) -> void:
	var p := _pipeline(pid)
	var belts: Array = p.get("belts", [])
	for b in belts.duplicate():
		if str(b.get("from", "")) == from and str(b.get("port", "")) == port:
			belts.erase(b)
	var belt := {"from": from, "port": port, "to": to}
	if not path.is_empty():
		belt["path"] = path  # laid by hand: the floor keeps it while it fits
		belt["manual"] = true
	belts.append(belt)
	p["belts"] = belts


## A belt laid by hand goes back to routing itself.
func _auto_route_selected() -> void:
	var b := _find_belt(_sel_belt)
	if b.is_empty():
		return
	b.erase("manual")
	b.erase("path")
	_rebuild()


func _remove_selected() -> void:
	if not _sel_belt.is_empty() and not str(_sel_belt.get("conv", "")).is_empty():
		_drop_conveyor(_sel_belt["pid"], _find_belt(_sel_belt))
		_sel_belt = {}
		_rebuild()
		return
	if not _sel_belt.is_empty():
		var p := _pipeline(_sel_belt["pid"])
		(p["belts"] as Array).erase(_find_belt(_sel_belt))
		_sel_belt = {}
		_rebuild()
		return
	if _sel_key.is_empty():
		return
	if FactoryFloor.is_storage(_sel_key):
		var owner := _pipeline(_pid_of(_sel_key))
		var sid := _end_name(_sel_key)
		owner["storages"] = FactoryFloor.storages_of(owner).filter(func(st) -> bool: return str(st.get("id", "")) != sid)
		owner["belts"] = (owner.get("belts", []) as Array).filter(func(b) -> bool: return str(b.get("from", "")) != sid)
		_sel_key = ""
		_rebuild()
		show_message("Removed the storage. Its line no longer takes what it held.")
		return
	if ":" in _sel_key and _end_name(_sel_key) != "intake":
		# The dock or a scrap bin: belts from every line that ran into it go with it.
		var line := _pipeline(_pid_of(_sel_key))
		var place := _end_name(_sel_key)
		if FactoryFloor.is_bin(place) and place != "scrap":
			line["bins"] = (line.get("bins", []) as Array).filter(func(b) -> bool: return str(b.get("id", "")) != place)
		elif FactoryFloor.is_dock(place) and place != "dock":
			line["docks"] = (line.get("docks", []) as Array).filter(func(b) -> bool: return str(b.get("id", "")) != place)
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
		note_edit()
		if key.keycode == KEY_ESCAPE and not _paste.is_empty():
			_stop_paste()
			_refresh_ui()
			show_message("")
			return true
		if key.is_command_or_control_pressed() and not key.shift_pressed and not key.alt_pressed:
			if key.keycode == KEY_C:
				copy_picked()
				return true
			if key.keycode == KEY_V:
				paste_clip()
				return true
		if key.keycode == KEY_ESCAPE:
			if _hand_belt and _drag.is_empty():
				_hand_belt = false
				_refresh_ui()
			elif not _placing.is_empty() or not _drag.is_empty():
				_placing = ""
				_drag = {}
				floor_view.set_ghost({})
				floor_view.set_wire({})
				_refresh_ui()
			elif _picked_count() > 0:
				_clear_group()
				_refresh_ui()
			else:
				_select("")
			return true
		if key.keycode == KEY_R and not key.is_command_or_control_pressed():
			return _turn_key(-1 if key.shift_pressed else 1)
		if key.keycode == KEY_DELETE or key.keycode == KEY_BACKSPACE:
			_keep_picks_exclusive()
			if _picked_count() > 1 or not _room_cells.is_empty():  # one picked room block counts too
				_remove_group()
			else:
				_remove_selected()
			return true
		return false

	var motion := event as InputEventMouseMotion
	if motion:
		if not _paste.is_empty():
			_move_paste_ghost(world)
			return true
		if not _placing.is_empty():
			var tl := floor_view.cell_at(world) - Vector2i(1, 1)
			floor_view.set_ghost({"pos": tl, "ok": floor_view.is_free(tl), "rot": _place_rot})
			return true
		if str(_drag.get("mode", "")) == "press":
			if get_viewport().get_mouse_position().distance_to(_drag["screen"]) < DRAG_THRESHOLD:
				return true
			if _drag.get("group_move", false):
				_drag["mode"] = "group"
			else:
				_drag["mode"] = "line" if _drag.get("whole_line", false) else "move"
			settings_window.visible = false  # out of the way while dragging
		match str(_drag.get("mode", "")):
			"group":
				var shift: Vector2i = floor_view.cell_at(world) - _drag["grab"]
				var gbox: Rect2i = _drag["box"]
				floor_view.set_ghost({"pos": gbox.position + shift, "size": gbox.size, "ok": _group_fits(shift)})
				return true
			"box":
				_drag["to"] = world
				floor_view.set_select_box(Rect2(_drag["from"], Vector2.ZERO).expand(world))
				return true
			"move":
				var tl: Vector2i = floor_view.cell_at(world) - _drag["offset"]
				floor_view.set_ghost({"pos": tl, "ok": floor_view.is_free(tl, _drag["key"]),
						"rot": floor_view.rot_of(str(_drag["key"]))})
				return true
			"line":
				var delta: Vector2i = floor_view.cell_at(world) - _drag["grab"]
				var box: Rect2i = _drag["box"]
				floor_view.set_ghost({"pos": box.position + delta, "size": box.size,
						"ok": floor_view.line_fits(_drag["pid"], delta)})
				return true
			"wire":
				if _drag.get("hand", false):
					_lay_cell(floor_view.cell_at(world))
					floor_view.set_wire({"cells": _drag["cells"], "kind": _drag["kind"]})
				else:
					floor_view.set_wire({"from": _drag["from"], "to": world, "kind": _drag["kind"]})
				return true
		return false

	var click := event as InputEventMouseButton
	if click and click.pressed and click.button_index == MOUSE_BUTTON_RIGHT and not _paste.is_empty():
		_stop_paste()  # a right click stops pasting, like Esc
		_refresh_ui()
		show_message("")
		return true
	if click == null or click.button_index != MOUSE_BUTTON_LEFT:
		return false
	if click.pressed and not _paste.is_empty():
		note_edit()
		_drop_paste(world)
		return true
	if not click.pressed and not _paste.is_empty():
		return true
	if click.pressed:
		note_edit()  # settings typed in so far are one undo step
		return _press(world)
	return _release(world)


## True where a left press would do nothing in build mode (so main can pan instead): not
## placing a machine, and no port, machine, station or belt under the mouse.
func is_empty_floor(world: Vector2) -> bool:
	if not _placing.is_empty() or not _paste.is_empty() or Input.is_key_pressed(KEY_SHIFT):
		return false  # Shift+drag on empty floor draws a box to pick machines
	var cell := floor_view.cell_at(world)
	if _room_cells.has(cell):
		return false  # a picked room block: a drag moves it (and the rest of the group)
	if not _hand_belt and _room_block_at(cell):
		return false  # a wall, door, window, port or sign: a press picks it, a drag moves it
	if _hand_belt and floor_view.node_at(world).is_empty():
		return false  # Conveyor belt is on: a drag on the floor lays belt
	for k in floor_view.footprints:
		var ports: Dictionary = floor_view.output_ports(k)
		for port in ports:
			if world.distance_to(ports[port]) <= _port_grab():
				return false
	if not floor_view.node_at(world).is_empty():
		return false
	return floor_view.route_at(floor_view.cell_at(world)) < 0


## How close a press must be to a port to start a belt there (further with Conveyor belt on).
func _port_grab() -> float:
	return PORT_GRAB_HAND if _hand_belt else PORT_GRAB


## A click (no drag) on empty floor: close the settings.
func click_empty_floor() -> void:
	# A click on a door picks it (Delete removes it); the floor counts as empty for panning.
	var rooms := _rooms()
	var c := floor_view.cell_at(floor_view.get_global_mouse_position())
	if rooms and rooms.has_method("door_at") and rooms.door_at(c):
		pick_room_cell(c)
		return
	# Anything else lets go of what was picked: the open machine or belt, and any group.
	var had_group := _picked_count() > 0
	_clear_group()
	_select("")
	if had_group:
		show_message("")


## A room block a plain press picks (and a drag moves): walls, windows, doors, belt ports,
## signs and worker doors. Floor and footbridges don't, so a drag there still pans or picks
## the belt; Shift+box picks those.
func _room_block_at(c: Vector2i) -> bool:
	var rooms := _rooms()
	if rooms == null:
		return false
	if rooms.has_method("block_at"):
		return rooms.block_at(c)
	return rooms.has_method("door_at") and rooms.door_at(c)


func _press(world: Vector2) -> bool:
	if not _placing.is_empty():
		var tl := floor_view.cell_at(world) - Vector2i(1, 1)
		if floor_view.is_free(tl):
			floor_view.set_ghost({})
			_place(_placing, tl)
			_relink_free_conveyors()  # a conveyor laid before may now run from its output or into it
		else:
			show_message("Something is already there. Pick an empty spot.")
		return true
	# A port: start laying a belt.
	for k in floor_view.footprints:
		var ports: Dictionary = floor_view.output_ports(k)
		for port in ports:
			if world.distance_to(ports[port]) <= _port_grab():
				if not _hand_belt:
					_select(k)  # (laying by hand, its settings would slide the view mid-drag)
				elif not _group.has(k):
					_clear_group()  # a press on a machine lets go of a picked room block
					_show_group()
				_drag = {"mode": "wire", "key": k, "port": port, "from": ports[port],
						"kind": "fail" if port == "fail" else "pass"}
				if _hand_belt:
					_drag["hand"] = true
					_drag["cells"] = [floor_view.out_cell(k, port)]
					floor_view.set_wire({"cells": _drag["cells"], "kind": _drag["kind"]})
				return true
	if _hand_belt and not Input.is_key_pressed(KEY_SHIFT):
		# Conveyor belt is on and the press is on (or by) an input: lay from it, back to an
		# output (the belt is turned round to run into the input when it's laid).
		for k in floor_view.footprints:
			var inputs: Dictionary = floor_view.input_ports(k)
			for kind in inputs:
				if world.distance_to(inputs[kind]) <= PORT_GRAB_HAND:
					var at := floor_view.in_cell(k, kind)
					if floor_view.route_at(at) >= 0:
						continue  # a belt already runs in there
					_drag = {"mode": "wire", "hand": true, "key": "", "port": "", "kind": "pass", "cells": [at]}
					floor_view.set_wire({"cells": _drag["cells"], "kind": "pass"})
					return true
	var hit := floor_view.node_at(world)
	var shift_held := Input.is_key_pressed(KEY_SHIFT)
	var cell := floor_view.cell_at(world)
	if hit.is_empty() and not shift_held and _room_cells.has(cell):
		# A picked room block (on its own or with others): a drag moves the whole group, a
		# click lets it go.
		_drag = {"mode": "press", "key": "", "screen": get_viewport().get_mouse_position(), "shift": false}
		_arm_group_move(world)
		return true
	if hit.is_empty() and not shift_held and not _hand_belt and _room_block_at(cell):
		# A wall, door, sign and so on: pick it on its own, and a drag moves it right away.
		_clear_group()
		_select("")
		_room_cells[cell] = true
		_refresh_ui()
		show_message("Room block picked. Drag it to move it, press Delete to remove it, or Esc (or a click on the floor) to let go.")
		_drag = {"mode": "press", "key": "", "screen": get_viewport().get_mouse_position(), "shift": false,
				"picked_now": true}
		_arm_group_move(world)
		return true
	if _hand_belt and hit.is_empty() and not shift_held:
		# Conveyor belt is on: lay belt from this cell. It becomes a link once it starts at an
		# output (or just in front of one) and ends at an input; see _finish_hand_belt.
		_drag = {"mode": "wire", "hand": true, "key": "", "port": "", "kind": "pass",
				"cells": [floor_view.cell_at(world)]}
		floor_view.set_wire({"cells": _drag["cells"], "kind": "pass"})
		return true
	if not hit.is_empty():
		if _picked_count() > 1 and _group.has(hit):
			pass  # one of the picked group: a drag moves them all
		elif not shift_held:
			_clear_group()
			_select(hit)
		# Only a move once the mouse travels a little: a plain click just opens the settings
		# (Shift+click adds it to the picked group, or takes it out).
		_drag = {"mode": "press", "key": hit, "offset": floor_view.cell_at(world) - floor_view.footprints[hit],
				"start": floor_view.footprints[hit], "screen": get_viewport().get_mouse_position(),
				"shift": shift_held}
		if _picked_count() > 1 and _group.has(hit):
			_arm_group_move(world)
		elif shift_held:
			_arm_line_move(_pid_of(hit), world)
		return true
	if shift_held:
		_drag = {"mode": "box", "from": world, "to": world}
		return true
	# A click on a door picks it on its own (Delete removes it, Esc lets go).
	var rooms := _rooms()
	if rooms and rooms.has_method("door_at") and rooms.door_at(floor_view.cell_at(world)):
		pick_room_cell(floor_view.cell_at(world))
		return true
	var route := floor_view.route_at(floor_view.cell_at(world))
	if route >= 0:
		var r: Dictionary = floor_view.routes[route]
		_clear_group()
		_select("", {"pid": r["pid"], "from": r["from"], "port": r["port"], "conv": str(r.get("conveyor", ""))})
		return true
	_clear_group()
	_select("")
	return true


func _release(world: Vector2) -> bool:
	var drag := _drag
	_drag = {}
	match str(drag.get("mode", "")):
		"press":
			if drag.get("shift", false):
				_toggle_in_group(str(drag["key"]))
			elif str(drag.get("key", "")).is_empty() and not drag.get("picked_now", false):
				# A click on an already picked room block: let go of the group.
				_clear_group()
				_select("")
				show_message("")
			return true  # a click: the settings are already open
		"box":
			floor_view.set_select_box(Rect2())
			var area := Rect2(drag["from"], Vector2.ZERO).expand(world)
			var added := 0
			for k in floor_view.footprints:
				var r := Rect2(Vector2(floor_view.footprints[k]) * FactoryFloor.CELL, Vector2(2, 2) * FactoryFloor.CELL)
				if area.intersects(r) and not _group.has(k):
					_group[k] = true
					added += 1
			var rooms := _rooms()
			if rooms and rooms.has_method("cells_in"):
				var cells := Rect2i(floor_view.cell_at(area.position), Vector2i.ONE) \
						.merge(Rect2i(floor_view.cell_at(area.end), Vector2i.ONE))
				for c in rooms.cells_in(cells):
					_room_cells[c] = true
			if _group.size() == 1 and _room_cells.is_empty():
				_select(str(_group.keys()[0]))
				_clear_group()
			elif _picked_count() > 0:
				_select("")
			_refresh_ui()
			show_message(_group_words())
			return true
		"group":
			floor_view.set_ghost({})
			var shift: Vector2i = floor_view.cell_at(world) - drag["grab"]
			if shift != Vector2i.ZERO and _group_fits(shift):
				note_edit()
				var steps := _history.size()
				_shift_group(shift)
				_rebuild()
				_push_group_step(steps)
			elif shift != Vector2i.ZERO:
				show_message("They would land on something else. Pick an empty spot.")
				_refresh_ui()
			else:
				_refresh_ui()
			return true
		"move":
			floor_view.set_ghost({})
			var tl: Vector2i = floor_view.cell_at(world) - drag["offset"]
			if tl != drag["start"] and floor_view.is_free(tl, drag["key"]):
				_node(drag["key"])["pos"] = [tl.x, tl.y]
				_rebuild()
				_relink_free_conveyors()  # moved onto a conveyor's end: it may link now
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
			if drag.get("hand", false) and not drag.get("loop", false):
				drag["cells"] = _snap_to_ports(drag["cells"])  # ended (or started) just by a port: onto it
			if drag.get("hand", false) and str(drag.get("key", "")).is_empty():
				_finish_hand_belt(drag, world)
			else:
				_finish_wire(drag, world)
			return true
	return false


## A belt laid by hand from open floor: find the output it starts at and the input it ends at
## (either way round), then link them along the cells laid.
## Dropped onto a machine (the belt stops beside it): it goes in at that machine's input.
func _finish_hand_belt(drag: Dictionary, world := Vector2.INF) -> void:
	var cells: Array = drag["cells"]
	var onto := floor_view.node_at(world) if world != Vector2.INF else ""
	for attempt in 2:
		var src := _output_at(cells[0], cells[1] if cells.size() > 1 else cells[0])
		var target := _input_at(cells[-1], src)
		var at := floor_view.cell_center(cells[-1])
		if target.is_empty() and attempt == 0 and not src.is_empty() and not onto.is_empty() \
				and onto != src[0] and _takes_from(onto, src) and _beside(onto, cells[-1]):
			target = onto
			at = world
		if not src.is_empty() and not target.is_empty():
			if cells[0] != floor_view.out_cell(src[0], src[1]):
				cells.push_front(floor_view.out_cell(src[0], src[1]))  # laid from inside the port
			var d := {"mode": "wire", "hand": true, "key": src[0], "port": src[1],
					"kind": "fail" if src[1] == "fail" else "pass", "target": target, "cells": cells}
			_finish_wire(d, at)
			return
		cells.reverse()  # laid from the input back to the output
	_lay_free_conveyor(drag)


## A belt laid by hand that starts or ends on the cell just beside a port's own cell (an
## output's or an input's, where its belt runs) is taken on onto it. Returns the cells.
func _snap_to_ports(cells: Array) -> Array:
	cells = cells.duplicate()
	if cells.is_empty():
		return cells
	for at_start in [true, false]:
		var end: Vector2i = cells[0] if at_start else cells[-1]
		if _port_cell(end):
			continue
		if not at_start and cells.size() == 1:
			break  # one cell: it was looked at as the start
		for d in FactoryFloor.DIRS.values():
			var n: Vector2i = end + d
			if cells.has(n) or floor_view.under_node(n) or floor_view.route_at(n) >= 0 or floor_view.is_wall(n):
				continue
			if _port_cell(n):
				if at_start:
					cells.push_front(n)
				else:
					cells.append(n)
				break
	return cells


## True if a belt runs from or into a port on cell c.
func _port_cell(c: Vector2i) -> bool:
	return not floor_view.output_at_cell(c).is_empty() or not floor_view.input_at_cell(c).is_empty()


## True if cell c is next to (or at a corner of) node key's footprint.
func _beside(key: String, c: Vector2i) -> bool:
	return Rect2i(floor_view.footprints[key] - Vector2i.ONE, Vector2i(4, 4)).has_point(c)


## Whether a belt from output src ([key, port]) can go into node key: a storage's only into an
## intake, anything else's into a machine, dock or scrap bin.
func _takes_from(key: String, src: Array) -> bool:
	if FactoryFloor.is_storage(key):
		return false
	if not src.is_empty() and FactoryFloor.is_storage(src[0]):
		return key.ends_with(":intake")
	return not key.ends_with(":intake")


## Conveyor cells as [[x, y], ...] for the draft.
static func _cell_pairs(cells: Array) -> Array:
	var out: Array = []
	for c in cells:
		out.append([c.x, c.y])
	return out


## The free conveyor whose open end is cell c: [pid, conveyor, at_start], or [].
func _conveyor_end_at(c: Vector2i, skip := {}) -> Array:
	for p in draft:
		for conv in FactoryFloor.conveyors_of(p):
			if conv == skip or conv.get("loop", false):
				continue
			var cs: Array = conv.get("cells", [])
			if cs.is_empty():
				continue
			if Vector2i(int(cs[0][0]), int(cs[0][1])) == c:
				return [str(p.get("id", "")), conv, true]
			if Vector2i(int(cs[-1][0]), int(cs[-1][1])) == c:
				return [str(p.get("id", "")), conv, false]
	return []


## Take a free conveyor off the draft.
func _drop_conveyor(pid: String, conv: Dictionary) -> void:
	var p := _pipeline(pid)
	p["conveyors"] = FactoryFloor.conveyors_of(p).filter(func(x) -> bool: return x != conv)
	if (p["conveyors"] as Array).is_empty():
		p.erase("conveyors")


## A belt laid by hand that doesn't link an output to an input: it stays on the floor as a free
## conveyor (a closed loop if it came back to where it started). Laid on from the open end of
## one already there, the two become one, and a link if that now runs output to input.
func _lay_free_conveyor(drag: Dictionary) -> void:
	var cells: Array = (drag["cells"] as Array).duplicate()
	var loop: bool = drag.get("loop", false)
	var joined: Array = []  # [pid, conveyor] taken into this one
	if not loop:
		# Conveyors whose end this one starts on (or next to), and whose start it ends on (or
		# next to), become one with it, as far as they go; not past a machine's port.
		var more := true
		while more:
			more = false
			if _input_at(cells[-1]).is_empty() or cells.size() < 2:
				var tail := _conveyor_touching(cells[-1], false, joined, cells)
				if not tail.is_empty():
					var old: Array = _cells_of_conv(tail[1])
					if old[0] == cells[-1]:
						old.pop_front()
					cells = cells + old
					joined.append(tail)
					more = true
			if _output_at(cells[0], cells[0]).is_empty():
				var head := _conveyor_touching(cells[0], true, joined, cells)
				if not head.is_empty():
					var old: Array = _cells_of_conv(head[1])
					if old[-1] == cells[0]:
						old.pop_back()
					cells = old + cells
					joined.append(head)
					more = true
	# It runs out of an output and into an input: laid the other way round, turn it.
	if not loop and cells.size() >= 2:
		var ends_out := not floor_view.output_at_cell(cells[-1]).is_empty()
		var starts_out := not floor_view.output_at_cell(cells[0]).is_empty()
		var starts_in := not floor_view.input_at_cell(cells[0]).is_empty()
		var ends_in := not floor_view.input_at_cell(cells[-1]).is_empty()
		if (ends_out and not starts_out) or (starts_in and not ends_in and not starts_out):
			cells.reverse()
	# Nothing doubled back over itself, and no machine under it.
	var seen := {}
	for c in cells:
		if seen.has(c) or floor_view.under_node(c):
			show_message("That belt runs over itself or under a machine. Lay it again round them.")
			return
		seen[c] = true
	# Ending on another belt (not at its start): this one runs into it from the side.
	var into := -1
	if not loop and cells.size() >= 2:
		var r_end := floor_view.route_at(cells[-1])
		if r_end >= 0 and not _is_joined(floor_view.routes[r_end], joined) \
				and ((floor_view.routes[r_end]["cells"] as Array).find(cells[-1]) > 0
					or (FactoryFloor.is_storage(str(floor_view.routes[r_end].get("from", ""))) and _end_name(floor_view.node_key(
						str(floor_view.routes[r_end]["pid"]), str(floor_view.routes[r_end].get("to", "")))) == "intake")):
			into = r_end
	# A run from an output to an input takes over the belt that output had: it may lie on it.
	var own := _link_ends(cells)
	var own_src: Array = own[0] if not own.is_empty() else []
	if into >= 0 and _leaves(floor_view.routes[into], own_src):
		into = -1
	for i in cells.size():
		if i == cells.size() - 1 and into >= 0:
			continue
		var r := floor_view.route_at(cells[i])
		if r >= 0 and not _is_joined(floor_view.routes[r], joined) and not _leaves(floor_view.routes[r], own_src) \
				and not floor_view.can_cross(cells, i):
			show_message("Belts can't branch off another one: a Splitter does that. Start on open floor, a machine's output or the end of a conveyor." if i == 0
					else CROSS_HINT)
			return
	if cells.size() < 2:
		show_message("Drag across the floor to lay a conveyor. Start on a machine's output and end at an input to link them.")
		return
	var onto_link := into >= 0 and not str(floor_view.routes[into].get("from", "")).is_empty()
	if onto_link and not _output_at(cells[0], cells[1]).is_empty():
		# Into a belt that carries crates: from a machine's output, it's a link to where that
		# belt goes, joining it. (Not from an output, it stays a free conveyor joined to its side.)
		var src := _output_at(cells[0], cells[1])
		if not _join_link(src, cells, floor_view.routes[into]):
			return
		for j in joined:
			_drop_conveyor(j[0], j[1])
		_rebuild()
		var b := _find_belt({"pid": _pid_of(src[0]), "from": _end_name(src[0]), "port": src[1]})
		show_message("Joined: crates run on along that belt." if b.get("manual", false)
				else "That join didn't fit (it has to meet a straight bit of that belt, the same colour), so the belt found its own way.")
		return
	for j in joined:
		_drop_conveyor(j[0], j[1])
	# A run from an output to an input (maybe joined up from pieces): that's a link.
	if not loop:
		for attempt in 2:
			var src := _output_at(cells[0], cells[1])
			var target := _input_at(cells[-1], src)
			if not src.is_empty() and not target.is_empty():
				var d := {"mode": "wire", "hand": true, "key": src[0], "port": src[1],
						"kind": "fail" if src[1] == "fail" else "pass", "target": target, "cells": cells}
				_finish_wire(d, floor_view.cell_center(cells[-1]))
				return
			cells.reverse()
		# Running past a machine's output on the way to an input: the output feeds onto it there.
		# From that port on it's a link; the part laid before it stays a free conveyor that runs
		# into the link's start.
		var passed := _output_passed(cells) if not onto_link else []
		if not passed.is_empty():
			var i: int = passed[0]
			var src: Array = passed[1]
			var rest: Array = cells.slice(i)
			var target := _input_at(rest[-1], src)
			if rest.size() >= 2 and not target.is_empty() and target != src[0]:
				var feed_pid := str(joined[0][0]) if not joined.is_empty() else _pid_of(src[0])
				var feed := {"id": _new_conveyor_id(), "cells": _cell_pairs(cells.slice(0, i + 1)), "feeds": true}
				var fp := _pipeline(feed_pid)
				var flist: Array = FactoryFloor.conveyors_of(fp)
				flist.append(feed)
				fp["conveyors"] = flist
				var d := {"mode": "wire", "hand": true, "key": src[0], "port": src[1],
						"kind": "fail" if src[1] == "fail" else "pass", "target": target, "cells": rest}
				_finish_wire(d, floor_view.cell_center(rest[-1]))
				return
	var pid := current if not _pipeline(current).is_empty() else ""
	if not joined.is_empty():
		pid = str(joined[0][0])
	else:
		var src := _output_at(cells[0], cells[1])
		if not src.is_empty():
			pid = _pid_of(src[0])  # laid from a machine's output: it's that line's
	if pid.is_empty():
		_new_pipeline(Vector2i(-1, -1), false)  # nothing on the floor yet: a line to keep it on
		pid = current
	var p := _pipeline(pid)
	var conv := {"id": _new_conveyor_id(), "cells": _cell_pairs(cells)}
	if loop:
		conv["loop"] = true
	if onto_link:
		conv["onto_link"] = true  # runs into the side of a belt carrying crates: laid after the links
	elif into >= 0:
		conv["join"] = true  # runs into another conveyor's side: laid after it
	var list: Array = FactoryFloor.conveyors_of(p)
	list.append(conv)
	p["conveyors"] = list
	_rebuild()
	var from_port := _output_at(cells[0], cells[1])
	var into_port := _input_at(cells[-1])
	var passed := _output_passed(cells, loop)
	if loop and not passed.is_empty():
		show_message("Loop laid past %s's output: the port is on it, but a loop goes nowhere, so nothing rides it. Run a belt from that output into %s to link them."
				% [_label_of(_end_name(passed[1][0])), _goes_into_words(passed[1])])
	elif loop:
		show_message("Loop laid: it runs round and round.")
	elif not from_port.is_empty():
		show_message("Conveyor attached to %s's output. Carry it on into %s to link them (it starts moving then)."
				% [_label_of(_end_name(from_port[0])), _goes_into_words(from_port)])
	elif not passed.is_empty():
		show_message("Conveyor runs past %s's output, so the port feeds onto it. Carry it on into %s to link them."
				% [_label_of(_end_name(passed[1][0])), _goes_into_words(passed[1])])
	elif onto_link:
		show_message("Conveyor joined to that belt's side. Lay on from a machine's output onto its start (or place a machine there) and crates run on along it.")
	elif not into_port.is_empty():
		show_message("Conveyor attached to %s's input. Run it back to a machine's output (or lay one from an output onto its start) to link them." % _label_of(_end_name(into_port)))
	else:
		show_message("Conveyor laid. Lay on from its end to make it longer, or start on a machine's output and end at an input to link them.")


## Where a belt from output src can go, in words.
func _goes_into_words(src: Array) -> String:
	return "an intake's input" if FactoryFloor.is_storage(src[0]) else "a machine's input (or a dock or scrap bin)"


## A fresh id for a free conveyor.
func _new_conveyor_id() -> String:
	var ids := {}
	for q in draft:
		for conv in FactoryFloor.conveyors_of(q):
			ids[str(conv.get("id", ""))] = true
	var n := 1
	while ids.has("conv_%d" % n):
		n += 1
	return "conv_%d" % n


## The first output (with no belt of its own yet) that a run of cells passes over on the way,
## not at its first cell: [index, [key, port]], or []. With loop, any cell.
func _output_passed(cells: Array, loop := false) -> Array:
	for i in range(0 if loop else 1, cells.size()):
		var src := floor_view.output_at_cell(cells[i])
		if src.is_empty():
			continue
		if not _find_belt({"pid": _pid_of(src[0]), "from": _end_name(src[0]), "port": src[1]}).is_empty():
			continue  # that port has its own belt already
		return [i, src]
	return []


## Free conveyors that now run from an output into an input (a machine was placed or moved
## onto their ends) become links.
func _relink_free_conveyors() -> void:
	for p in draft:
		for conv in FactoryFloor.conveyors_of(p):
			if conv.get("loop", false) or conv.get("feeds", false):
				continue
			var cells := _cells_of_conv(conv)
			if cells.size() < 2:
				continue
			if conv.get("onto_link", false):
				# Joined to a link's side: now running from an output, it joins that link.
				var src := _output_at(cells[0], cells[1])
				var r_end := floor_view.route_at(cells[-1])
				if not src.is_empty() and r_end >= 0 and not str(floor_view.routes[r_end].get("from", "")).is_empty() \
						and _find_belt({"pid": _pid_of(src[0]), "from": _end_name(src[0]), "port": src[1]}).is_empty() \
						and _join_link(src, cells, floor_view.routes[r_end]):
					_drop_conveyor(str(p.get("id", "")), conv)
					_rebuild()
					return  # one at a time: the draft just changed
				continue
			for attempt in 2:
				var src := _output_at(cells[0], cells[1])
				var target := _input_at(cells[-1], src)
				if not src.is_empty() and not target.is_empty() and target != src[0] \
						and _find_belt({"pid": _pid_of(src[0]), "from": _end_name(src[0]), "port": src[1]}).is_empty():
					_drop_conveyor(str(p.get("id", "")), conv)
					var d := {"mode": "wire", "hand": true, "key": src[0], "port": src[1],
							"kind": "fail" if src[1] == "fail" else "pass", "target": target, "cells": cells}
					_finish_wire(d, floor_view.cell_center(cells[-1]))
					return  # one at a time: the draft just changed
				cells.reverse()


## The free conveyor (not a loop, not one already taken) whose last cell (at_end) or first cell
## is c, or failing that is next to it without doubling back over `cells`: [pid, conveyor], or [].
func _conveyor_touching(c: Vector2i, at_end: bool, joined: Array, cells: Array) -> Array:
	var near: Array = []
	for p in draft:
		for conv in FactoryFloor.conveyors_of(p):
			if conv.get("loop", false) or _is_joined({"belt": conv}, joined):
				continue
			var cs := _cells_of_conv(conv)
			if cs.is_empty():
				continue
			if (not at_end and not _output_at(cs[0], cs[0]).is_empty()) or (at_end and not _input_at(cs[-1]).is_empty()):
				continue  # its other end is on a machine's port: nothing runs on past that
			var end: Vector2i = cs[-1] if at_end else cs[0]
			if end == c:
				return [str(p.get("id", "")), conv]
			if near.is_empty() and (end - c).length_squared() == 1:
				var clash := false
				for x in cs:
					if cells.has(x):
						clash = true
				if not clash:
					near = [str(p.get("id", "")), conv]
	return near


## The output and input a run of cells links, either way round: [[key, port], target], or [].
func _link_ends(cells: Array) -> Array:
	if cells.size() < 2:
		return []
	for ends in [[cells[0], cells[1], cells[-1]], [cells[-1], cells[-2], cells[0]]]:
		var src := _output_at(ends[0], ends[1])
		var target := _input_at(ends[2], src)
		if not src.is_empty() and not target.is_empty():
			return [src, target]
	return []


## True if the route is the belt that leaves output src ([key, port]).
func _leaves(route: Dictionary, src: Array) -> bool:
	return not src.is_empty() and str(route.get("from", "")) == _end_name(src[0]) \
			and str(route.get("port", "")) == str(src[1]) and str(route.get("pid", "")) == _pid_of(src[0])


func _is_joined(route: Dictionary, joined: Array) -> bool:
	for j in joined:
		if route.get("belt") == j[1]:
			return true
	return false


## A belt laid by hand from output src ([key, port]) that ends on the side of link `into`:
## a link to the same place, joining it there. False (with a message) if it can't be.
func _join_link(src: Array, cells: Array, into: Dictionary) -> bool:
	var pid := _pid_of(src[0])
	var target := floor_view.node_key(str(into["pid"]), str(into["to"]))
	if target == src[0]:
		show_message("That belt goes back into the machine this one starts at.")
		return false
	var kind := "fail" if src[1] == "fail" else "pass"
	if str(into["kind"]) != kind and not floor_view.is_merger(target):
		show_message("A red belt can only join a red one, and a green belt a green one.")
		return false
	if FactoryFloor.is_storage(src[0]) and _end_name(target) == "intake":
		# Another storage's belt into an intake: this one merges onto it, so several storages
		# (Jira issues, GitHub issues, ...) feed the same intake.
		_wire_storage(src[0], target, _cell_pairs(cells), true)
		return not _find_belt({"pid": _pid_of(target), "from": _end_name(src[0]), "port": "out"}).is_empty()  # empty: it said why it couldn't (same source and filter twice)
	if FactoryFloor.is_storage(src[0]) or _end_name(target) == "intake" or FactoryFloor.is_storage(target):
		show_message("A storage's belt goes into an intake (or joins another storage's belt into one); machines' belts don't go into intakes.")
		return false
	_adopt(pid, target)
	var to := _belt_target(pid, target)
	_connect(pid, _end_name(src[0]), src[1], to, _cell_pairs(cells))
	var b := _find_belt({"pid": pid, "from": _end_name(src[0]), "port": src[1]})
	if b.get("manual", false):
		b["join"] = true  # routed after the belt it joins
	return true


func _cells_of_conv(conv: Dictionary) -> Array:
	var out: Array = []
	for c in conv.get("cells", []):
		out.append(Vector2i(int(c[0]), int(c[1])))
	return out


## The output a hand-laid belt starting on cell c leaves by: [footprint key, port], or [].
func _output_at(c: Vector2i, _next: Vector2i) -> Array:
	for k in floor_view.footprints:
		for port in floor_view.output_ports(k):
			if floor_view.out_cell(k, port) == c:
				return [k, port]
	return []


## What a hand-laid belt ending on cell c runs into: the footprint key of a machine, dock or bin
## whose input cell (or footprint) it is, or "".
## src: the output it comes from ([key, port]): a storage's goes only into an intake, any
## other only into a machine, dock or bin. With none given, any of them.
func _input_at(c: Vector2i, src := []) -> String:
	for k in floor_view.footprints:
		if FactoryFloor.is_storage(k):
			continue
		if k.ends_with(":intake"):
			if not src.is_empty() and not FactoryFloor.is_storage(src[0]):
				continue
			if floor_view.in_cell(k, "pass") == c or Rect2i(floor_view.footprints[k], Vector2i(2, 2)).has_point(c):
				return k
			continue
		if not src.is_empty() and FactoryFloor.is_storage(src[0]):
			continue
		if floor_view.in_cell(k, "pass") == c or floor_view.in_cell(k, "fail") == c \
				or Rect2i(floor_view.footprints[k], Vector2i(2, 2)).has_point(c):
			return k
	return ""


# Picking several -------------------------------------------------------------

func _toggle_in_group(key: String) -> void:
	if _group.is_empty() and not _room_cells.is_empty():
		# Room blocks picked on their own (a click on a wall, door or sign): Shift+click on a
		# machine lets them go rather than picking both. A Shift+box still takes both together.
		_room_cells.clear()
	if _group.is_empty() and _sel_key != "" and _sel_key != key and floor_view.footprints.has(_sel_key):
		_group[_sel_key] = true  # what was open joins the group
	if _group.has(key):
		_group.erase(key)
	else:
		_group[key] = true
	if _group.size() <= 1 and _room_cells.is_empty():
		var only := "" if _group.is_empty() else str(_group.keys()[0])
		_clear_group()
		_select(only)
	else:
		_select("")
	show_message(_group_words())


func _group_words() -> String:
	if _picked_count() <= 1:
		return ""
	var what := "%d picked" % _group.size() if _room_cells.is_empty() else \
			"%d picked, with %d room block%s" % [_group.size(), _room_cells.size(), "" if _room_cells.size() == 1 else "s"]
	return "%s. Drag any of them to move them all, or press Delete to remove them. Shift+click adds or takes one out; Esc lets go." % what


## Pick one room block on its own, such as a door you clicked: Delete then removes it.
func pick_room_cell(c: Vector2i) -> void:
	_clear_group()
	_select("")
	_room_cells[c] = true
	_refresh_ui()
	show_message("Door picked. Drag it to move it, press Delete to remove it, or Esc (or a click on the floor) to let go.")


func _clear_group() -> void:
	_group.clear()
	_room_cells.clear()


## Machines, stations and room blocks picked together.
func _picked_count() -> int:
	return _group.size() + _room_cells.size()


## The room layer (room_layer.gd), if there is one: main.gd hands us its undo.
func _rooms() -> Object:
	return rooms_undo.get_object() if rooms_undo.is_valid() else null


func _show_group() -> void:
	if floor_view:
		floor_view.set_group(_group if visible else {}, _room_cells if visible else {})


func _arm_group_move(world: Vector2) -> void:
	var box := Rect2i()
	var first := true
	for k in _group:
		if not floor_view.footprints.has(k):
			continue
		var r := Rect2i(floor_view.footprints[k], Vector2i(2, 2))
		box = r if first else box.merge(r)
		first = false
	for c in _room_cells:
		box = Rect2i(c, Vector2i.ONE) if first else box.merge(Rect2i(c, Vector2i.ONE))
		first = false
	_drag["group_move"] = true
	_drag["grab"] = floor_view.cell_at(world)
	_drag["box"] = box


## Whether the group could sit `shift` cells away without landing on anything else.
func _group_fits(shift: Vector2i) -> bool:
	var others := {}
	for k in floor_view.footprints:
		if not _group.has(k):
			for c in _cells2(floor_view.footprints[k]):
				others[c] = true
	var landing := others.duplicate()
	for k in _group:
		if floor_view.footprints.has(k):
			for c in _cells2(floor_view.footprints[k] + shift):
				if others.has(c) or (floor_view.room_walls.has(c) and not _room_cells.has(c)):
					return false
				landing[c] = true
	var rooms := _rooms()
	if not _room_cells.is_empty() and rooms and rooms.has_method("can_shift"):
		# Only machines left behind count: the picked ones move with the blocks (a room's floor
		# under its own machine is fine), so `others`, not `landing`.
		return rooms.can_shift(_room_cells.keys(), shift, others)
	return true


static func _cells2(tl: Vector2i) -> Array:
	return [tl, tl + Vector2i(1, 0), tl + Vector2i(0, 1), tl + Vector2i(1, 1)]


## A belt's ends as footprint keys.
func _belt_ends(pid: String, b: Dictionary) -> Array:
	var from := str(b.get("from", ""))
	var to := str(b.get("to", ""))
	var from_key := "%s:%s" % [pid, from] if from == "intake" or FactoryFloor.is_storage(from) else from
	var to_key := to
	if not ":" in to and (to in FactoryFloor.STATIONS or FactoryFloor.is_bin(to) or FactoryFloor.is_dock(to)):
		to_key = "%s:%s" % [pid, to]
	return [from_key, to_key]


## Move everything picked by `shift` cells. Belts between two picked things keep their shape;
## the floor routes the others again.
func _shift_group(shift: Vector2i) -> void:
	var rooms := _rooms()
	if not _room_cells.is_empty() and rooms and rooms.has_method("shift_cells"):
		# The blocks move too, as part of the same undo step (see _push_group_step).
		rooms.shift_cells(_room_cells.keys(), shift)
		var moved := {}
		for c in _room_cells:
			moved[c + shift] = true
		_room_cells = moved
		_pending_rooms_step = true
	for k in _group:
		var place := _node(k)
		var pos = place.get("pos")
		if pos is Array and pos.size() == 2:
			place["pos"] = [int(pos[0]) + shift.x, int(pos[1]) + shift.y]
	for p in draft:
		var pid := str(p.get("id", ""))
		for b in p.get("belts", []):
			var ends := _belt_ends(pid, b)
			var path = b.get("path")
			if not path is Array:
				continue
			if _group.has(ends[0]) and _group.has(ends[1]):
				var moved: Array = []
				for c in path:
					moved.append([int(c[0]) + shift.x, int(c[1]) + shift.y])
				b["path"] = moved
			elif _group.has(ends[0]) or _group.has(ends[1]):
				b.erase("path")


## One undo step for a group edit that moved or removed room blocks as well as machines: the
## draft as it was, plus the room layer's own step.
## steps: the history's size before the edit (the draft's own step, if any, came after it).
func _push_group_step(steps: int) -> void:
	if not _pending_rooms_step:
		return
	_pending_rooms_step = false
	if _history.size() > steps and str(_history[-1].get("who", "")) == "build":
		_history[-1]["rooms"] = true  # the machines' step undoes the blocks too
		return
	_push({"who": "build", "draft": _snapshot(), "current": current, "rooms": true})
	_last = _snapshot()


## Delete removes every picked machine, bin, storage and dock (an intake goes with its line),
## and the room blocks picked with them.
func _remove_group() -> void:
	var keys := _group.keys()
	var skipped := 0
	var blocks := _room_cells.keys()
	note_edit()  # settings typed before it are their own step
	var steps := _history.size()
	_clear_group()
	var rooms := _rooms()
	if not blocks.is_empty() and rooms and rooms.has_method("erase_cells"):
		rooms.erase_cells(blocks)
		_pending_rooms_step = true
	_restoring = true  # one undo step for all of them
	for k in keys:
		if not floor_view.footprints.has(k) and _node(k).is_empty():
			continue
		if ":" in k and _end_name(k) == "intake":
			skipped += 1
			continue
		_sel_key = k
		_sel_belt = {}
		_remove_selected()
	_sel_key = ""
	_restoring = false
	note_edit()
	_push_group_step(steps)
	_refresh_ui()
	show_message("Removed %d. %s" % [keys.size() - skipped + blocks.size(),
			"Intakes stay: delete a whole line from the Lines list." if skipped > 0 else "Ctrl+Z brings them back."])


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
	places.append_array(p.get("docks", []))
	places.append_array(FactoryFloor.storages_of(p))
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
	for conv in FactoryFloor.conveyors_of(p):
		var moved: Array = []
		for c in conv.get("cells", []):
			moved.append([int(c[0]) + delta.x, int(c[1]) + delta.y])
		conv["cells"] = moved


func _finish_wire(drag: Dictionary, world: Vector2) -> void:
	# A run already found to end at an input names it: a machine turned round has its input on
	# the far side, where the point in front of it isn't on the machine.
	var target := str(drag.get("target", ""))
	if target.is_empty():
		target = floor_view.node_at(world)
	if target.is_empty():
		# Dropped just in front of a machine's input: count it.
		target = floor_view.node_at(world + Vector2(FactoryFloor.CELL * 0.5, 0))
	if target.is_empty() or target == drag["key"]:
		if drag.get("hand", false) and target.is_empty() and not drag.has("target"):
			_lay_free_conveyor({"cells": drag["cells"]})  # from an output to open floor: lay it there
		return
	var pid := _pid_of(drag["key"])
	if FactoryFloor.is_storage(drag["key"]):
		_wire_storage(drag["key"], target, _hand_path(drag, target) if drag.get("hand", false) else [])
		return
	if FactoryFloor.is_storage(target):
		show_message("Nothing goes into a storage: drag from its port into an intake instead.")
		return
	if _end_name(target) == "intake":
		show_message("Belts go into machines, the dock or the scrap bin.")
		return
	var adopted := _adopt(pid, target)
	var to := _belt_target(pid, target)
	var laid: Array = _hand_path(drag, target) if drag.get("hand", false) else []
	_connect(pid, _end_name(drag["key"]), drag["port"], to, laid)
	_rebuild()
	if not laid.is_empty():
		var b := _find_belt({"pid": pid, "from": _end_name(drag["key"]), "port": drag["port"]})
		if b.get("manual", false):
			show_message("Linked %s → %s: crates ride this belt now." % [_label_of(_end_name(drag["key"])), _label_of(_end_name(target))])
		else:
			show_message("That way is blocked, so the belt found its own way. " + CROSS_HINT)
		return
	show_message("" if adopted or _pid_of(target) == pid else
			"Joined to %s: crates from this line carry on along it." % str(_pipeline(_pid_of(target)).get("name", "")))


func _on_belt_tile_toggled(on: bool) -> void:
	_hand_belt = on
	_placing = ""
	floor_view.set_ghost({})
	_refresh_ui()


## Laying a belt by hand: the mouse is over cell c. Fills in any cells skipped (a fast mouse)
## in straight lines, and going back over the last cell takes it up again.
func _lay_cell(c: Vector2i) -> void:
	var cells: Array = _drag["cells"]
	var last: Vector2i = cells[-1]
	if _drag.get("loop", false):
		if c == cells[-1] or c == cells[0]:
			return
		_drag.erase("loop")  # pulled back out of the closed loop: it opens again
	while last != c:
		var step := Vector2i(signi(c.x - last.x), 0) if last.x != c.x else Vector2i(0, signi(c.y - last.y))
		var next := last + step
		if cells.size() > 1 and cells[-2] == next:
			cells.pop_back()  # back the way it came
		elif next == cells[0] and cells.size() >= 4 and str(_drag.get("key", "")).is_empty():
			_drag["loop"] = true  # back to where it started: a closed loop
			return
		elif cells.has(next) or floor_view.under_node(next):
			return  # never loop over itself, or run under a machine
		else:
			cells.append(next)
		last = next


## The cells laid by hand, from the output port's cell to the cell in front of the target's
## input (filled in straight, then along, if it was dropped short of it or onto the machine).
func _hand_path(drag: Dictionary, target: String) -> Array:
	var goal := floor_view.in_cell(target, drag["kind"])
	var drawn: Array = drag.get("cells", [])
	if not drawn.is_empty():
		var lower := floor_view.in_cell(target, "fail")
		var upper := floor_view.in_cell(target, "pass")
		var at_lower := drawn.rfind(lower)
		var at_upper := drawn.rfind(upper)
		if at_lower >= 0 or at_upper >= 0:
			# Laid onto an input: that's the one it goes in at, whichever (even if it ran past
			# the other on the way, say round the machine).
			goal = lower if at_lower > at_upper else upper
		elif floor_view.is_merger(target):
			# Either input of a belt merger: the one nearer where it was laid to.
			var end: Vector2i = drawn[-1]
			goal = lower if (end - lower).length_squared() < (end - upper).length_squared() else upper
	var drawn_to: Array = drag["cells"]
	if not drawn_to.has(goal):
		# Dropped onto the machine rather than on its input: cut off the bit laid past where the
		# belt came nearest the input, so it goes straight in instead of doubling back.
		var best := -1
		for i in drawn_to.size():
			var d: int = absi(drawn_to[i].x - goal.x) + absi(drawn_to[i].y - goal.y)
			if best < 0 or d < absi(drawn_to[best].x - goal.x) + absi(drawn_to[best].y - goal.y):
				best = i
		drawn_to = drawn_to.slice(0, best + 1)
	var cells: Array = []
	for c in drawn_to:
		if c == goal:
			cells.append(c)
			break
		if floor_view.node_at(Vector2(c) * FactoryFloor.CELL + Vector2.ONE * FactoryFloor.CELL * 0.5) == target:
			break  # onto the machine itself: it goes in at its input from here
		cells.append(c)
	if cells.is_empty():
		return []
	var last: Vector2i = cells[-1]
	while last != goal:
		var step := Vector2i(signi(goal.x - last.x), 0) if last.x != goal.x else Vector2i(0, signi(goal.y - last.y))
		last += step
		if cells.has(last):
			return []
		cells.append(last)
	var out: Array = []
	for c in cells:
		out.append([c.x, c.y])
	return out


## A machine placed while another line was picked, belted into for the first time: it joins
## the line the belt comes from (with its own belts out), so it isn't stranded on a line whose
## intake never reaches it. Returns whether it moved.
func _adopt(pid: String, target: String) -> bool:
	if ":" in target:
		return false
	var from_pid := _pid_of(target)
	if from_pid.is_empty() or from_pid == pid:
		return false
	for q in draft:
		for b in q.get("belts", []):
			if str(b.get("to", "")) == target:
				return false
	var src := _pipeline(from_pid)
	var dst := _pipeline(pid)
	var m := _machine(target)
	if src.is_empty() or dst.is_empty() or m.is_empty():
		return false
	# A line with no intake (machines placed before any line) comes along whole from here: the
	# machines its belts lead on to as well.
	var going := {target: true}
	if not src.get("intake") is Dictionary:
		var more := true
		while more:
			more = false
			for b in src.get("belts", []):
				var to := str(b.get("to", ""))
				if going.has(str(b.get("from", ""))) and not going.has(to) and not _machine(to).is_empty() \
						and _pid_of(to) == from_pid:
					going[to] = true
					more = true
	var list: Array = dst.get("machines", [])
	for x in (src.get("machines", []) as Array).duplicate():
		if going.has(str(x.get("id", ""))):
			list.append(x)
	dst["machines"] = list
	src["machines"] = (src.get("machines", []) as Array).filter(func(x) -> bool: return not going.has(str(x.get("id", ""))))
	var keep: Array = []
	var moved: Array = dst.get("belts", [])
	for b in src.get("belts", []):
		if not going.has(str(b.get("from", ""))):
			keep.append(b)
			continue
		var to := str(b.get("to", ""))
		if not ":" in to and (to in FactoryFloor.STATIONS or FactoryFloor.is_bin(to) or FactoryFloor.is_dock(to)):
			to = "%s:%s" % [from_pid, to]  # the old line's dock or bin, named in full
		if to.begins_with(pid + ":"):
			to = to.get_slice(":", 1)
		b["to"] = to
		moved.append(b)
	src["belts"] = keep
	dst["belts"] = moved
	_drop_if_empty(from_pid)
	return true


## A belt from a storage into an intake. The storage moves to that intake's line (a storage
## feeds one intake), and a line takes each source from one storage.
func _wire_storage(key: String, target: String, laid: Array = [], join := false) -> void:
	if _end_name(target) != "intake":
		show_message("A storage's belt goes into an intake.")
		return
	var from_line := _pipeline(_pid_of(key))
	var to_pid := _pid_of(target)
	var to_line := _pipeline(to_pid)
	var sid := _end_name(key)
	var st := _node(key)
	for other in FactoryFloor.fed_storages(to_line):
		if other != st and str(other.get("source", "")) == str(st.get("source", "")) \
				and github_name(str(other.get("repo", ""))) == github_name(str(st.get("repo", ""))) \
				and str(other.get("query", "")).strip_edges().to_lower() == str(st.get("query", "")).strip_edges().to_lower():
			show_message("%s already takes %s from another storage with the same repo and filter. Give this one another repo or filter, or remove that one."
					% [str(to_line.get("name", to_pid)), str(STORE_WORDS.get(str(st.get("source", "")), ""))])
			return
	from_line["belts"] = (from_line.get("belts", []) as Array).filter(func(b) -> bool: return str(b.get("from", "")) != sid)
	if from_line != to_line:
		from_line["storages"] = FactoryFloor.storages_of(from_line).filter(func(s2) -> bool: return s2 != st)
		var stores: Array = FactoryFloor.storages_of(to_line)
		stores.append(st)
		to_line["storages"] = stores
		if not from_line.get("intake") is Dictionary and FactoryFloor.storages_of(from_line).is_empty() \
				and (from_line.get("machines", []) as Array).is_empty():
			draft.erase(from_line)  # it only held storages waiting for an intake
	if not to_line.has("sources") or not to_line["sources"] is Array:
		to_line["sources"] = []
	_connect(to_pid, sid, "out", "intake", laid)
	if join and not laid.is_empty():
		_find_belt({"pid": to_pid, "from": sid, "port": "out"})["join"] = true  # routed after the belt it joins
	_sel_key = "%s:%s" % [to_pid, sid]
	current = to_pid
	_rebuild()
	show_message("Linked storage → %s intake: it now takes %s from this storage." % [str(to_line.get("name", to_pid)),
			str(STORE_WORDS.get(str(st.get("source", "")), ""))])


# Settings ---------------------------------------------------------------------

func _on_name_changed(text: String) -> void:
	if _loading:
		return
	_machine(_sel_key)["name"] = text
	_title.text = text
	floor_view.set_pipelines(draft)


func _on_switch_toggled(on: bool) -> void:
	if _loading:
		return
	var m := _machine(_sel_key)
	if m.is_empty():
		return
	if on:
		m.erase("enabled")
	else:
		m["enabled"] = false
	floor_view.set_pipelines(draft)
	_refresh_ui()


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


func _on_retries_changed(value: float) -> void:
	if _loading:
		return
	var m := _machine(_sel_key)
	if int(value) > 0:
		m["retries"] = int(value)
	else:
		m.erase("retries")


## The factory-wide Claude default and the pickers' lists (snapshot or "agent_defaults" message).
func set_agent_settings(defaults: Dictionary, choices: Dictionary = {}) -> void:
	agent_defaults = {"model": str(defaults.get("model", "")), "effort": str(defaults.get("effort", ""))}
	if not choices.is_empty():
		agent_choices = choices.duplicate(true)
	if _agent_box != null and _agent_box.visible and not _machine(_sel_key).is_empty():
		_loading = true
		_fill_agent_picker(_model_picker, "model", _machine(_sel_key))
		_fill_agent_picker(_effort_picker, "effort", _machine(_sel_key))
		_loading = false


## A machine's model or effort picker: Default (and what that is now), then each choice.
func _fill_agent_picker(pick: OptionButton, field: String, machine: Dictionary) -> void:
	pick.clear()
	var fallback := str(agent_defaults.get(field, ""))
	pick.add_item("Default (%s)" % (fallback if not fallback.is_empty() else "Claude Code's own"))
	pick.set_item_metadata(0, "")
	var values: Array = agent_choices.get("models" if field == "model" else "efforts", [])
	var current := str(machine.get(field, "")) if machine.get(field) != null else ""
	if not current.is_empty() and not values.has(current):
		values = values + [current]  # kept from an older list
	pick.select(0)
	for v in values:
		pick.add_item(str(v))
		pick.set_item_metadata(pick.item_count - 1, str(v))
		if str(v) == current:
			pick.select(pick.item_count - 1)


func _on_agent_picked(field: String, pick: OptionButton, index: int) -> void:
	if _loading:
		return
	var m := _machine(_sel_key)
	var value := str(pick.get_item_metadata(index))
	if value.is_empty():
		m.erase(field)
	else:
		m[field] = value


func _on_prompt_changed() -> void:
	if _loading:
		return
	_machine(_sel_key)["prompt"] = _prompt_input.text


## Earlier machines on the line (by the pass belts) that a failure here can go back to.
func _fill_fail_info(id: String) -> void:
	var current_target := floor_view.on_fail(id)
	if current_target.is_empty():
		_fail_label.text = "Failed crates leave by the red port. No belt is connected to it, so they wait here for you. Drag from the red port to lay one."
	elif FactoryFloor.is_bin(current_target):
		var place := _end_name(current_target)
		_fail_label.text = "Failed crates leave by the red port, along its belt into %s." % (
				"the scrap bin" if place == "scrap" else "scrap bin %s" % place.trim_prefix("scrap_"))
	elif _is_dock(current_target):
		_fail_label.text = "Failed crates leave by the red port, along its belt to the shipping dock."
	elif int(floor_view.depth.get(current_target, 0)) <= int(floor_view.depth.get(id, 0)):
		_fail_label.text = "Failed crates leave by the red port, along its belt back to %s for rework." % _label_of(current_target)
	else:
		_fail_label.text = "Failed crates leave by the red port, along its belt on to %s, and carry on from there." % _label_of(current_target)
	_scrap_box.visible = FactoryFloor.is_bin(current_target)
	var when = _machine(id).get("scrap_when", null)
	for kind in SCRAP_KINDS:
		(_scrap_checks[kind] as CheckBox).set_pressed_no_signal(when == null or kind in when)
	(_scrap_checks["rejected"] as CheckBox).visible = FactoryFloor.gated(_machine(id),
			catalog.get(str(_machine(id).get("type", "")), {}))


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
				"result":
					value = "fail"
				"repo":
					var names := _known_repo_names()
					value = str(names[0]) if not names.is_empty() else ""
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
			"repo":
				_fill_repo_ticks(port, value)
			"result":
				_fill_result_rule(port, value)
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


## A sorter's "Arrived as" rule: passed (it came off a green belt) or failed (a red belt), and
## for failed, optionally which machines it failed at. Saved as "pass", "fail" or "fail:Tester, Coder".
func _fill_result_rule(port: String, value: String) -> void:
	var parts := value.split(":", true, 1)
	var failed := parts[0].strip_edges() == "fail"
	var at := parts[1].strip_edges() if parts.size() > 1 else ""
	var pick := OptionButton.new()
	pick.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	pick.add_item("Passed (green belt)")
	pick.add_item("Failed (red belt)")
	pick.select(1 if failed else 0)
	_rules_box.add_child(pick)
	var edit := LineEdit.new()
	edit.text = at
	edit.placeholder_text = "failed at (optional): e.g. Tester, Reviewer"
	edit.visible = failed
	_rules_box.add_child(edit)
	var save := func() -> void:
		var v := "fail" if pick.selected == 1 else "pass"
		if pick.selected == 1 and not edit.text.strip_edges().is_empty():
			v += ":" + edit.text.strip_edges()
		_set_rule(port, "result", v)
	pick.item_selected.connect(func(index: int) -> void:
		edit.visible = index == 1
		save.call()
		_fit_settings_window.call_deferred())
	edit.text_changed.connect(func(_text: String) -> void: save.call())


## A sorter's "Repo is" rule: a tick box for each repo on the factory's list (any ticked one
## matches). Repos the rule names that aren't on the list any more still show, ticked.
func _fill_repo_ticks(port: String, value: String) -> void:
	var ticked: Array = []
	for w in value.split(","):
		if not w.strip_edges().is_empty():
			ticked.append(w.strip_edges())
	var names := _known_repo_names()
	for t in ticked:
		if not names.any(func(n) -> bool: return github_name(str(n)) == github_name(str(t))):
			names.append(t)
	if names.is_empty():
		_hint(_rules_box, "No repos yet: add them under the gear (Factory settings, Repos).")
		return
	var box := VBoxContainer.new()
	_rules_box.add_child(box)
	for repo_name in names:
		var tick := CheckBox.new()
		tick.text = str(repo_name)
		tick.set_pressed_no_signal(ticked.any(func(t) -> bool: return github_name(str(t)) == github_name(str(repo_name))))
		tick.toggled.connect(func(_on: bool) -> void:
			var chosen: Array = []
			for child in box.get_children():
				if child is CheckBox and child.button_pressed:
					chosen.append(child.text)
			_set_rule(port, "repo", ", ".join(chosen)))
		box.add_child(tick)


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
	_option_rows = []
	_option_specs = base.get("option_specs", [])
	var values = machine.get("options", {})
	if not values is Dictionary:
		values = {}
	for spec in base.get("option_specs", []):
		var id := str(spec.get("id", ""))
		var first_child := _options_box.get_child_count()
		_add_option(spec, id, values)
		var nodes: Array = []
		for i in range(first_child, _options_box.get_child_count()):
			nodes.append(_options_box.get_child(i))
		if not str(spec.get("show_when", "")).is_empty():
			_option_rows.append({"when": str(spec["show_when"]), "nodes": nodes})
	_refresh_option_rows(values)


## Show each show_when setting only while its choice has one of the listed values.
func _refresh_option_rows(values: Dictionary) -> void:
	for row in _option_rows:
		var parts := str(row["when"]).split("=", true, 1)
		var key := parts[0].strip_edges()
		var current := ""
		for spec in _option_specs:
			if str(spec.get("id", "")) == key:
				current = str(values.get(key, spec.get("default", "")))
		var shown := parts.size() < 2 or current in Array(parts[1].split("|"))
		for node in row["nodes"]:
			if is_instance_valid(node):
				node.visible = shown
	_refresh_prompt(values)


## A prompt only some settings use (prompt_when, e.g. the Document writer's "Document to
## create"): shown while they do; otherwise a note saying why there's none. Your edits are kept.
func _refresh_prompt(values: Dictionary) -> void:
	var when := str(_prompt_base.get("prompt_when", ""))
	if _prompt_base.is_empty() or when.is_empty():
		return
	var parts := when.split("=", true, 1)
	var key := parts[0].strip_edges()
	var current := ""
	for spec in _option_specs:
		if str(spec.get("id", "")) == key:
			current = str(values.get(key, spec.get("default", "")))
	var used := parts.size() < 2 or current in Array(parts[1].split("|"))
	_prompt_label.visible = used
	_prompt_input.visible = used
	var notes: Dictionary = _prompt_base.get("prompt_notes", {}) if _prompt_base.get("prompt_notes") is Dictionary else {}
	_prompt_note.text = str(notes.get(current, "No prompt with these settings."))
	_prompt_note.visible = not used


## One setting's controls (a tick box, a drop-down or a text box), added to _options_box.
func _add_option(spec: Dictionary, id: String, values: Dictionary) -> void:
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
			m["options"] = opts
			_refresh_option_rows(opts))
		_options_box.add_child(pick)
		return
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
		return
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


## Saves even a line that can't run yet: the backend pauses it and says why.
func _save() -> void:
	save_requested.emit(draft.duplicate(true))
