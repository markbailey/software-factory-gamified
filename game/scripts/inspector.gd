extends PanelContainer
## The status window for one machine while the factory runs: what it is doing right now,
## its steps, the crates queued in front of it, and its latest output. Read-only: settings
## are changed in build mode.

signal closed
signal open_crate(item_id: int)

const WIDTH := 360.0  # the left dock
const C_TEXT := "E6E9EF"
const C_QUIET := "9AA1AD"
const C_BLUE := "8CBFFF"
const C_GREEN := "5ABF66"
const C_RED := "E5484D"
const C_AMBER := "F2B84C"

var machine_id := ""
var _title: Label
var _body: RichTextLabel
var _output_title: Label
var _output: RichTextLabel
var _crate_button: Button
var _crate_id := -1
var _last_output := ""
var _content: VBoxContainer


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
	# Everything under the title scrolls when the window is shorter than its contents.
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(scroll)
	var inner := VBoxContainer.new()
	inner.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(inner)
	_content = inner
	col = inner

	_body = RichTextLabel.new()
	_body.bbcode_enabled = true
	_body.fit_content = true
	_body.scroll_active = false
	_body.selection_enabled = true
	_body.meta_underlined = false
	_body.meta_clicked.connect(func(meta) -> void: open_crate.emit(int(str(meta))))
	col.add_child(_body)

	_crate_button = Button.new()
	_crate_button.icon = load("res://assets/ui/icon_log.svg")
	_crate_button.pressed.connect(func() -> void:
		if _crate_id >= 0:
			open_crate.emit(_crate_id))
	col.add_child(_crate_button)

	_output_title = Label.new()
	_output_title.text = "Latest output"
	_output_title.theme_type_variation = &"LabelMuted"
	col.add_child(_output_title)
	_output = RichTextLabel.new()
	_output.bbcode_enabled = true
	_output.scroll_following = true
	_output.selection_enabled = true
	_output.custom_minimum_size = Vector2(0, 170)
	_output.theme_type_variation = &"LogText"
	col.add_child(_output)


