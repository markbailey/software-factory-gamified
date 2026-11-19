extends PanelContainer
## The window for an intake while the factory runs: the issues and pull requests out there in
## each of its sources, which are already crates, and a button to pull one in by hand.

signal closed
signal open_crate(item_id: int)
signal pull(source: String, ref: String, storage: String)
signal check(source: String, storage: String)
signal submit(pipeline: String, text: String)
## A change typed into a Custom storage (storage: its id) or the intake itself (storage "").
signal describe(pipeline: String, text: String, storage: String)
signal pause_toggled(pipeline: String, paused: bool)
## Switch a storage (its id) on or off.
signal storage_toggled(pipeline: String, storage: String, on: bool)
## A change to a Custom storage's queue: msg is a custom_queue_edit / _remove / _move message.
signal queue_change(pipeline: String, msg: Dictionary)
## Close these issues or PRs out there (Jira: move them to a done status), after you confirmed.
signal close_items(source: String, storage: String, refs: Array, comment: String, reason: String, transition: String)
## Ask how they would be closed (Jira's transitions), for the confirm box.
signal close_options(source: String, storage: String, refs: Array)

const WIDTH := 360.0  # the left dock
const C_TEXT := "E6E9EF"
const C_QUIET := "9AA1AD"
const C_BLUE := "8CBFFF"
const C_GREEN := "5ABF66"
const C_RED := "E5484D"
const C_AMBER := "F2B84C"
const SOURCE_TITLES := {"custom": "Changes you describe", "game": "Typed in the game", "github": "GitHub issues", "jira": "Jira issues",
		"pr_review": "Pull requests to review", "prd": "PRDs", "github_pr": "Open PRs", "notion": "Notion pages"}
const SOURCE_ICONS := {"custom": "icon_crate", "game": "icon_crate", "github": "icon_github", "jira": "icon_jira", "pr_review": "icon_pr",
		"prd": "icon_prd", "github_pr": "icon_pr", "notion": "icon_prd"}
const STATE_WORDS := {"queued": "at the intake", "waiting": "on the line", "running": "on the line",
		"moving": "on the line", "awaiting_approval": "waiting for you", "held": "held at a machine for you", "shipped": "shipped",
		"scrapped": "in the scrap bin"}
const STATE_COLORS := {"awaiting_approval": C_AMBER, "held": C_RED, "shipped": C_GREEN, "scrapped": C_RED}

var pipeline := ""
var only_source := ""  # a storage's window: just that source
var only_storage := ""  # and just that storage (its id), when the listing says which is which
var store_name := ""  # its title then ("Jira issues storage")
var _title: Label
var _scroll: ScrollContainer
var _content: VBoxContainer
var _pending := {}  # "source|ref" being pulled in, so its button shows that
var _draft := ""  # a PRD being pasted, kept while the listing refreshes
var _paste_box: TextEdit
var _change_draft := ""  # a change being described, kept while the listing refreshes
var _editing_queue_id := -1  # the queued change being edited, if one is
var _edit_draft := ""


var _pause_button: Button
var _paused := false

# Closing issues and PRs from a storage: what's ticked, and the confirm box while it's open.
const CLOSE_WORDS := {"github": ["issue", "issues"], "prd": ["issue", "issues"], "jira": ["Jira issue", "Jira issues"],
		"github_pr": ["pull request", "pull requests"], "pr_review": ["pull request", "pull requests"]}
var _ticked := {}  # "storage|source|ref" -> its label
var _closing := {}  # the confirm box: {source, storage, refs, labels, account}; empty when shut
var _close_comment := ""
var _close_reason := "completed"
var _close_transitions: Array = []  # Jira: [{id, name, to, done}], from intake_close_options
var _close_transition := ""
var _close_busy := false
var _last_listing := {}
var _last_name := ""


