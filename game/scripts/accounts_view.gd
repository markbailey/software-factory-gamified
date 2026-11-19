extends PanelContainer
## Accounts (left dock): the GitHub and Jira accounts the factory can work as. Add as many as you
## like; each service has a default, and a line (its intake settings) or a storage can pick
## another in build mode. "This machine's sign-in" (gh auth login, git's credential manager,
## JIRA_API_TOKEN) is always a choice. The backend keeps them in backend/accounts.toml
## (gitignored) and never sends a token back: a saved one shows as "saved", and leaving its box
## empty keeps it.

signal save_account(service: String, account: Dictionary)
signal remove_account(service: String, id: String)
signal set_default(service: String, id: String)
signal check
signal closed
signal force_delete_changed(on: bool)  # the Cleanup section's tick box; applies at once
signal branch_pattern_changed(pattern: String)  # the Branch naming section's Save
signal repos_changed(repos: Array)  # the Repos section: the whole list of names it saves
signal agent_defaults_changed(model: String, effort: String)  # the Claude section; applies at once

const MACHINE := "@machine"
const C_QUIET := "9AA1AD"
const C_RED := "E5484D"
const C_GREEN := "5ABF66"
const SERVICES := {"github": "GitHub", "jira": "Jira"}
const FIELDS := {
	"github": [["name", "Name", "e.g. Factory bot", false],
		["token", "Token", "github_pat_... (Contents, Issues, Pull requests: read and write)", true],
		["git_name", "Commit author name (optional)", "e.g. Factory Bot", false],
		["git_email", "Commit author email (optional)", "e.g. 12345+factory-bot@users.noreply.github.com", false]],
	"jira": [["name", "Name", "e.g. Acme Jira bot", false],
		["base_url", "Jira site", "https://your-site.atlassian.net", false],
		["email", "Email of the Atlassian account", "factory-bot@example.com", false],
		["token", "API token", "from id.atlassian.com, Security, API tokens", true]],
}

var _content: VBoxContainer
var _last: Dictionary = {}
var _editing := {}  # {service, id ("" for a new one)} while the form is open
var _inputs := {}  # field -> LineEdit in the form
var _note := ""
var _force_delete := false  # as the backend last said
var _branch_pattern := "crate/{id}"  # as the backend last said
var _repos: Array = []  # [{github, saved, in_use, default}] as the backend last said
var _repo_input: LineEdit
var _agent_defaults := {"model": "", "effort": ""}  # as the backend last said ("" = the CLI's own)
var _agent_choices := {"models": [], "efforts": []}


func _ready() -> void:
	theme_type_variation = &"PanelDock"
	var col := VBoxContainer.new()
	add_child(col)
	var header := HBoxContainer.new()
	col.add_child(header)
	var title := Label.new()
	title.text = "Factory settings"
	title.theme_type_variation = &"LabelHeading"
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(title)
	var check_button := Button.new()
	check_button.theme_type_variation = &"ButtonGhost"
	check_button.icon = load("res://assets/ui/icon_refresh.svg")
	check_button.tooltip_text = "Check who each account is signed in as"
	check_button.pressed.connect(func() -> void:
		_note = "[color=#%s]Checking...[/color]" % C_QUIET
		_render()
		check.emit())
	header.add_child(check_button)
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
	_content.add_theme_constant_override("separation", 6)
	scroll.add_child(_content)
	_render()


func open() -> void:
	visible = true


## The backend's force delete setting (snapshot or "force_delete" message).
func show_force_delete(on: bool) -> void:
	if on == _force_delete:
		return
	_force_delete = on
	_render()


## The backend's branch naming pattern (snapshot or "branch_pattern" message).
func show_branch_pattern(pattern: String) -> void:
	if pattern.is_empty() or pattern == _branch_pattern:
		return
	_branch_pattern = pattern
	_render()


