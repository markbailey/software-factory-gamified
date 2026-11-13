extends PanelContainer
## Build mode: edit the line of machines. Changes are only a draft until you press Save;
## the backend checks the line and refuses it if, say, the Tester comes before the Coder.

signal draft_changed(machines: Array)
signal save_requested(machines: Array)
signal cancelled

var catalog := {}  # type -> summary from the backend
var catalog_order: Array = []
var draft: Array = []  # [{type, id, name, gate, prompt}]
var current := -1

var _list: ItemList
var _type_picker: OptionButton
var _name_input: LineEdit
var _gate_box: CheckBox
var _prompt_label: Label
var _prompt_input: TextEdit
var _description: Label
var _message: Label
var _editor: VBoxContainer
var _loading := false


func _ready() -> void:
	custom_minimum_size = Vector2(340, 0)
	var outer := VBoxContainer.new()
	add_child(outer)
	# Everything but the message and Save/Cancel scrolls, so the buttons stay in view.
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	outer.add_child(scroll)
	var col := VBoxContainer.new()
	col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(col)

	var title := Label.new()
	title.text = "Build the line"
	title.add_theme_font_size_override("font_size", 18)
	col.add_child(title)
	var hint := Label.new()
	hint.text = "Crates go through these machines top to bottom. Click a machine on the floor or in the list to edit it."
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.modulate = Color(0.75, 0.75, 0.8)
	col.add_child(hint)

	_list = ItemList.new()
	_list.custom_minimum_size = Vector2(0, 190)
	_list.item_selected.connect(_on_list_selected)
	col.add_child(_list)

	var moves := HBoxContainer.new()
	col.add_child(moves)
	_button(moves, "Up", func() -> void: _move(-1))
	_button(moves, "Down", func() -> void: _move(1))
	_button(moves, "Remove", _remove)

	var add_row := HBoxContainer.new()
	col.add_child(add_row)
	_type_picker = OptionButton.new()
	_type_picker.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_row.add_child(_type_picker)
	_button(add_row, "Add", _add)

	col.add_child(HSeparator.new())

	_editor = VBoxContainer.new()
	col.add_child(_editor)
	_description = Label.new()
	_description.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_description.modulate = Color(0.75, 0.75, 0.8)
	_editor.add_child(_description)
	var name_label := Label.new()
	name_label.text = "Name"
	_editor.add_child(name_label)
	_name_input = LineEdit.new()
	_name_input.max_length = 40
	_name_input.text_changed.connect(_on_name_changed)
	_editor.add_child(_name_input)
	_gate_box = CheckBox.new()
	_gate_box.text = "Wait for my approval after this machine"
	_gate_box.toggled.connect(_on_gate_toggled)
	_editor.add_child(_gate_box)
	_prompt_label = Label.new()
	_prompt_label.text = "Prompt for Claude"
	_editor.add_child(_prompt_label)
	_prompt_input = TextEdit.new()
	_prompt_input.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_prompt_input.custom_minimum_size = Vector2(0, 220)
	_prompt_input.text_changed.connect(_on_prompt_changed)
	_editor.add_child(_prompt_input)

	_message = Label.new()
	_message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_message.modulate = Color("F2B84C")
	outer.add_child(_message)

	var actions := HBoxContainer.new()
	actions.alignment = BoxContainer.ALIGNMENT_END
	outer.add_child(actions)
	_button(actions, "Cancel", func() -> void: cancelled.emit())
	_button(actions, "Save line", func() -> void: save_requested.emit(draft.duplicate(true)))


func _button(parent: Control, label: String, on_press: Callable) -> Button:
	var b := Button.new()
	b.text = label
	b.pressed.connect(on_press)
	parent.add_child(b)
	return b