## Whether this line is paused (its storages bring nothing in, and no crate goes into its
## machines). The button only shows in the intake's own window.
func set_paused(paused: bool) -> void:
	_paused = paused
	_pause_button.icon = load("res://assets/ui/icon_play.svg" if paused else "res://assets/ui/icon_pause.svg")
	_pause_button.tooltip_text = ("Resume line: start this line again" if paused else
			"Pause line: stop this line. Nothing new comes in, and crates wait on the belts until you resume it. Machines finish the crate inside.")
	_pause_button.visible = only_source.is_empty()


func _ready() -> void:
	theme_type_variation = &"PanelDock"
	custom_minimum_size = Vector2(WIDTH, 0)

	var col := VBoxContainer.new()
	add_child(col)
	var header := HBoxContainer.new()
	col.add_child(header)
	_title = Label.new()
	_title.theme_type_variation = &"LabelHeading"
	_title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_title.clip_text = true
	header.add_child(_title)
	_pause_button = Button.new()
	_pause_button.theme_type_variation = &"ButtonGhost"
	_pause_button.focus_mode = Control.FOCUS_NONE
	_pause_button.pressed.connect(func() -> void: pause_toggled.emit(pipeline, not _paused))
	header.add_child(_pause_button)
	set_paused(false)
	var esc := Label.new()
	esc.text = "Esc"
	esc.theme_type_variation = &"LabelKey"
	esc.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	header.add_child(esc)
	var close := Button.new()
	close.theme_type_variation = &"ButtonGhost"
	close.icon = load("res://assets/ui/theme/icons/close.svg")
	close.tooltip_text = "Close (Esc)"
	close.pressed.connect(func() -> void: closed.emit())
	header.add_child(close)
	_scroll = ScrollContainer.new()
	_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(_scroll)
	_content = VBoxContainer.new()
	_content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_content.add_theme_constant_override("separation", 6)
	_scroll.add_child(_content)


## Before the backend answers.
func show_loading(pid: String, name: String) -> void:
	if pid != pipeline:
		_pending.clear()
	pipeline = pid
	_title.text = store_name if not only_source.is_empty() else "%s intake" % name
	_clear()
	_content.add_child(_text("[color=#%s]Looking up what's waiting...[/color]" % C_QUIET))


## data: an intake_list event (see protocol/README.md).
func show_listing(data: Dictionary, name: String) -> void:
	if is_instance_valid(_paste_box) and _paste_box.has_focus() and str(data.get("pipeline", "")) == pipeline:
		return  # don't pull the box out from under someone typing; the next refresh catches up
	if str(data.get("pipeline", "")) != pipeline:
		_ticked.clear()
		_closing = {}
	pipeline = str(data.get("pipeline", ""))
	_last_listing = data
	_last_name = name
	_title.text = store_name if not only_source.is_empty() else "%s intake" % name
	var keep := _scroll.scroll_vertical
	_clear()
	var repo: Dictionary = data.get("repo", {}) if data.get("repo") is Dictionary else {}
	if not repo.is_empty() and only_storage.is_empty():
		var what := str(repo.get("github", "")) if not str(repo.get("github", "")).is_empty() else str(repo.get("path", ""))
		_content.add_child(_text("[color=#%s]Works on %s, branching from %s. Change it in build mode, in the intake's settings.[/color]"
				% [C_QUIET, what, str(repo.get("base_branch", "")) if not str(repo.get("base_branch", "")).is_empty() else "its default branch"]))
	var sources: Array = data.get("sources", [])
	if not only_source.is_empty():
		var by_store := not only_storage.is_empty() and sources.any(func(s) -> bool: return str(s.get("storage", "")) == only_storage)
		sources = sources.filter(func(s) -> bool:
			if by_store:
				return str(s.get("storage", "")) == only_storage
			return str(s.get("source", "")) == only_source)
		if sources.is_empty():
			_content.add_child(_text("[color=#%s]Nothing to show yet: save the layout in build mode, then look again.[/color]" % C_QUIET))
	elif sources.is_empty():
		_content.add_child(_text("[color=#%s]This line takes no crates from anywhere yet. In build mode, belt a Storage into its intake or tick typed specs.[/color]" % C_QUIET))
	for s in sources:
		_add_source(s)
	await get_tree().process_frame
	_scroll.scroll_vertical = keep