## What a pattern gives for a made-up crate (the backend cleans it the same way, and has the say).
static func branch_preview(pattern: String) -> String:
	var sample := {"id": "42", "issue": "PROJ-12", "issue_number": "12", "slug": "add-dark-mode",
		"line": "build", "machine": "coder", "source": "jira", "repo": "my-app",
		"date": Time.get_date_string_from_system(), "yyyy": "%04d" % Time.get_date_dict_from_system()["year"],
		"mm": "%02d" % Time.get_date_dict_from_system()["month"], "dd": "%02d" % Time.get_date_dict_from_system()["day"]}
	var out := pattern
	for key in sample:
		out = out.replace("{%s}" % key, str(sample[key]))
	return out.replace(" ", "-")


## The factory-wide Claude model and effort (snapshot or "agent_defaults" message), and what
## the pickers list (snapshot's agent_choices; empty keeps the last).
func show_agent_defaults(defaults: Dictionary, choices: Dictionary = {}) -> void:
	var d := {"model": str(defaults.get("model", "")), "effort": str(defaults.get("effort", ""))}
	if d == _agent_defaults and (choices.is_empty() or choices == _agent_choices):
		return
	_agent_defaults = d
	if not choices.is_empty():
		_agent_choices = choices.duplicate(true)
	_render()


## The backend's repo list (snapshot or "repos" message).
func show_repos(repos: Array) -> void:
	if repos == _repos:
		return
	_repos = repos.duplicate(true)
	_render()


## The backend's "accounts" message.
func show_accounts(msg: Dictionary) -> void:
	var checked: Dictionary = _last.get("check", {})
	_last = msg.duplicate()
	if not msg.has("check") and not checked.is_empty():
		_last["check"] = checked  # keep the last check's answers until the next one
	if msg.has("error"):
		_note = "[color=#%s]Not saved: %s[/color]" % [C_RED, _esc(str(msg["error"]))]
	else:
		_note = ""
		if msg.has("saved") or msg.has("check"):
			_editing = {}
	_render()


func _render() -> void:
	if _content == null:
		return
	for child in _content.get_children():
		_content.remove_child(child)
		child.queue_free()
	_content.add_child(_text("[color=#%s]The accounts the factory works as. Each service has a default; a line (its intake settings) or a storage can pick another in build mode. Kept in backend/accounts.toml, which git ignores; tokens never come back to the game.[/color]" % C_QUIET))
	if not _note.is_empty():
		_content.add_child(_text(_note))
	for service in SERVICES:
		_content.add_child(HSeparator.new())
		_section(service)
	_content.add_child(HSeparator.new())
	_repos_section()
	_content.add_child(HSeparator.new())
	_agent_section()
	_content.add_child(HSeparator.new())
	_branch_section()
	_content.add_child(HSeparator.new())
	_cleanup_section()


## Repos: the GitHub repos lines, storages and sorter rules pick from in build mode.
func _repos_section() -> void:
	var label := Label.new()
	label.text = "Repos"
	label.theme_type_variation = &"LabelHeading"
	_content.add_child(label)
	_content.add_child(_text("[color=#%s]The GitHub repos the factory works on. A line's repo, a storage's repo and a sorter's Repo is rule pick from this list in build mode. Repos a line or storage already uses are listed too, and stay until nothing uses them.[/color]" % C_QUIET))
	for r in _repos:
		var repo_name := str(r.get("github", ""))
		var row := HBoxContainer.new()
		_content.add_child(row)
		var words := repo_name
		if r.get("in_use", false):
			words += "  [color=#%s](in use)[/color]" % C_QUIET
		var text := _text(words)
		text.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		row.add_child(text)
		if r.get("saved", false):
			var remove := Button.new()
			remove.theme_type_variation = &"ButtonGhost"
			remove.icon = load("res://assets/ui/theme/icons/close.svg")
			remove.tooltip_text = "Take %s off the list" % repo_name
			remove.pressed.connect(func() -> void:
				repos_changed.emit(_saved_repo_names().filter(func(n: String) -> bool: return n != repo_name)))
			row.add_child(remove)
	var add_row := HBoxContainer.new()
	_content.add_child(add_row)
	_repo_input = LineEdit.new()
	_repo_input.placeholder_text = "owner/name or a github.com link"
	_repo_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_repo_input.text_submitted.connect(func(_t: String) -> void: _add_repo())
	add_row.add_child(_repo_input)
	var add := Button.new()
	add.theme_type_variation = &"ButtonGhost"
	add.icon = load("res://assets/ui/icon_add.svg")
	add.tooltip_text = "Add this repo to the list"
	add.pressed.connect(_add_repo)
	add_row.add_child(add)


