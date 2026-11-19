extends PanelContainer
## The holding bay: every crate waiting for you, from every Approver (and any machine that
## waits for your approval) and every crate held at a machine, in one list. Decide each one
## here, or tick several and decide them together. The line keeps running meanwhile: crates
## waiting here don't count toward a line's "most crates on the line".

signal closed
signal open_crate(item_id: int)
signal decide(item_ids: Array, kind: String, note: String)  # kind: approve, reject, scrap, retry
signal open_pr(item_id: int)

const WIDTH := 360.0  # the left dock
const C_QUIET := "9AA1AD"
const C_AMBER := "F2B84C"
const C_RED := "E5484D"
const RISK_COLORS := {"low": "5ABF66", "medium": "F2B84C", "high": "E5484D"}

var _title: Label
var _content: VBoxContainer
var _note: LineEdit
var _all_box: CheckBox
var _approve_all: Button
var _reject_all: Button
var _bulk_row: Control
var _shown := ""  # what the rows were built from, to rebuild only on a change
var _picked := {}  # item_id -> true: ticked for a bulk decision
var _waiting_ids: Array = []  # crates you can approve or send back (not held ones)
var _sent := {}  # item_id -> when you decided it here (msec): its buttons are off until it leaves
const SENT_PATIENCE := 6000  # or until this long passes (the backend refused it, say)


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

	var hint := Label.new()
	hint.theme_type_variation = &"LabelMuted"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.text = "Crates wait here for you without holding up the line. Approve or send each back, or tick several."
	col.add_child(hint)

	_note = LineEdit.new()
	_note.placeholder_text = "Note sent with your decision (optional)"
	col.add_child(_note)

	_bulk_row = HBoxContainer.new()
	col.add_child(_bulk_row)
	_all_box = CheckBox.new()
	_all_box.text = "All"
	_all_box.tooltip_text = "Tick every crate waiting for approval"
	_all_box.toggled.connect(func(on: bool) -> void:
		_picked.clear()
		if on:
			for id in _waiting_ids:
				if not _sent.has(id):
					_picked[id] = true
		_shown = ""
		_sync_bulk())
	_bulk_row.add_child(_all_box)
	var gap := Control.new()
	gap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_bulk_row.add_child(gap)
	_approve_all = Button.new()
	_approve_all.theme_type_variation = &"ButtonApprove"
	_approve_all.icon = load("res://assets/ui/icon_check.svg")
	_approve_all.pressed.connect(func() -> void: _decide_picked("approve"))
	_bulk_row.add_child(_approve_all)
	_reject_all = Button.new()
	_reject_all.theme_type_variation = &"ButtonReject"
	_reject_all.icon = load("res://assets/ui/icon_send_back.svg")
	_reject_all.pressed.connect(func() -> void: _decide_picked("reject"))
	_bulk_row.add_child(_reject_all)

	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(scroll)
	_content = VBoxContainer.new()
	_content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_content.add_theme_constant_override("separation", 8)
	scroll.add_child(_content)
	_sync_bulk()


## rows: [{id, title, state ("awaiting_approval" or "held"), at, line, source, risk,
## has_pr (bool), sends_back (bool), held_why}], oldest first.
func show_crates(rows: Array) -> void:
	var ids := {}
	for r in rows:
		ids[int(r["id"])] = true
	for id in _picked.keys():  # decided elsewhere, or gone: untick it
		if not ids.has(id):
			_picked.erase(id)
	var now := Time.get_ticks_msec()
	for id in _sent.keys():
		if not ids.has(id) or now - int(_sent[id]) > SENT_PATIENCE:
			_sent.erase(id)
	var signature := JSON.stringify([rows, _picked.keys(), _sent.keys()])
	if signature == _shown:
		return
	_shown = signature
	_waiting_ids.clear()
	var held := 0
	for r in rows:
		if str(r.get("state", "")) == "awaiting_approval":
			_waiting_ids.append(int(r["id"]))
		else:
			held += 1
	_title.text = "Waiting for you (%d)" % rows.size()
	for child in _content.get_children():
		_content.remove_child(child)
		child.queue_free()
	if rows.is_empty():
		_content.add_child(_text("[color=#%s]Nothing is waiting for you.[/color]" % C_QUIET))
	for r in rows:
		_content.add_child(_row(r))
	_sync_bulk()