func _add_source(s: Dictionary) -> void:
	var source := str(s.get("source", ""))
	var storage := str(s.get("storage", ""))
	var items: Array = s.get("items", [])
	var heading := str(SOURCE_TITLES.get(source, source))
	if source != "game" and bool(s.get("enabled", false)) and str(s.get("error", "")).is_empty():
		heading += " (%d)" % items.size()
	if _content.get_child_count() > 0:
		_content.add_child(HSeparator.new())
	var head := HBoxContainer.new()
	_content.add_child(head)
	var icon_path := "res://assets/ui/%s.svg" % str(SOURCE_ICONS.get(source, "icon_crate"))
	if ResourceLoader.exists(icon_path):
		var icon := TextureRect.new()
		icon.texture = load(icon_path)
		icon.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
		icon.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
		icon.custom_minimum_size = Vector2(18, 18)
		icon.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		head.add_child(icon)
	var label := Label.new()
	label.text = heading
	label.theme_type_variation = &"LabelHeading"
	label.clip_text = true
	label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	label.tooltip_text = heading
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(label)
	if bool(s.get("enabled", false)) and not (source in ["game", "custom"]):
		var now_button := Button.new()
		now_button.theme_type_variation = &"ButtonGhost"
		now_button.icon = load("res://assets/ui/icon_refresh.svg")
		now_button.tooltip_text = "Check now: look for new ones straight away and bring in any that come in by themselves"
		now_button.pressed.connect(func() -> void:
			now_button.disabled = true
			now_button.tooltip_text = "Checking..."
			check.emit(source, storage))
		head.add_child(now_button)
	if not storage.is_empty():
		var off := bool(s.get("disabled", false))
		var power := Button.new()
		power.theme_type_variation = &"ButtonGhost"
		power.focus_mode = Control.FOCUS_NONE
		power.text = "Turn on" if off else "Turn off"
		power.tooltip_text = ("Switch this storage on again: it's checked and brings crates in" if off else
				"Switch this storage off: it stays where it is, but isn't checked and brings nothing in until you turn it on")
		power.pressed.connect(func() -> void:
			power.disabled = true
			storage_toggled.emit(pipeline, storage, off))
		head.add_child(power)
		if off:
			label.add_theme_color_override("font_color", Color("#" + C_QUIET))

	# Changes are described in the Custom storage's own window; the intake only lists them.
	var in_storage := source == "game" or not only_source.is_empty()
	var about := str(s.get("about", "")) if in_storage or source != "custom" \
			else "Click the Custom storage to describe a change. Ones you've added are listed here."
	var lines := PackedStringArray()
	if not about.is_empty():
		lines.append("[color=#%s]%s[/color]" % [C_QUIET, _esc(about)])
	var last := float(s.get("last_poll", 0.0))
	if last > 0.0:
		var now := Time.get_unix_time_from_system()
		var when := "Last checked %s ago" % _ago(now - last)
		var next := float(s.get("next_poll", 0.0))
		if next > now:
			when += ", next in %s" % _ago(next - now)
		lines.append("[color=#%s]%s[/color]" % [C_QUIET, when])
	if not str(s.get("poll_error", "")).is_empty():
		lines.append("[color=#%s]The last check failed: %s[/color]" % [C_RED, _esc(str(s["poll_error"]))])
	if not str(s.get("error", "")).is_empty():
		lines.append("[color=#%s]Couldn't list them: %s[/color]" % [C_RED, _esc(str(s["error"]))])
	if not lines.is_empty():
		_content.add_child(_text("\n".join(lines)))
	if bool(s.get("enabled", false)) and not (source in ["game", "custom"]) and items.is_empty() and str(s.get("error", "")).is_empty():
		_content.add_child(_text("[color=#%s]Nothing open right now.[/color]" % C_QUIET))
	if source == "prd":
		_add_paste_box()
	if source in ["custom", "game"] and in_storage:
		_add_change_box(storage)

	var can_close := bool(s.get("can_close", false)) and not items.is_empty()
	if can_close:
		var here := items.map(func(i: Dictionary) -> String: return str(i.get("ref", "")))
		for key in _ticked.keys():  # forget ticks on ones that have gone
			var bits: PackedStringArray = str(key).split("|", true, 2)
			if bits.size() == 3 and bits[0] == storage and bits[1] == source and not (bits[2] in here):
				_ticked.erase(key)
		if not _closing.is_empty() and str(_closing.get("source", "")) == source and str(_closing.get("storage", "")) == storage:
			_add_close_box()
		else:
			_add_close_bar(source, storage, str(s.get("account", "")))

	# Not yet in the factory first, then the ones that are.
	var outside := items.filter(func(i: Dictionary) -> bool: return i.get("item_id") == null)
	var inside := items.filter(func(i: Dictionary) -> bool: return i.get("item_id") != null)
	for item in outside + inside:
		_add_row(source, item, storage, can_close, str(s.get("account", "")))