func _saved_repo_names() -> Array:
	var out: Array = []
	for r in _repos:
		if r.get("saved", false):
			out.append(str(r.get("github", "")))
	return out


func _add_repo() -> void:
	var repo_name := _repo_input.text.strip_edges() if _repo_input else ""
	if repo_name.is_empty():
		return
	var names := _saved_repo_names()
	# Ones already in use go on the saved list too, so they stay once nothing uses them.
	for r in _repos:
		if not r.get("saved", false) and not r.get("default", false):
			names.append(str(r.get("github", "")))
	names.append(repo_name)
	repos_changed.emit(names)


## Claude: the model and effort level every Claude machine uses unless its own settings pick one.
func _agent_section() -> void:
	var label := Label.new()
	label.text = "Claude"
	label.theme_type_variation = &"LabelHeading"
	_content.add_child(label)
	_content.add_child(_text("[color=#%s]The model and effort level machines that run Claude use (Coder, Reviewer, Planner, Document writer and the rest). A machine can pick its own in build mode; one left on Default uses these. Changes straight away, from the next step a machine starts.[/color]" % C_QUIET))
	for field in ["model", "effort"]:
		var row := HBoxContainer.new()
		_content.add_child(row)
		var caption := Label.new()
		caption.text = "Model" if field == "model" else "Effort"
		caption.custom_minimum_size = Vector2(70, 0)
		row.add_child(caption)
		var pick := OptionButton.new()
		pick.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		pick.clip_text = true
		pick.add_item("Claude Code's default")
		pick.set_item_metadata(0, "")
		var values: Array = _agent_choices.get("models" if field == "model" else "efforts", [])
		var current := str(_agent_defaults.get(field, ""))
		if not current.is_empty() and not values.has(current):
			values = values + [current]  # one typed into agents.json by hand
		for v in values:
			pick.add_item(str(v))
			pick.set_item_metadata(pick.item_count - 1, str(v))
			if str(v) == current:
				pick.select(pick.item_count - 1)
		pick.item_selected.connect(func(i: int) -> void:
			_agent_defaults[field] = str(pick.get_item_metadata(i))
			agent_defaults_changed.emit(str(_agent_defaults["model"]), str(_agent_defaults["effort"])))
		row.add_child(pick)


## Branch naming: the pattern machines name a crate's branch by (a line can have its own in build mode).
func _branch_section() -> void:
	var label := Label.new()
	label.text = "Branch naming"
	label.theme_type_variation = &"LabelHeading"
	_content.add_child(label)
	var row := HBoxContainer.new()
	_content.add_child(row)
	var edit := LineEdit.new()
	edit.text = _branch_pattern
	edit.placeholder_text = "crate/{id}"
	edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(edit)
	var preview := _text("")
	var refresh := func(t: String) -> void:
		preview.text = "[color=#%s]e.g. %s[/color]" % [C_QUIET, _esc(branch_preview(t if not t.strip_edges().is_empty() else "crate/{id}"))]
	refresh.call(_branch_pattern)
	edit.text_changed.connect(refresh)
	var save := Button.new()
	save.text = "Save"
	save.tooltip_text = "Name new crates' branches by this pattern (blank: crate/{id}, as before)"
	save.pressed.connect(func() -> void:
		_branch_pattern = edit.text.strip_edges() if not edit.text.strip_edges().is_empty() else "crate/{id}"
		branch_pattern_changed.emit(edit.text.strip_edges()))
	edit.text_submitted.connect(func(_t: String) -> void: save.pressed.emit())
	row.add_child(save)
	_content.add_child(preview)
	_content.add_child(_text("[color=#%s]Placeholders: {id} crate number, {issue} issue key or number (PROJ-12, 34), {issue_number}, {slug} title as words-with-dashes, {line}, {machine} (coder), {source}, {repo}, {date} (or {yyyy} {mm} {dd}). E.g. feature/{issue}-{slug}. The factory cleans it into a legal git name and adds -<crate number> if it would clash. Only new crates use a changed pattern; a line can set its own in build mode.[/color]" % C_QUIET))