## catalog_list: machine types the backend offers. machines: the line as it is now.
func open(catalog_list: Array, machines: Array) -> void:
	catalog.clear()
	catalog_order.clear()
	_type_picker.clear()
	for t in catalog_list:
		var kind := str(t.get("type", t.get("id", "")))
		catalog[kind] = t
		catalog_order.append(kind)
		_type_picker.add_item(str(t.get("name", kind)))
	draft.clear()
	for m in machines:
		draft.append({
			"type": str(m.get("type", m.get("id", ""))),
			"id": str(m.get("id", "")),
			"name": str(m.get("name", "")),
			"gate": bool(m.get("gate", false)),
			"prompt": str(m.get("prompt", "")),
		})
	show_message("")
	_refresh(0 if not draft.is_empty() else -1)


func show_message(text: String) -> void:
	_message.text = text


## Select the machine with this id (clicked on the floor).
func select_machine(id: String) -> void:
	for i in draft.size():
		if draft[i]["id"] == id:
			_refresh(i)
			return


## The draft merged with what the catalog knows (sprite, steps), for drawing a preview.
func preview() -> Array:
	var out: Array = []
	for entry in draft:
		var base: Dictionary = catalog.get(entry["type"], {}).duplicate(true)
		base["id"] = entry["id"]
		base["name"] = entry["name"]
		base["gate"] = entry["gate"]
		out.append(base)
	return out


func selected_id() -> String:
	return str(draft[current]["id"]) if current >= 0 and current < draft.size() else ""


func _refresh(select: int) -> void:
	_loading = true
	current = clampi(select, -1, draft.size() - 1)
	_list.clear()
	for entry in draft:
		var label := str(entry["name"])
		if entry["gate"]:
			label += "  (waits for you)"
		_list.add_item(label)
	if current >= 0:
		_list.select(current)
	_editor.visible = current >= 0
	if current >= 0:
		var entry: Dictionary = draft[current]
		var base: Dictionary = catalog.get(entry["type"], {})
		_description.text = str(base.get("description", ""))
		_name_input.text = str(entry["name"])
		_gate_box.button_pressed = bool(entry["gate"])
		var has_prompt := bool(base.get("has_prompt", false))
		_prompt_label.visible = has_prompt
		_prompt_input.visible = has_prompt
		_prompt_input.text = str(entry["prompt"]) if has_prompt else ""
	_loading = false
	_message.text = ""
	draft_changed.emit(preview())


func _on_list_selected(index: int) -> void:
	_refresh(index)


func _move(step: int) -> void:
	var to := current + step
	if current < 0 or to < 0 or to >= draft.size():
		return
	var entry = draft[current]
	draft.remove_at(current)
	draft.insert(to, entry)
	_refresh(to)


func _remove() -> void:
	if current < 0:
		return
	draft.remove_at(current)
	_refresh(mini(current, draft.size() - 1))


func _add() -> void:
	var picked := _type_picker.selected
	if picked < 0 or picked >= catalog_order.size():
		return
	var kind: String = catalog_order[picked]
	var base: Dictionary = catalog[kind]
	var ids := {}
	for existing in draft:
		ids[existing["id"]] = true
	var id := kind
	var n := 2
	while ids.has(id):
		id = "%s-%d" % [kind, n]
		n += 1
	var added := {
		"type": kind,
		"id": id,
		"name": str(base.get("name", kind)),
		"gate": bool(base.get("gate", false)),
		"prompt": str(base.get("prompt", "")),
	}
	var at := current + 1 if current >= 0 else draft.size()
	draft.insert(at, added)
	_refresh(at)


func _on_name_changed(text: String) -> void:
	if _loading or current < 0:
		return
	draft[current]["name"] = text
	_list.set_item_text(current, text + ("  (waits for you)" if draft[current]["gate"] else ""))
	draft_changed.emit(preview())


func _on_gate_toggled(on: bool) -> void:
	if _loading or current < 0:
		return
	draft[current]["gate"] = on
	_list.set_item_text(current, str(draft[current]["name"]) + ("  (waits for you)" if on else ""))
	draft_changed.emit(preview())


func _on_prompt_changed() -> void:
	if _loading or current < 0:
		return
	draft[current]["prompt"] = _prompt_input.text