func _add_row(source: String, item: Dictionary, storage := "", can_close := false, account := "") -> void:
	if item.get("queue_id") != null:
		_add_queued_row(item)
		return
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 6)
	_content.add_child(row)
	var ref := str(item.get("ref", ""))
	var tick_key := "%s|%s|%s" % [storage, source, ref]
	if can_close:
		var tick := CheckBox.new()
		tick.focus_mode = Control.FOCUS_NONE
		tick.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		tick.button_pressed = _ticked.has(tick_key)
		tick.tooltip_text = "Tick to close several at once"
		tick.disabled = _close_busy
		tick.toggled.connect(func(on: bool) -> void:
			if on:
				_ticked[tick_key] = str(item.get("label", ref))
			else:
				_ticked.erase(tick_key)
			_redraw())
		row.add_child(tick)
	var t := PackedStringArray()
	t.append("[b]%s[/b]  %s" % [_esc(str(item.get("label", ""))), _esc(str(item.get("title", "")))])
	var bits := PackedStringArray()
	var item_id = item.get("item_id")
	if item_id != null:
		var state := str(item.get("item_state", ""))
		bits.append("[color=#%s]Crate #%d, %s[/color]" % [STATE_COLORS.get(state, C_BLUE), int(item_id),
				STATE_WORDS.get(state, state)])
	elif bool(item.get("auto", false)):
		bits.append("[color=#%s]Comes in by itself on the next check[/color]" % C_AMBER)
	else:
		bits.append("[color=#%s]Not in the factory[/color]" % C_QUIET)
	if not str(item.get("by", "")).is_empty():
		bits.append("[color=#%s]by %s[/color]" % [C_QUIET, _esc(str(item["by"]))])
	var tags: Array = item.get("tags", [])
	if not tags.is_empty():
		bits.append("[color=#%s]%s[/color]" % [C_QUIET, _esc(", ".join(PackedStringArray(tags)))])
	t.append("  ·  ".join(bits))
	var text := _text("\n".join(t))
	text.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(text)

	var buttons := HBoxContainer.new()
	buttons.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	row.add_child(buttons)
	var key := "%s|%s" % [source, str(item.get("ref", ""))]
	if item_id != null:
		_pending.erase(key)
		var open := Button.new()
		open.theme_type_variation = &"ButtonGhost"
		open.icon = load("res://assets/ui/icon_log.svg")
		open.tooltip_text = "Log: show this crate's log"
		open.pressed.connect(func() -> void: open_crate.emit(int(item_id)))
		buttons.add_child(open)
	if bool(item.get("can_pull", false)) and (item_id == null or (source == "github_pr" and \
			str(item.get("item_state", "")) in ["shipped", "scrapped"])):
		# Not taken yet, or its crate has finished (an open PR that shipped to the dock without
		# merging): pull it in (again) as a new crate.
		var take := Button.new()
		take.icon = load("res://assets/ui/icon_pull_in.svg")
		take.disabled = _pending.has(key)
		var again := item_id != null
		take.tooltip_text = "Pulling in..." if _pending.has(key) else (
				"Pull in again: its last crate has finished; make a new crate for it on this line now" if again
				else "Pull in: make it a crate on this line now")
		take.pressed.connect(func() -> void:
			_pending[key] = true
			take.tooltip_text = "Pulling in..."
			take.disabled = true
			pull.emit(source, str(item.get("ref", "")), storage))
		buttons.add_child(take)
	if can_close:
		var shut := Button.new()
		shut.theme_type_variation = &"ButtonGhost"
		shut.icon = load("res://assets/ui/icon_cross.svg")
		shut.disabled = _close_busy
		shut.tooltip_text = "Close: close it on %s (asks first)" % ("Jira" if source == "jira" else "GitHub")
		shut.pressed.connect(func() -> void:
			_open_close_box(source, storage, [ref], [str(item.get("label", ref))], account))
		buttons.add_child(shut)
	var url := str(item.get("url", ""))
	if not url.is_empty():
		var link := Button.new()
		link.theme_type_variation = &"ButtonGhost"
		link.icon = load("res://assets/ui/icon_open.svg")
		link.tooltip_text = "Open: %s" % url
		link.pressed.connect(func() -> void: OS.shell_open(url))
		buttons.add_child(link)