## data: see main.gd _machine_status().
func show_status(data: Dictionary) -> void:
	machine_id = str(data.get("id", ""))
	_title.text = str(data.get("name", machine_id))
	var t := PackedStringArray()
	t.append("[color=#%s]%s[/color]" % [C_QUIET, _esc(str(data.get("description", "")))])
	t.append("")
	t.append("[b][color=#%s]%s[/color][/b]" % [str(data.get("status_color", C_TEXT)), _esc(str(data.get("status", "")))])

	var crate: Dictionary = data.get("crate", {})
	if not crate.is_empty():
		t.append("Crate #%d: %s" % [int(crate["id"]), _esc(str(crate.get("title", "")))])
		if int(crate.get("loops", 0)) > 0:
			t.append("[color=#%s]Back for rework (%d of %d)[/color]" % [C_AMBER, int(crate["loops"]),
					int(data.get("max_rework", 0))])
		var risk := str(crate.get("risk", ""))
		if not risk.is_empty():
			var colors := {"low": C_GREEN, "medium": C_AMBER, "high": C_RED}
			var why := str(crate.get("risk_reason", ""))
			t.append("[color=#%s]%s risk[/color]%s" % [colors.get(risk, C_TEXT), risk.capitalize(),
					("[color=#%s]: %s[/color]" % [C_QUIET, _esc(why)]) if not why.is_empty() else ""])
	var elapsed := float(data.get("elapsed", -1.0))
	if elapsed >= 0.0:
		var line := "On this machine for %s" % _duration(elapsed)
		var step_elapsed := float(data.get("step_elapsed", -1.0))
		if step_elapsed >= 0.0:
			line += ", this step for %s" % _duration(step_elapsed)
		t.append("[color=#%s]%s[/color]" % [C_QUIET, line])

	# Steps, with where the crate is.
	var steps: Array = data.get("steps", [])
	if not steps.is_empty():
		t.append("")
		t.append("[b]Steps[/b]")
		var at := int(data.get("step_index", -1))
		var failed := bool(data.get("failed", false))
		var finished := bool(data.get("finished", false))
		for i in steps.size():
			var mark := "[color=#%s]○[/color]" % C_QUIET
			if finished or (at >= 0 and i < at):
				mark = "[color=#%s]✓[/color]" % C_GREEN
			elif i == at:
				mark = "[color=#%s]✗[/color]" % C_RED if failed else "[color=#%s]▶[/color]" % C_BLUE
			var name := _esc(str(steps[i]))
			if i == at and not finished:
				var pct := int(round(float(data.get("progress", 0.0)) * 100.0))
				name = "[b]%s[/b]  [color=#%s]%d%%[/color]" % [name, C_QUIET, pct]
			t.append("%s  %s" % [mark, name])

	t.append("")
	var queue: Array = data.get("queue", [])
	if queue.is_empty():
		t.append("[color=#%s]Nothing waiting in front of it[/color]" % C_QUIET)
	else:
		t.append("[b]Waiting in front (%d)[/b]" % queue.size())
		for q in queue.slice(0, 6):
			t.append("  %s" % _crate_link(str(q)))
		if queue.size() > 6:
			t.append("  [color=#%s]and %d more[/color]" % [C_QUIET, queue.size() - 6])
	var held: Array = data.get("held", [])
	if not held.is_empty():
		t.append("[b][color=#%s]Waiting on GitHub (%d)[/color][/b]  [color=#%s]these carry on by themselves; ready ones merge past them[/color]"
				% [C_AMBER, held.size(), C_QUIET])
		for q in held.slice(0, 12):
			t.append("  %s" % _crate_link(str(q)))
		if held.size() > 12:
			t.append("  [color=#%s]and %d more[/color]" % [C_QUIET, held.size() - 12])
	var stuck: Array = data.get("stuck", [])
	if not stuck.is_empty():
		t.append("[b][color=#%s]Held here (%d)[/color][/b]  [color=#%s]failed with nowhere to go: click one to run it again or scrap it[/color]"
				% [C_RED, stuck.size(), C_QUIET])
		for q in stuck.slice(0, 12):
			t.append("  %s" % _crate_link(str(q)))
		if stuck.size() > 12:
			t.append("  [color=#%s]and %d more[/color]" % [C_QUIET, stuck.size() - 12])
	var approvals: Array = data.get("approvals", [])
	if not approvals.is_empty():
		t.append("[b][color=#%s]Waiting for you (%d)[/color][/b]  [color=#%s]click one to approve or send it back[/color]"
				% [C_AMBER, approvals.size(), C_QUIET])
		for q in approvals.slice(0, 12):
			t.append("  %s" % _crate_link(str(q)))
		if approvals.size() > 12:
			t.append("  [color=#%s]and %d more[/color]" % [C_QUIET, approvals.size() - 12])

	t.append("")
	var facts := PackedStringArray()
	if bool(data.get("gate", false)):
		match str(data.get("auto_approve", "")):
			"low":
				facts.append("approves by itself when the review looks good and the risk is low; otherwise waits for you")
			"medium":
				facts.append("approves by itself when the review looks good and the risk is low or medium; otherwise waits for you")
			_:
				facts.append("waits for your approval")
	facts.append("failures go %s" % str(data.get("on_fail", "to the scrap bin")))
	if bool(data.get("uses_agent", false)):
		facts.append("uses Claude")
	if bool(data.get("parallel", false)):
		facts.append("takes any number of crates at once")
	var summary := ", ".join(facts)
	t.append("[color=#%s]%s.[/color]" % [C_QUIET, _esc(summary.substr(0, 1).to_upper() + summary.substr(1))])
	var stats: Dictionary = data.get("stats", {})
	t.append("[color=#%s]Since the game opened: %d passed, %d failed[/color]" % [C_QUIET,
			int(stats.get("passed", 0)), int(stats.get("failed", 0))])
	_body.text = "\n".join(t)

	_crate_id = int(crate.get("id", -1)) if not crate.is_empty() else -1
	_crate_button.visible = _crate_id >= 0
	_crate_button.text = "Open crate #%d's full log" % _crate_id

	var lines: Array = data.get("output", [])
	_output_title.visible = not lines.is_empty()
	_output.visible = not lines.is_empty()
	var out := PackedStringArray()
	for l in lines:
		out.append(_esc(str(l)))
	var text := "\n".join(out)
	if text != _last_output:
		_last_output = text
		_output.text = text


## "#12 Title" as a link that opens crate 12's log (with its Approve and Send back buttons).
func _crate_link(text: String) -> String:
	var id := text.get_slice(" ", 0).trim_prefix("#")
	if not id.is_valid_int():
		return _esc(text)
	return "[url=%s][color=#%s]%s[/color][/url]" % [id, C_BLUE, _esc(text)]


func _esc(text: String) -> String:
	return text.replace("[", "[lb]")


func _duration(seconds: float) -> String:
	var s := int(seconds)
	if s < 60:
		return "%ds" % s
	if s < 3600:
		return "%dm %02ds" % [s / 60, s % 60]
	return "%dh %02dm" % [s / 3600, (s % 3600) / 60]


## How tall the window wants to be to show everything without scrolling.
func wanted_height() -> float:
	return _content.get_combined_minimum_size().y + 64.0