func _row(r: Dictionary) -> Control:
	var id := int(r["id"])
	var waiting := str(r.get("state", "")) == "awaiting_approval"
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 2)
	var top := HBoxContainer.new()
	top.add_theme_constant_override("separation", 6)
	box.add_child(top)
	if waiting:
		var tick := CheckBox.new()
		tick.button_pressed = _picked.has(id)
		tick.disabled = _sent.has(id)
		tick.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
		tick.toggled.connect(func(on: bool) -> void:
			if on:
				_picked[id] = true
			else:
				_picked.erase(id)
			_sync_bulk())
		top.add_child(tick)
	var bits := PackedStringArray()
	bits.append("[color=#%s]%s[/color]" % [C_AMBER if waiting else C_RED,
			("at " if waiting else "held at ") + _esc(str(r.get("at", "")))])
	if not str(r.get("line", "")).is_empty():
		bits.append("[color=#%s]%s[/color]" % [C_QUIET, _esc(str(r["line"]))])
	var risk := str(r.get("risk", ""))
	if RISK_COLORS.has(risk):
		bits.append("[color=#%s]%s risk[/color]" % [RISK_COLORS[risk], risk.capitalize()])
	if not str(r.get("source", "")).is_empty():
		bits.append("[color=#%s]%s[/color]" % [C_QUIET, _esc(str(r["source"]))])
	var text := "[b]#%d[/b]  %s\n%s" % [id, _esc(str(r.get("title", ""))), "  ·  ".join(bits)]
	if not waiting and not str(r.get("held_why", "")).is_empty():
		text += "\n[color=#%s]%s[/color]" % [C_QUIET, _esc(str(r["held_why"]))]
	var label := _text(text)
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	top.add_child(label)

	var buttons := HBoxContainer.new()
	buttons.alignment = BoxContainer.ALIGNMENT_END
	box.add_child(buttons)
	if bool(r.get("has_pr", false)):
		var pr := _button(buttons, "PR", "res://assets/ui/icon_pr.svg", &"ButtonGhost",
				"Open PR: open this crate's pull request in your browser")
		pr.pressed.connect(func() -> void: open_pr.emit(id))
	var log_button := _button(buttons, "", "res://assets/ui/icon_log.svg", &"ButtonGhost", "Log: show this crate's log")
	log_button.pressed.connect(func() -> void: open_crate.emit(id))
	var sent := _sent.has(id)
	if waiting:
		var sends_back := bool(r.get("sends_back", false))
		var ok := _button(buttons, "Approve", "res://assets/ui/icon_check.svg", &"ButtonApprove",
				"Approve: let it carry on down the line, with your note")
		ok.disabled = sent
		ok.pressed.connect(func() -> void: _decide_one(id, "approve"))
		var no := _button(buttons, "Send back" if sends_back else "Reject",
				"res://assets/ui/icon_send_back.svg" if sends_back else "res://assets/ui/icon_cross.svg", &"ButtonReject",
				"Send back: out of the red port, with your note" if sends_back
				else "Reject: no red belt here, so it's scrapped (or held at the machine)")
		no.disabled = sent
		no.pressed.connect(func() -> void: _decide_one(id, "reject"))
	else:
		var again := _button(buttons, "Run again", "res://assets/ui/icon_retry.svg", &"",
				"Run again: run this machine again on the crate, with your note")
		again.disabled = sent
		again.pressed.connect(func() -> void: _decide_one(id, "retry"))
		var scrap := _button(buttons, "", "res://assets/ui/icon_bin.svg", &"ButtonGhost", "Scrap: throw it in the scrap bin")
		scrap.disabled = sent
		scrap.pressed.connect(func() -> void: _decide_one(id, "scrap"))
	box.add_child(HSeparator.new())
	return box


func _decide_one(id: int, kind: String) -> void:
	_sent[id] = Time.get_ticks_msec()
	_picked.erase(id)
	decide.emit([id], kind, _note.text.strip_edges())
	_note.clear()
	_shown = ""


func _decide_picked(kind: String) -> void:
	var ids: Array = []
	for id in _waiting_ids:
		if _picked.has(id) and not _sent.has(id):
			ids.append(id)
	if ids.is_empty():
		return
	for id in ids:
		_sent[id] = Time.get_ticks_msec()
	_picked.clear()
	_all_box.set_pressed_no_signal(false)
	decide.emit(ids, kind, _note.text.strip_edges())
	_note.clear()
	_shown = ""
	_sync_bulk()


func _sync_bulk() -> void:
	if _approve_all == null:
		return
	var n := _picked.size()
	_bulk_row.visible = _waiting_ids.size() > 1
	_approve_all.text = "Approve %d" % n if n > 0 else "Approve"
	_reject_all.text = "Send back %d" % n if n > 0 else "Send back"
	_approve_all.disabled = n == 0
	_reject_all.disabled = n == 0
	_approve_all.tooltip_text = "Approve every ticked crate, with your note"
	_reject_all.tooltip_text = "Send every ticked crate back (or reject it, where its machine has no red belt), with your note"


func _button(parent: Control, label: String, icon_path: String, variation: StringName, tip: String) -> Button:
	var b := Button.new()
	b.text = label
	b.icon = load(icon_path)
	if variation != &"":
		b.theme_type_variation = variation
	b.tooltip_text = tip
	b.focus_mode = Control.FOCUS_NONE
	parent.add_child(b)
	return b


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


func open() -> void:
	visible = true
	_shown = ""