## A change waiting in a Custom storage's queue: the line takes the top one next. Move it up or
## down, edit it, or take it out.
func _add_queued_row(item: Dictionary) -> void:
	var qid := int(item.get("queue_id"))
	var pos := int(item.get("queue_pos", 1))
	var count := int(item.get("queue_len", 1))
	if qid == _editing_queue_id:
		var box := TextEdit.new()
		box.custom_minimum_size = Vector2(0, 90)
		box.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
		box.text = _edit_draft
		box.text_changed.connect(func() -> void: _edit_draft = box.text)
		_paste_box = box  # so a refresh doesn't pull it out from under you while you type
		_content.add_child(box)
		var bar := HBoxContainer.new()
		_content.add_child(bar)
		var gap := Control.new()
		gap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		bar.add_child(gap)
		var cancel := Button.new()
		cancel.text = "Cancel"
		cancel.theme_type_variation = &"ButtonGhost"
		cancel.pressed.connect(func() -> void:
			_editing_queue_id = -1
			queue_change.emit(pipeline, {}))
		bar.add_child(cancel)
		var save := Button.new()
		save.icon = load("res://assets/ui/icon_save.svg")
		save.text = "Save"
		save.theme_type_variation = &"ButtonPrimary"
		save.tooltip_text = "Save: change the text of this queued change"
		save.pressed.connect(func() -> void:
			var text := box.text.strip_edges()
			if text.is_empty():
				return
			_editing_queue_id = -1
			queue_change.emit(pipeline, {"type": "custom_queue_edit", "id": qid, "text": text}))
		bar.add_child(save)
		box.grab_focus.call_deferred()
		return
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 6)
	_content.add_child(row)
	var when := "Goes onto the line next" if pos == 1 else "Waiting in this storage"
	var text := _text("[b]%s[/b]  %s\n[color=#%s]%s[/color]" % [_esc(str(item.get("label", ""))),
			_esc(str(item.get("title", ""))), C_AMBER if pos == 1 else C_QUIET, when])
	text.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	text.tooltip_text = str(item.get("text", ""))
	row.add_child(text)
	var buttons := HBoxContainer.new()
	buttons.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	row.add_child(buttons)
	var specs := [["res://assets/ui/theme/icons/arrow_up.svg", "Up: the line takes it sooner", pos > 1,
			{"type": "custom_queue_move", "id": qid, "delta": -1}],
		["res://assets/ui/theme/icons/arrow_down.svg", "Down: the line takes it later", pos < count,
			{"type": "custom_queue_move", "id": qid, "delta": 1}],
		["res://assets/ui/icon_name.svg", "Edit: change what it says before it goes onto the line", true, {}],
		["res://assets/ui/icon_delete.svg", "Remove: take it out of the queue", true,
			{"type": "custom_queue_remove", "id": qid}]]
	for spec in specs:
		var b := Button.new()
		b.theme_type_variation = &"ButtonGhost"
		b.focus_mode = Control.FOCUS_NONE
		b.icon = load(spec[0])
		b.tooltip_text = spec[1]
		b.disabled = not spec[2]
		var msg: Dictionary = spec[3]
		b.pressed.connect(func() -> void:
			if msg.is_empty():  # edit
				_editing_queue_id = qid
				_edit_draft = str(item.get("text", item.get("title", "")))
			queue_change.emit(pipeline, msg))
		buttons.add_child(b)


