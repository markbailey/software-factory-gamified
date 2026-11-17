extends PanelContainer
## The window for an intake while the factory runs: the issues and pull requests out there in
## each of its sources, which are already crates, and a button to pull one in by hand.

signal closed
signal open_crate(item_id: int)
signal pull(source: String, ref: String, storage: String)
signal check(source: String, storage: String)
signal submit(pipeline: String, text: String)
signal pause_toggled(pipeline: String, paused: bool)

const WIDTH := 360.0  # the left dock
const C_TEXT := "E6E9EF"
const C_QUIET := "9AA1AD"
const C_BLUE := "8CBFFF"
const C_GREEN := "5ABF66"
const C_RED := "E5484D"
const C_AMBER := "F2B84C"
const SOURCE_TITLES := {"game": "Typed in the game", "github": "GitHub issues", "jira": "Jira issues",
		"pr_review": "Pull requests to review", "prd": "PRDs", "github_pr": "Open PRs"}
const SOURCE_ICONS := {"game": "icon_crate", "github": "icon_github", "jira": "icon_jira", "pr_review": "icon_pr",
		"prd": "icon_prd", "github_pr": "icon_pr"}
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


var _pause_button: Button
var _paused := false


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
	pipeline = str(data.get("pipeline", ""))
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
	if bool(s.get("enabled", false)) and source != "game":
		var now_button := Button.new()
		now_button.theme_type_variation = &"ButtonGhost"
		now_button.icon = load("res://assets/ui/icon_refresh.svg")
		now_button.tooltip_text = "Check now: look for new ones straight away and bring in any that come in by themselves"
		now_button.pressed.connect(func() -> void:
			now_button.disabled = true
			now_button.tooltip_text = "Checking..."
			check.emit(source, storage))
		head.add_child(now_button)

	var about := str(s.get("about", ""))
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
	if bool(s.get("enabled", false)) and source != "game" and items.is_empty() and str(s.get("error", "")).is_empty():
		_content.add_child(_text("[color=#%s]Nothing open right now.[/color]" % C_QUIET))
	if source == "prd":
		_add_paste_box()

	# Not yet in the factory first, then the ones that are.
	var outside := items.filter(func(i: Dictionary) -> bool: return i.get("item_id") == null)
	var inside := items.filter(func(i: Dictionary) -> bool: return i.get("item_id") != null)
	for item in outside + inside:
		_add_row(source, item, storage)


func _add_row(source: String, item: Dictionary, storage := "") -> void:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 6)
	_content.add_child(row)
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
	elif bool(item.get("can_pull", false)):
		var take := Button.new()
		take.icon = load("res://assets/ui/icon_pull_in.svg")
		take.disabled = _pending.has(key)
		take.tooltip_text = "Pulling in..." if _pending.has(key) else "Pull in: make it a crate on this line now"
		take.pressed.connect(func() -> void:
			_pending[key] = true
			take.tooltip_text = "Pulling in..."
			take.disabled = true
			pull.emit(source, str(item.get("ref", "")), storage))
		buttons.add_child(take)
	var url := str(item.get("url", ""))
	if not url.is_empty():
		var link := Button.new()
		link.theme_type_variation = &"ButtonGhost"
		link.icon = load("res://assets/ui/icon_open.svg")
		link.tooltip_text = "Open: %s" % url
		link.pressed.connect(func() -> void: OS.shell_open(url))
		buttons.add_child(link)


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


## A pull the backend refused: let the button be pressed again.
func pull_failed() -> void:
	_pending.clear()


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