## Worktree cleanup: how hard the factory tries when it deletes a crate's worktree (a retry
## starting fresh, or the janitor clearing shipped and scrapped crates).
func _cleanup_section() -> void:
	var label := Label.new()
	label.text = "Worktree cleanup"
	label.theme_type_variation = &"LabelHeading"
	_content.add_child(label)
	var box := CheckBox.new()
	box.text = "Force delete worktrees"
	box.button_pressed = _force_delete
	box.tooltip_text = "Also delete locked worktrees, stop programs still running inside them (dev servers, watchers) and keep retrying files Windows holds open. Changes straight away."
	box.toggled.connect(func(on: bool) -> void:
		_force_delete = on
		force_delete_changed.emit(on))
	_content.add_child(box)
	_content.add_child(_text("[color=#%s]Off: a crate's worktree is removed with git worktree remove --force, and a folder Windows won't let go of is left for the janitor to try again. On: locked worktrees go too, and anything still running from inside the folder is stopped first. Uncommitted changes in a removed worktree are lost either way.[/color]" % C_QUIET))


func _section(service: String) -> void:
	var head := HBoxContainer.new()
	_content.add_child(head)
	var icon_path := "res://assets/ui/icon_%s.svg" % service
	if ResourceLoader.exists(icon_path):
		var icon := TextureRect.new()
		icon.texture = load(icon_path)
		icon.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
		icon.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
		icon.custom_minimum_size = Vector2(18, 18)
		icon.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		head.add_child(icon)
	var label := Label.new()
	label.text = "%s accounts" % SERVICES[service]
	label.theme_type_variation = &"LabelHeading"
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(label)
	var add := Button.new()
	add.theme_type_variation = &"ButtonGhost"
	add.icon = load("res://assets/ui/icon_add.svg")
	add.tooltip_text = "Add a %s account" % SERVICES[service]
	add.pressed.connect(func() -> void: _edit(service, ""))
	head.add_child(add)

	var accounts: Array = _last.get(service, [])
	var checked: Dictionary = (_last.get("check", {}) as Dictionary).get(service, {})
	# The default.
	var row := HBoxContainer.new()
	_content.add_child(row)
	var dl := Label.new()
	dl.text = "Default"
	dl.theme_type_variation = &"LabelMuted"
	row.add_child(dl)
	var pick := OptionButton.new()
	pick.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	pick.clip_text = true
	var ids: Array = [MACHINE]
	pick.add_item("This machine's sign-in")
	for a in accounts:
		ids.append(str(a.get("id", "")))
		pick.add_item(str(a.get("name", a.get("id", ""))))
	var current := str((_last.get("defaults", {}) as Dictionary).get(service, ""))
	pick.select(maxi(0, ids.find(current if not current.is_empty() else MACHINE)))
	pick.item_selected.connect(func(i: int) -> void: set_default.emit(service, str(ids[i])))
	row.add_child(pick)

	# This machine's sign-in, then each account.
	var machine_words := "gh auth login and git's credential manager" if service == "github" else \
			("JIRA_API_TOKEN, with factory.toml's site" if bool((_last.get("machine", {}) as Dictionary).get("jira_ready", false))
			else "not set up (no JIRA_API_TOKEN or site in factory.toml)")
	_content.add_child(_text("[b]This machine's sign-in[/b]  [color=#%s]%s[/color]%s"
			% [C_QUIET, machine_words, _status(checked.get(MACHINE, {}))]))
	if accounts.is_empty():
		_content.add_child(_text("[color=#%s]No %s accounts yet: press + to add one.[/color]" % [C_QUIET, SERVICES[service]]))
	for a in accounts:
		var aid := str(a.get("id", ""))
		var line := HBoxContainer.new()
		_content.add_child(line)
		var bits := PackedStringArray()
		if service == "jira":
			bits.append(_esc(str(a.get("email", "")) + (" on " + str(a.get("base_url", "")) if not str(a.get("base_url", "")).is_empty() else "")))
		elif not str(a.get("git_name", "")).is_empty():
			bits.append(_esc("commits as " + str(a.get("git_name", ""))))
		bits.append("token saved" if bool(a.get("token_set", false)) else "[color=#%s]no token[/color]" % C_RED)
		var t := _text("[b]%s[/b]  [color=#%s]%s[/color]%s" % [_esc(str(a.get("name", aid))), C_QUIET,
				"  ·  ".join(bits),
				_status(checked.get(aid, {}))])
		t.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		line.add_child(t)
		var edit := Button.new()
		edit.theme_type_variation = &"ButtonGhost"
		edit.icon = load("res://assets/ui/icon_name.svg")
		edit.tooltip_text = "Edit this account"
		edit.pressed.connect(func() -> void: _edit(service, aid))
		line.add_child(edit)
		var del := Button.new()
		del.theme_type_variation = &"ButtonGhost"
		del.icon = load("res://assets/ui/icon_delete.svg")
		del.tooltip_text = "Remove this account (lines and storages that picked it use the default)"
		del.pressed.connect(func() -> void: remove_account.emit(service, aid))
		line.add_child(del)
		if _editing.get("service", "") == service and _editing.get("id", "") == aid:
			_form(service, a)
	if _editing.get("service", "") == service and str(_editing.get("id", "")).is_empty():
		_form(service, {})