## A box to paste a PRD into: it becomes a crate on this line, like a PRD issue would.
func _add_paste_box() -> void:
	var box := TextEdit.new()
	box.placeholder_text = "Paste a product requirements doc here"
	box.custom_minimum_size = Vector2(0, 120)
	box.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	box.text = _draft
	box.text_changed.connect(func() -> void: _draft = box.text)
	_paste_box = box
	_content.add_child(box)
	var row := HBoxContainer.new()
	_content.add_child(row)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(spacer)
	var add := Button.new()
	add.tooltip_text = "Plan it: put it on this line; the Planner turns it into a plan and issues"
	var icon := load("res://assets/ui/icon_prd.svg") if ResourceLoader.exists("res://assets/ui/icon_prd.svg") else null
	if icon:
		add.icon = icon
	add.pressed.connect(func() -> void:
		var text := box.text.strip_edges()
		if text.is_empty():
			return
		submit.emit(pipeline, text)
		_draft = ""
		box.text = "")
	row.add_child(add)


## The "Describe a change" box: what you type becomes a crate on this line (from this storage).
## Ctrl+Enter adds it too.
func _add_change_box(storage: String) -> void:
	var box := TextEdit.new()
	box.placeholder_text = "Describe a change"
	box.custom_minimum_size = Vector2(0, 90)
	box.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	box.text = _change_draft
	_paste_box = box  # so a refresh doesn't pull it out from under you while you type
	_content.add_child(box)
	var row := HBoxContainer.new()
	_content.add_child(row)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(spacer)
	var add := Button.new()
	add.icon = load("res://assets/ui/icon_add.svg")
	add.text = "Add"
	add.theme_type_variation = &"ButtonPrimary"
	add.tooltip_text = "Add: queue this change; the line takes queued changes one crate at a time (Ctrl+Enter)"
	add.disabled = _change_draft.strip_edges().is_empty()
	var send := func() -> void:
		var text := box.text.strip_edges()
		if text.is_empty():
			return
		describe.emit(pipeline, text, storage)
		_change_draft = ""
		box.text = ""
		add.disabled = true
	box.text_changed.connect(func() -> void:
		_change_draft = box.text
		add.disabled = box.text.strip_edges().is_empty())
	box.gui_input.connect(func(event: InputEvent) -> void:
		var key := event as InputEventKey
		if key and key.pressed and key.keycode in [KEY_ENTER, KEY_KP_ENTER] and key.is_command_or_control_pressed():
			box.accept_event()
			send.call())
	add.pressed.connect(send)
	row.add_child(add)


