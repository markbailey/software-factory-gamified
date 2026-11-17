extends PanelContainer
## Factory stats (left dock): how many crates shipped and were scrapped, how long they take,
## how often they fail or come back for rework, and what Claude cost, per day, line and machine.
## The backend works the numbers out; this asks for them when it opens and every so often.

signal closed
signal refresh(days: int)

const C_QUIET := "9AA1AD"
const C_RED := "E5484D"
const C_GREEN := "5ABF66"
const C_AMBER := "F5A524"
const C_BAR := "5AB0FF"
const REFRESH_SECONDS := 20.0
const DAY_CHOICES := [1, 7, 30]

var days := 7
var _title: Label
var _content: VBoxContainer
var _range: OptionButton
var _tick := 0.0
var _last: Dictionary = {}


func _ready() -> void:
	theme_type_variation = &"PanelDock"
	var col := VBoxContainer.new()
	add_child(col)
	var header := HBoxContainer.new()
	col.add_child(header)
	_title = Label.new()
	_title.text = "Factory stats"
	_title.theme_type_variation = &"LabelHeading"
	_title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(_title)
	_range = OptionButton.new()
	for d in DAY_CHOICES:
		_range.add_item("Today" if d == 1 else "Last %d days" % d)
	_range.select(DAY_CHOICES.find(days))
	_range.focus_mode = Control.FOCUS_NONE
	_range.item_selected.connect(func(i: int) -> void:
		days = int(DAY_CHOICES[i])
		_ask())
	header.add_child(_range)
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
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(scroll)
	_content = VBoxContainer.new()
	_content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_content.add_theme_constant_override("separation", 10)
	scroll.add_child(_content)
	_content.add_child(_text("[color=#%s]Working out the numbers…[/color]" % C_QUIET))


func open() -> void:
	visible = true
	_ask()


func _process(delta: float) -> void:
	if not visible:
		return
	_tick += delta
	if _tick >= REFRESH_SECONDS:
		_ask()


func _ask() -> void:
	_tick = 0.0
	refresh.emit(days)


## The backend's "stats" message.
func show_stats(s: Dictionary) -> void:
	if int(s.get("days", days)) != days:
		return  # an answer to an earlier choice
	_last = s
	for child in _content.get_children():
		_content.remove_child(child)
		child.queue_free()
	var shipped := int(s.get("shipped", 0))
	var scrapped := int(s.get("scrapped", 0))
	var times: Dictionary = s.get("crate_minutes", {})
	var budget: Dictionary = s.get("budget", {})

	# The headline numbers.
	var grid := GridContainer.new()
	grid.columns = 2
	grid.add_theme_constant_override("h_separation", 16)
	grid.add_theme_constant_override("v_separation", 6)
	_content.add_child(grid)
	_tile(grid, str(shipped), "shipped", C_GREEN)
	_tile(grid, str(scrapped), "scrapped", C_RED if scrapped > 0 else C_QUIET)
	_tile(grid, _minutes(float(times.get("median", 0.0))) if shipped else "–", "typical time per crate")
	_tile(grid, _pct(float(s.get("rework_rate", 0.0))) if shipped else "–", "shipped after rework")
	_tile(grid, _usd(float(s.get("cost_usd", 0.0))), "Claude cost")
	_tile(grid, _usd(float(s.get("cost_per_shipped", 0.0))) if shipped else "–", "per shipped crate")

	# Today's spend against the budget.
	var today := float(budget.get("today_usd", 0.0))
	var daily := float(budget.get("daily_usd", 0.0))
	var crate_cap := float(budget.get("crate_usd", 0.0))
	var spend := "Today so far: [b]%s[/b]" % _usd(today)
	if daily > 0.0:
		var colour := C_RED if today >= daily else (C_AMBER if today >= daily * 0.8 else C_QUIET)
		spend += " [color=#%s]of %s a day[/color]" % [colour, _usd(daily)]
	if crate_cap > 0.0:
		spend += "[color=#%s]  ·  at most %s a crate[/color]" % [C_QUIET, _usd(crate_cap)]
	if daily <= 0.0 and crate_cap <= 0.0:
		spend += "[color=#%s]  ·  no budget set ([lb]budget] in factory.toml)[/color]" % C_QUIET
	_content.add_child(_text(spend))

	# Per day: a small bar chart of crates shipped (and scrapped).
	var per_day: Array = s.get("per_day", [])
	if per_day.size() > 1:
		_content.add_child(_heading("Per day"))
		var most := 1
		for d in per_day:
			most = maxi(most, int(d.get("shipped", 0)) + int(d.get("scrapped", 0)))
		for d in per_day:
			_content.add_child(_day_row(d, most))

	var lines: Array = s.get("lines", [])
	if lines.size() > 1:
		_content.add_child(_heading("Lines"))
		for ln in lines:
			var bits := PackedStringArray()
			bits.append("[color=#%s]%d shipped[/color]" % [C_GREEN, int(ln.get("shipped", 0))])
			if int(ln.get("scrapped", 0)) > 0:
				bits.append("[color=#%s]%d scrapped[/color]" % [C_RED, int(ln.get("scrapped", 0))])
			bits.append(_usd(float(ln.get("cost_usd", 0.0))))
			_content.add_child(_text("[b]%s[/b]\n%s" % [_esc(str(ln.get("name", ""))), "  ·  ".join(bits)]))

	var machines: Array = s.get("machines", [])
	if not machines.is_empty():
		_content.add_child(_heading("Machines"))
		for m in machines:
			_content.add_child(_machine_row(m, lines.size() > 1))
	if shipped == 0 and scrapped == 0 and machines.is_empty():
		_content.add_child(_text("[color=#%s]Nothing has run in this time yet.[/color]" % C_QUIET))