func _edit(service: String, aid: String) -> void:
	_editing = {"service": service, "id": aid}
	_render()


## The form for adding an account (acc empty) or changing one.
func _form(service: String, acc: Dictionary) -> void:
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 4)
	_content.add_child(box)
	_inputs = {}
	for f in FIELDS[service]:
		var key := str(f[0])
		var label := Label.new()
		label.text = str(f[1])
		label.theme_type_variation = &"LabelMuted"
		box.add_child(label)
		var edit := LineEdit.new()
		edit.secret = bool(f[3])
		edit.placeholder_text = str(f[2])
		if key == "token" and bool(acc.get("token_set", false)):
			edit.placeholder_text = "Saved (type a new one to replace it)"
		elif key != "token":
			edit.text = str(acc.get(key, ""))
		box.add_child(edit)
		_inputs[key] = edit
	var row := HBoxContainer.new()
	box.add_child(row)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(spacer)
	var cancel := Button.new()
	cancel.text = "Cancel"
	cancel.theme_type_variation = &"ButtonGhost"
	cancel.pressed.connect(func() -> void:
		_editing = {}
		_render())
	row.add_child(cancel)
	var save := Button.new()
	save.text = "Save" if not acc.is_empty() else "Add"
	save.theme_type_variation = &"ButtonPrimary"
	save.tooltip_text = "Save it and use it now (no restart needed), then check who it signs in as"
	save.pressed.connect(func() -> void:
		var out := {}
		if not acc.is_empty():
			out["id"] = str(acc.get("id", ""))
		for key in _inputs:
			var value := (_inputs[key] as LineEdit).text.strip_edges()
			if key == "token" and value.is_empty():
				continue  # blank keeps the saved one
			out[key] = value
		save.disabled = true
		_note = "[color=#%s]Saving and checking...[/color]" % C_QUIET
		save_account.emit(service, out))
	row.add_child(save)


func _status(c) -> String:
	if not c is Dictionary or c.is_empty():
		return ""
	var colour := C_GREEN if bool(c.get("ok", false)) else C_RED
	return "\n[color=#%s]%s[/color]" % [colour, _esc(str(c.get("message", "")))]


func _esc(text: String) -> String:
	return text.replace("[", "[lb]")


func _text(bbcode: String) -> RichTextLabel:
	var label := RichTextLabel.new()
	label.bbcode_enabled = true
	label.fit_content = true
	label.scroll_active = false
	label.selection_enabled = true
	label.text = bbcode
	return label