## "3 ticked: Close" over a storage's list, while some of its rows are ticked.
func _add_close_bar(source: String, storage: String, account: String) -> void:
	var refs := []
	var labels := []
	for key in _ticked.keys():
		var bits: PackedStringArray = str(key).split("|", true, 2)
		if bits.size() == 3 and bits[0] == storage and bits[1] == source:
			refs.append(bits[2])
			labels.append(str(_ticked[key]))
	if refs.is_empty():
		return
	var bar := HBoxContainer.new()
	_content.add_child(bar)
	var what := Label.new()
	what.text = "%d ticked" % refs.size()
	what.theme_type_variation = &"LabelMuted"
	what.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bar.add_child(what)
	var clear := Button.new()
	clear.text = "Untick all"
	clear.theme_type_variation = &"ButtonGhost"
	clear.pressed.connect(func() -> void:
		for key in _ticked.keys():
			if str(key).begins_with("%s|%s|" % [storage, source]):
				_ticked.erase(key)
		_redraw())
	bar.add_child(clear)
	var shut := Button.new()
	shut.icon = load("res://assets/ui/icon_cross.svg")
	shut.text = "Close %d" % refs.size()
	shut.theme_type_variation = &"ButtonReject"
	shut.disabled = _close_busy
	shut.tooltip_text = "Close the ticked ones on %s (asks first)" % ("Jira" if source == "jira" else "GitHub")
	shut.pressed.connect(func() -> void: _open_close_box(source, storage, refs, labels, account))
	bar.add_child(shut)


func _open_close_box(source: String, storage: String, refs: Array, labels: Array, account: String) -> void:
	_closing = {"source": source, "storage": storage, "refs": refs, "labels": labels, "account": account}
	_close_comment = ""
	_close_reason = "completed"
	_close_transitions = []
	_close_transition = ""
	if source == "jira":
		close_options.emit(source, storage, refs)
	_redraw()


## The confirm box: what will be closed, as which account, an optional comment, and (GitHub
## issues) why or (Jira) which transition. Nothing changes out there until Close is pressed.
func _add_close_box() -> void:
	var source := str(_closing.get("source", ""))
	var refs: Array = _closing.get("refs", [])
	var labels: Array = _closing.get("labels", [])
	var words: Array = CLOSE_WORDS.get(source, ["item", "items"])
	var noun: String = "this %s" % words[0] if refs.size() == 1 else "%d %s" % [refs.size(), words[1]]
	var where := "Jira" if source == "jira" else "GitHub"
	var account := str(_closing.get("account", ""))
	var shown := ", ".join(PackedStringArray(labels.slice(0, 8))) + (", ..." if labels.size() > 8 else "")
	var panel := PanelContainer.new()
	panel.theme_type_variation = &"PanelBanner"
	_content.add_child(panel)
	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 6)
	panel.add_child(col)
	var verb := "Move %s to done" % noun if source == "jira" else "Close %s" % noun
	var others := " Someone else's PR: its author is told it was closed." if source == "pr_review" else ""
	col.add_child(_text("[b]%s on %s?[/b]\n%s\n[color=#%s]As %s. This changes %s on %s for everyone; reopen %s there to undo.%s[/color]"
			% [verb, where, _esc(shown), C_QUIET, _esc(account if not account.is_empty() else "the default account"),
			"them" if refs.size() > 1 else "it", where, "them" if refs.size() > 1 else "it", others]))
	var box := TextEdit.new()
	box.placeholder_text = "Comment to post first (optional)"
	box.custom_minimum_size = Vector2(0, 64)
	box.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	box.text = _close_comment
	box.editable = not _close_busy
	box.text_changed.connect(func() -> void: _close_comment = box.text)
	_paste_box = box  # so a refresh doesn't pull it out from under you while you type
	col.add_child(box)
	var pick: OptionButton = null
	if source in ["github", "prd"]:
		pick = OptionButton.new()
		pick.add_item("Completed")
		pick.add_item("Not planned")
		pick.selected = 1 if _close_reason == "not planned" else 0
		pick.tooltip_text = "Why it's closed, as GitHub shows it"
		pick.item_selected.connect(func(i: int) -> void: _close_reason = "not planned" if i == 1 else "completed")
		col.add_child(pick)
	elif source == "jira":
		if _close_transitions.is_empty():
			col.add_child(_text("[color=#%s]Looking up where it can move to...[/color]" % C_QUIET))
		else:
			pick = OptionButton.new()
			pick.tooltip_text = "The transition to make (done statuses first)"
			for t in _close_transitions:
				pick.add_item("%s → %s%s" % [str(t.get("name", "")), str(t.get("to", "")), "" if bool(t.get("done", false)) else " (not done)"])
				if str(t.get("name", "")) == _close_transition:
					pick.selected = pick.item_count - 1
			pick.item_selected.connect(func(i: int) -> void: _close_transition = str(_close_transitions[i].get("name", "")))
			col.add_child(pick)
	var bar := HBoxContainer.new()
	col.add_child(bar)
	var gap := Control.new()
	gap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bar.add_child(gap)
	var cancel := Button.new()
	cancel.text = "Cancel"
	cancel.theme_type_variation = &"ButtonGhost"
	cancel.disabled = _close_busy
	cancel.pressed.connect(func() -> void:
		_closing = {}
		_redraw())
	bar.add_child(cancel)
	var go := Button.new()
	go.icon = load("res://assets/ui/icon_cross.svg")
	go.text = "Closing..." if _close_busy else ("Move to done" if source == "jira" else "Close") + ("" if refs.size() == 1 else " %d" % refs.size())
	go.theme_type_variation = &"ButtonReject"
	go.disabled = _close_busy or (source == "jira" and _close_transitions.is_empty())
	go.pressed.connect(func() -> void:
		_close_busy = true
		close_items.emit(source, str(_closing.get("storage", "")), refs, _close_comment.strip_edges(),
				_close_reason if source in ["github", "prd"] else "", _close_transition if source == "jira" else "")
		_redraw())
	bar.add_child(go)