func _machine_row(m: Dictionary, show_line: bool) -> RichTextLabel:
	var runs := int(m.get("runs", 0))
	var failed := int(m.get("failed", 0))
	var head := "[b]%s[/b]" % _esc(str(m.get("name", m.get("id", ""))))
	if show_line:
		head += "  [color=#%s]%s[/color]" % [C_QUIET, _esc(str(m.get("line", "")))]
	var bits := PackedStringArray()
	bits.append("%d run%s" % [runs, "" if runs == 1 else "s"])
	bits.append("%s each" % _seconds(float(m.get("avg_seconds", 0.0))))
	if failed > 0:
		bits.append("[color=#%s]%s failed[/color]" % [C_RED, _pct(float(m.get("fail_rate", 0.0)))])
	if float(m.get("cost_usd", 0.0)) > 0.0:
		bits.append(_usd(float(m.get("cost_usd", 0.0))))
	var text := head + "\n" + "  ·  ".join(bits)
	var top: Array = m.get("top_failures", [])
	if not top.is_empty():
		var steps := PackedStringArray()
		for t in top:
			steps.append("%s ×%d" % [_esc(str(t.get("step", ""))), int(t.get("count", 0))])
		text += "\n[color=#%s]Fails at: %s[/color]" % [C_QUIET, ", ".join(steps)]
	return _text(text)


func _day_row(d: Dictionary, most: int) -> Control:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 8)
	var date := Label.new()
	date.text = str(d.get("date", ""))
	date.theme_type_variation = &"LabelMuted"
	date.custom_minimum_size = Vector2(84, 0)
	row.add_child(date)
	var bar := Control.new()
	bar.custom_minimum_size = Vector2(0, 14)
	bar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bar.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	var good := int(d.get("shipped", 0))
	var bad := int(d.get("scrapped", 0))
	bar.draw.connect(func() -> void:
		var w := bar.size.x
		var g := w * float(good) / float(most)
		var b := w * float(bad) / float(most)
		bar.draw_rect(Rect2(0, 0, w, bar.size.y), Color(1, 1, 1, 0.05))
		if g > 0.0:
			bar.draw_rect(Rect2(0, 0, g, bar.size.y), Color(C_GREEN))
		if b > 0.0:
			bar.draw_rect(Rect2(g, 0, b, bar.size.y), Color(C_RED)))
	row.add_child(bar)
	var count := Label.new()
	count.text = "%d%s" % [good, (" / %d" % bad) if bad > 0 else ""]
	count.custom_minimum_size = Vector2(44, 0)
	count.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	row.add_child(count)
	var cost := Label.new()
	cost.text = _usd(float(d.get("cost_usd", 0.0)))
	cost.theme_type_variation = &"LabelMuted"
	cost.custom_minimum_size = Vector2(56, 0)
	cost.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	row.add_child(cost)
	return row


func _tile(grid: GridContainer, value: String, words: String, colour: String = "") -> void:
	var v := "[font_size=22][b]%s[/b][/font_size]" % value
	if not colour.is_empty():
		v = "[color=#%s]%s[/color]" % [colour, v]
	var label := _text("%s\n[color=#%s]%s[/color]" % [v, C_QUIET, words])
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	grid.add_child(label)


func _heading(text: String) -> Label:
	var label := Label.new()
	label.text = text
	label.theme_type_variation = &"LabelHeading"
	return label


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


static func _usd(v: float) -> String:
	if v <= 0.0:
		return "$0"
	return "$%.2f" % v if v >= 0.1 else "$%.3f" % v


static func _pct(v: float) -> String:
	return "%d%%" % roundi(v * 100.0)


static func _minutes(m: float) -> String:
	if m < 1.0:
		return "%ds" % roundi(m * 60.0)
	if m < 90.0:
		return "%dm" % roundi(m)
	return "%.1fh" % (m / 60.0)


static func _seconds(s: float) -> String:
	if s < 90.0:
		return "%ds" % roundi(s)
	return _minutes(s / 60.0)
