extends PanelContainer
## A list of the crates in a scrap bin or at a shipping dock, one row each, so you can pick
## one out even when they lie on top of each other on the floor.

signal closed
signal open_crate(item_id: int)
signal retry(item_id: int)

const FactoryFloor := preload("res://scripts/factory_floor.gd")
const WIDTH := 360.0  # the left dock
const C_QUIET := "9AA1AD"
const C_RED := "E5484D"
const C_GREEN := "5ABF66"

var key := ""  # the station shown, e.g. "build:scrap"
var _title: Label
var _scroll: ScrollContainer
var _content: VBoxContainer
var _shown := ""  # what the rows were built from, to rebuild only on a change


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
	_scroll = ScrollContainer.new()
	_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(_scroll)
	_content = VBoxContainer.new()
	_content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_content.add_theme_constant_override("separation", 6)
	_scroll.add_child(_content)


## rows: [{id, title, source, detail, retry (bool)}], newest first.
func show_crates(station_key: String, title: String, rows: Array, empty_text: String) -> void:
	var signature := JSON.stringify([station_key, title, rows])
	if signature == _shown:
		return
	_shown = signature
	key = station_key
	_title.text = "%s (%d)" % [title, rows.size()]
	for child in _content.get_children():
		_content.remove_child(child)
		child.queue_free()
	if rows.is_empty():
		_content.add_child(_text("[color=#%s]%s[/color]" % [C_QUIET, empty_text]))
	var scrap := FactoryFloor.is_bin(station_key)  # a scrap bin ("scrap" or an extra "scrap_2"), not a dock
	for r in rows:
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 6)
		_content.add_child(row)
		var head := "[b]#%d[/b]  %s" % [int(r["id"]), _esc(str(r.get("title", "")))]
		var bits := PackedStringArray()
		if not str(r.get("detail", "")).is_empty():
			bits.append("[color=#%s]%s[/color]" % [C_RED if scrap else C_GREEN, _esc(str(r["detail"]))])
		if not str(r.get("source", "")).is_empty():
			bits.append("[color=#%s]%s[/color]" % [C_QUIET, _esc(str(r["source"]))])
		var text := _text(head + ("\n" + "  ·  ".join(bits) if not bits.is_empty() else ""))
		text.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		row.add_child(text)
		var buttons := HBoxContainer.new()
		buttons.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		row.add_child(buttons)
		var id := int(r["id"])
		var log_button := Button.new()
		log_button.theme_type_variation = &"ButtonGhost"
		log_button.icon = load("res://assets/ui/icon_log.svg")
		log_button.tooltip_text = "Log: show this crate's log"
		log_button.pressed.connect(func() -> void: open_crate.emit(id))
		if bool(r.get("retry", false)):
			var again := Button.new()
			again.icon = load("res://assets/ui/icon_retry.svg")
			again.tooltip_text = "Retry: send it back to the start of the line"
			again.pressed.connect(func() -> void:
				again.disabled = true
				retry.emit(id))
			buttons.add_child(again)
		buttons.add_child(log_button)


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


## How tall the window wants to be to show everything without scrolling.
func wanted_height() -> float:
	return _content.get_combined_minimum_size().y + 64.0