## intake_close_options from the backend: the Jira transitions for the confirm box.
func show_close_options(msg: Dictionary) -> void:
	if _closing.is_empty() or str(msg.get("source", "")) != str(_closing.get("source", "")):
		return
	_close_transitions = msg.get("transitions", []) if msg.get("transitions") is Array else []
	_close_transition = str(msg.get("default", ""))
	if _close_transition.is_empty() and not _close_transitions.is_empty():
		_close_transition = str(_close_transitions[0].get("name", ""))
	if _close_transitions.is_empty():
		_closing = {}
		_close_busy = false
	_redraw()


## intake_closed from the backend: shut the box and untick what went. Returns a line to toast.
func close_done(msg: Dictionary) -> void:
	_close_busy = false
	_closing = {}
	var storage := str(msg.get("storage", ""))
	var source := str(msg.get("source", ""))
	for ref in msg.get("closed", []):
		_ticked.erase("%s|%s|%s" % [storage, source, str(ref)])
	_redraw()


func _redraw() -> void:
	if not _last_listing.is_empty():
		_paste_box = null  # redraw even if the comment box has focus
		show_listing(_last_listing, _last_name)


## A pull (or close) the backend refused: let the button be pressed again.
func pull_failed() -> void:
	_pending.clear()
	if _close_busy:
		_close_busy = false
		_redraw()


func _clear() -> void:
	for child in _content.get_children():
		_content.remove_child(child)
		child.queue_free()


func _text(bbcode: String) -> RichTextLabel:
	var label := RichTextLabel.new()
	label.bbcode_enabled = true
	label.fit_content = true
	label.scroll_active = false
	label.selection_enabled = true
	label.text = bbcode
	return label


func _esc(text: String) -> String:
	return text.replace("[", "[lb]")


func _ago(seconds: float) -> String:
	var s := maxi(0, int(seconds))
	if s < 60:
		return "%ds" % s
	if s < 3600:
		return "%dm" % (s / 60)
	return "%dh %02dm" % [s / 3600, (s % 3600) / 60]


## How tall the window wants to be to show everything without scrolling.
func wanted_height() -> float:
	return _content.get_combined_minimum_size().y + 64.0
