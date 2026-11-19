extends Node2D
## Renders the backend's state and sends it your commands (specs, approve, reject, retry,
## and the pipelines you lay out in build mode).
## Nothing here decides where a crate goes; it only applies backend events.

const BackendClient := preload("res://scripts/backend_client.gd")
const FactoryFloor := preload("res://scripts/factory_floor.gd")
const Crate := preload("res://scripts/crate.gd")
const BuildPanel := preload("res://scripts/build_panel.gd")
const Inspector := preload("res://scripts/inspector.gd")
const IntakeView := preload("res://scripts/intake_view.gd")
const BinView := preload("res://scripts/bin_view.gd")
const StatsView := preload("res://scripts/stats_view.gd")
const AccountsView := preload("res://scripts/accounts_view.gd")
const ApprovalsView := preload("res://scripts/approvals_view.gd")
const DashboardView := preload("res://scripts/dashboard_view.gd")
const INTAKE_REFRESH := 20.0  # seconds between refreshes of an open intake window
const MACHINE_OUTPUT_LINES := 14

const BACKEND_URL := "ws://127.0.0.1:8765/ws"
const OLD_BACKEND := "The backend is an older version: stop it (Ctrl+C) and start it again"
const THEME_PATH := "res://assets/ui/theme/factory_theme.tres"
const LEFT_DOCK := 360.0  # machine status, intake list, bin list and build settings, one at a time
const BANNER_HEIGHT := 46.0
const GATE_ROW := 38.0  # the bottom dock grows by this while a crate waits for you
const LOG_HINT := "Log: click a crate to see its output"
const TOP_BAR := 44.0
const BOTTOM_PANEL := 180.0
const BUILD_PANEL := 320.0
const ZOOM_MIN := 0.2
const ZOOM_MAX := 2.0
const TOAST_SECONDS := 4.0
const SCRAP_WORDS := {"needs_info": "Needs info", "already_done": "Already done", "blocked": "Blocked",
		"work_failed": "Work failed", "rejected": "Rejected"}
const LOG_COLORS := {"header": "8CBFFF", "factory": "F2B84C", "fail": "E5484D", "command": "8A93A2"}

var client: BackendClient
var factory_floor: FactoryFloor
var crates_layer: Node2D
var machines_layer: Node2D
var build_panel: BuildPanel
var build_button: Button
var pause_button: Button  # pauses or resumes the whole factory
var pauses := {"factory": false, "lines": [], "machines": []}
var bottom_panel: PanelContainer
var building := false
var pending_save := false
var _save_wait := 0.0  # seconds the backend has taken to answer a save
const SAVE_PATIENCE := 20.0  # then the build panel says it's still waiting
var last_layout := {}
var crates := {}  # item_id -> Crate
var logs := {}  # item_id -> Array of log lines
var logs_partial := {}  # item_id -> true while only the end of its log is here (the snapshot's)
var costs := {}  # item_id -> what Claude has cost on it so far, in USD
var pr_urls := {}  # item_id -> pull request URL
var pr_states := {}  # item_id -> "merged", "open", "closed" or "unknown" (checked when it ships)
var selected_id := -1
var last_seq := 0

var camera: Camera2D
var manual_camera := false  # true once you zoom or pan; F fits the line again
var _panning := false
var _left_pan := false  # left button held on empty floor: a drag pans, a click closes windows
var _left_dragged := false
var _left_pan_from := Vector2.ZERO
const PAN_THRESHOLD := 6.0  # screen pixels before a left press on the floor becomes a pan
const KEY_PAN_SPEED := 900.0  # screen pixels per second for arrow keys / WASD
var hover_tip: PanelContainer
var hover_label: Label
var counters_label: Label
var toasts: VBoxContainer
var status_label: RichTextLabel
var log_title: Label
var log_view: RichTextLabel
var note_input: LineEdit
var approve_button: Button
var reject_button: Button
var scrap_button: Button
var retry_button: Button
var pr_button: Button
var agent_banner: PanelContainer
var gate_row: HBoxContainer
var log_subtitle: Label
var agent_label: Label
var agent_available := true
var agent_text := ""  # the Claude part of the banner, "" while Claude is fine
var tool_outages: Array = []  # broken tools from the backend (tool_status)
var banner_check: Button
var inspector: Inspector
var inspected := ""  # machine whose status window is open
var machine_runs := {}  # machine id -> {item_id, started, step, step_started, progress, failed, finished}
var machine_output := {}  # machine id -> its latest output lines
var machine_stats := {}  # machine id -> {passed, failed} since the game opened
var _inspect_tick := 0.0
var intake_view: IntakeView
var npc_layer: Node2D  # people on the floor (npc_layer.gd)
var sfx: Node  # sound effects (sfx.gd): listens to the backend's messages, M mutes
var viewed_intake := ""  # pipeline whose intake window is open
var viewed_store := ""  # or the storage (footprint key) whose window that is
var _store_tick := 0.0  # seconds since the storages' counts were last asked for
const STORE_REFRESH := 120.0
var _intake_tick := 0.0
var bin_view: BinView
var stats_view: StatsView
var stats_button: Button
var accounts_view: AccountsView
var accounts_button: Button
var approvals_view: ApprovalsView  # the holding bay: every crate waiting for you, in one list
var inbox_button: Button  # top bar: "N waiting for you", opens it
var _approvals_tick := 0.0
var dashboard_view: DashboardView  # everything at a glance, with controls, over the floor
var dashboard_button: Button
var crate_times := {}  # item_id -> when it was made (unix seconds), for the dashboard
var _bin_tick := 0.0
var viewed_bin := ""  # station key ("<pipeline>:scrap" or ":dock") whose crate list is open


func _ready() -> void:
	factory_floor = FactoryFloor.new()
	add_child(factory_floor)
	crates_layer = Node2D.new()
	add_child(crates_layer)
	# Machines draw above the crates, so a crate inside a machine is hidden.
	machines_layer = Node2D.new()
	add_child(machines_layer)
	factory_floor.attach_machine_layer(machines_layer)
	if ResourceLoader.exists("res://scripts/npc_layer.gd"):
		npc_layer = load("res://scripts/npc_layer.gd").new()
		npc_layer.factory_floor = factory_floor
		add_child(npc_layer)
	# Room walls, doors and belt ports (room_layer.gd): above the crates, under the machines.
	if ResourceLoader.exists("res://scripts/room_layer.gd"):
		var rooms: Node2D = load("res://scripts/room_layer.gd").new()
		rooms.factory_floor = factory_floor
		rooms.npc_layer = npc_layer
		add_child(rooms)
		move_child(rooms, crates_layer.get_index() + 1)

	camera = Camera2D.new()
	add_child(camera)
	camera.make_current()
	get_viewport().size_changed.connect(func() -> void: _fit_camera(true))

	_build_ui()

	client = BackendClient.new()
	client.url = BACKEND_URL
	client.connected.connect(_on_connected)
	client.disconnected.connect(_on_disconnected)
	client.message_received.connect(_on_message)
	if npc_layer:
		client.message_received.connect(npc_layer.on_message)
	if ResourceLoader.exists("res://scripts/sfx.gd"):
		sfx = load("res://scripts/sfx.gd").new()
		sfx.main = self
		add_child(sfx)
		client.message_received.connect(sfx.on_message)
	add_child(client)
	_set_status("[color=#80858F]●[/color] Connecting")


# UI ------------------------------------------------------------------------

func _build_ui() -> void:
	# The GUI theme (assets/ui/theme): set on the window so pop-ups and tooltips get it too.
	var layer := CanvasLayer.new()
	add_child(layer)
	# Every panel hangs off one full-screen Control that carries the theme (a theme doesn't
	# pass through a CanvasLayer). The window gets it too, for pop-up menus.
	var ui := Control.new()
	ui.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	ui.mouse_filter = Control.MOUSE_FILTER_IGNORE
	layer.add_child(ui)
	if ResourceLoader.exists(THEME_PATH):
		ui.theme = load(THEME_PATH)
		get_window().theme = ui.theme

	var top := PanelContainer.new()
	top.theme_type_variation = &"PanelBar"
	ui.add_child(top)
	top.set_anchors_and_offsets_preset(Control.PRESET_TOP_WIDE)
	var row := HBoxContainer.new()
	top.add_child(row)

	# Changes are described in a Custom storage's window (or the intake's), not up here.
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(spacer)

	counters_label = Label.new()
	counters_label.theme_type_variation = &"LabelMuted"
	counters_label.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	row.add_child(counters_label)

	# Every crate waiting for you, from every line, in one list (the holding bay).
	inbox_button = Button.new()
	inbox_button.icon = load("res://assets/ui/icon_human_approval.svg")
	inbox_button.theme_type_variation = &"ButtonGhost"
	inbox_button.focus_mode = Control.FOCUS_NONE
	inbox_button.tooltip_text = "Waiting for you: every crate waiting for your approval (or held at a machine), to approve or send back, one at a time or together"
	inbox_button.pressed.connect(func() -> void: _view_approvals(not approvals_view.visible))
	row.add_child(inbox_button)

	# Only the connection lives here; everything else is a pop-up.
	status_label = RichTextLabel.new()
	status_label.bbcode_enabled = true
	status_label.fit_content = true
	status_label.autowrap_mode = TextServer.AUTOWRAP_OFF
	status_label.scroll_active = false
	status_label.custom_minimum_size = Vector2(200, 0)
	status_label.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	row.add_child(status_label)

	pause_button = Button.new()  # an icon only, so the top bar still fits a small window
	pause_button.icon = load("res://assets/ui/icon_pause.svg")
	pause_button.theme_type_variation = &"ButtonGhost"
	pause_button.focus_mode = Control.FOCUS_NONE
	pause_button.pressed.connect(func() -> void:
		_send_pause("factory", "", not bool(pauses.get("factory", false))))
	row.add_child(pause_button)

	dashboard_button = Button.new()  # an icon too
	dashboard_button.icon = load("res://assets/ui/icon_dashboard.svg")
	dashboard_button.theme_type_variation = &"ButtonGhost"
	dashboard_button.focus_mode = Control.FOCUS_NONE
	dashboard_button.tooltip_text = "Dashboard: the whole factory at a glance, with tables, charts and controls"
	dashboard_button.pressed.connect(func() -> void: _view_dashboard(not dashboard_view.visible))
	row.add_child(dashboard_button)

	stats_button = Button.new()  # an icon too
	stats_button.icon = load("res://assets/ui/icon_stats.svg")
	stats_button.theme_type_variation = &"ButtonGhost"
	stats_button.focus_mode = Control.FOCUS_NONE
	stats_button.tooltip_text = "Factory stats: crates shipped, time, failures and Claude cost"
	stats_button.pressed.connect(func() -> void: _view_stats(not stats_view.visible))
	row.add_child(stats_button)

	accounts_button = Button.new()  # an icon too
	accounts_button.icon = load("res://assets/ui/icon_settings.svg")
	accounts_button.theme_type_variation = &"ButtonGhost"
	accounts_button.focus_mode = Control.FOCUS_NONE
	accounts_button.tooltip_text = "Settings: the GitHub and Jira accounts the factory works as, and worktree cleanup"
	accounts_button.pressed.connect(func() -> void: _view_accounts(not accounts_view.visible))
	row.add_child(accounts_button)

	build_button = Button.new()
	build_button.set_pressed_no_signal(false)
	build_button.icon = load("res://assets/ui/icon_build.svg")
	build_button.toggle_mode = true
	build_button.tooltip_text = "Build: lay out machines and belts"
	build_button.pressed.connect(_toggle_build)
	row.add_child(build_button)

	bottom_panel = PanelContainer.new()
	bottom_panel.theme_type_variation = &"PanelDockBottom"
	ui.add_child(bottom_panel)
	bottom_panel.set_anchors_and_offsets_preset(Control.PRESET_BOTTOM_WIDE)
	bottom_panel.offset_top = -BOTTOM_PANEL
	var col := VBoxContainer.new()
	bottom_panel.add_child(col)

	var header := HBoxContainer.new()
	col.add_child(header)
	log_title = Label.new()
	log_title.text = LOG_HINT
	log_title.theme_type_variation = &"LabelHeading"
	header.add_child(log_title)
	log_subtitle = Label.new()
	log_subtitle.theme_type_variation = &"LabelMuted"
	log_subtitle.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	log_subtitle.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	log_subtitle.clip_text = true
	header.add_child(log_subtitle)
	pr_button = _action_button(header, "", _open_pr)
	pr_button.tooltip_text = "Open PR: open this crate's pull request in your browser"
	pr_button.theme_type_variation = &"ButtonGhost"
	pr_button.icon = load("res://assets/ui/icon_pr.svg")
	var esc := Label.new()
	esc.text = "Esc"
	esc.theme_type_variation = &"LabelKey"
	esc.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	header.add_child(esc)
	var close := _action_button(header, "", func() -> void: _select(-1))
	close.theme_type_variation = &"ButtonGhost"
	close.icon = load("res://assets/ui/theme/icons/close.svg")
	close.tooltip_text = "Close the log (Esc)"

	# Only while the crate waits for you (or sits in the scrap bin): your note and the choices.
	gate_row = HBoxContainer.new()
	col.add_child(gate_row)
	note_input = LineEdit.new()
	note_input.placeholder_text = "Notes for the Coder (optional)"
	note_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	gate_row.add_child(note_input)
	approve_button = _action_button(gate_row, "", func() -> void: _send_action("approve"))
	approve_button.theme_type_variation = &"ButtonApprove"
	approve_button.icon = load("res://assets/ui/icon_check.svg")
	approve_button.tooltip_text = "Approve: let the crate carry on down the line, with your note"
	reject_button = _action_button(gate_row, "", func() -> void: _send_action("reject"))
	reject_button.theme_type_variation = &"ButtonReject"
	scrap_button = _action_button(gate_row, "", func() -> void: _send_action("scrap"))
	scrap_button.theme_type_variation = &"ButtonGhost"
	scrap_button.icon = load("res://assets/ui/icon_bin.svg")
	scrap_button.tooltip_text = "Scrap: throw the crate in the scrap bin"
	retry_button = _action_button(gate_row, "", func() -> void: _send_action("retry"))
	retry_button.icon = load("res://assets/ui/icon_retry.svg")
	_refresh_actions()

	log_view = RichTextLabel.new()
	log_view.theme_type_variation = &"LogText"
	log_view.scroll_following = true
	log_view.bbcode_enabled = true
	log_view.selection_enabled = true
	log_view.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(log_view)
	bottom_panel.visible = false  # shown while a crate is selected

	# Claude unavailable: a banner across the top until it's back.
	# One line under the top bar; the full detail is in its tooltip.
	agent_banner = PanelContainer.new()
	agent_banner.theme_type_variation = &"PanelBanner"
	ui.add_child(agent_banner)
	agent_banner.set_anchors_and_offsets_preset(Control.PRESET_TOP_WIDE)
	agent_banner.offset_top = TOP_BAR + 8
	agent_banner.offset_bottom = TOP_BAR + 8 + BANNER_HEIGHT
	agent_banner.offset_left = 12
	agent_banner.offset_right = -12
	var banner_row := HBoxContainer.new()
	agent_banner.add_child(banner_row)
	var warn := TextureRect.new()
	warn.texture = load("res://assets/ui/icon_warning.svg")
	warn.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	warn.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	warn.custom_minimum_size = Vector2(18, 18)
	warn.modulate = Color("F2B84C")
	warn.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	banner_row.add_child(warn)
	agent_label = Label.new()
	agent_label.clip_text = true
	agent_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	agent_label.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	agent_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	agent_label.mouse_filter = Control.MOUSE_FILTER_PASS
	banner_row.add_child(agent_label)
	banner_check = _action_button(banner_row, "", func() -> void:
		var sent := false
		if not agent_available:
			sent = client.send({"type": "check_agent"})
		if not tool_outages.is_empty():
			sent = client.send({"type": "check_tools"})
		if sent:
			_toast("Checking...", Color(0.8, 0.82, 0.86)))
	banner_check.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	banner_check.icon = load("res://assets/ui/icon_refresh.svg")
	agent_banner.visible = false

	# Status window for the machine you click (while the factory runs).
	inspector = Inspector.new()
	ui.add_child(inspector)
	inspector.position = Vector2(0, TOP_BAR)
	inspector.visible = false
	inspector.closed.connect(func() -> void: _inspect(""))
	inspector.open_crate.connect(func(item_id: int) -> void: _select(item_id))
	inspector.pause_toggled.connect(func(id: String, on: bool) -> void: _send_pause("machine", id, on))

	# What's waiting to come in at the intake you click: issues and PRs in its sources.
	intake_view = IntakeView.new()
	ui.add_child(intake_view)
	intake_view.position = Vector2(0, TOP_BAR)
	intake_view.visible = false
	intake_view.closed.connect(func() -> void: _view_intake(""))
	intake_view.open_crate.connect(func(item_id: int) -> void: _select(item_id))
	intake_view.pause_toggled.connect(func(pid: String, on: bool) -> void: _send_pause("line", pid, on))
	intake_view.storage_toggled.connect(_set_storage_enabled)
	intake_view.pull.connect(func(source: String, ref: String, storage: String) -> void:
		if not client.send({"type": "intake_pull", "pipeline": viewed_intake, "source": source, "ref": ref,
				"storage": storage}):
			intake_view.pull_failed()
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	intake_view.check.connect(func(source: String, storage: String) -> void:
		client.send({"type": "intake_check", "pipeline": viewed_intake, "source": source, "storage": storage}))
	intake_view.describe.connect(describe_change)
	intake_view.queue_change.connect(func(pid: String, msg: Dictionary) -> void:
		if not msg.is_empty() and not client.send(msg):
			_toast("Not connected. Is the backend running?", Color("E5484D"))
			return
		client.send({"type": "intake_list", "pipeline": pid}))
	# Closing issues and PRs in a storage (it asked you first): see intake_close in protocol/README.md.
	intake_view.close_options.connect(func(source: String, storage: String, refs: Array) -> void:
		client.send({"type": "intake_close_options", "pipeline": viewed_intake, "source": source, "storage": storage,
				"refs": refs}))
	intake_view.close_items.connect(func(source: String, storage: String, refs: Array, comment: String, reason: String,
			transition: String) -> void:
		if not client.send({"type": "intake_close", "pipeline": viewed_intake, "source": source, "storage": storage,
				"refs": refs, "comment": comment, "reason": reason, "transition": transition}):
			intake_view.pull_failed()
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	intake_view.submit.connect(func(pid: String, text: String) -> void:
		if client.send({"type": "submit_work", "text": text, "pipeline": pid}):
			_toast("PRD added to the %s" % _pipeline_name(pid), Color("5ABF66"))
		else:
			_toast("Not connected. Is the backend running?", Color("E5484D")))

	# The crates in a scrap bin or at a dock, as a list (on the floor they pile up).
	bin_view = BinView.new()
	ui.add_child(bin_view)
	bin_view.position = Vector2(0, TOP_BAR)
	bin_view.visible = false
	bin_view.closed.connect(func() -> void: _view_bin(""))
	bin_view.open_crate.connect(func(item_id: int) -> void: _select(item_id))
	bin_view.retry.connect(func(item_id: int) -> void:
		if not client.send({"type": "retry", "item_id": item_id, "note": ""}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))

	# The holding bay: every crate waiting for you (left dock).
	approvals_view = ApprovalsView.new()
	ui.add_child(approvals_view)
	approvals_view.position = Vector2(0, TOP_BAR)
	approvals_view.visible = false
	approvals_view.closed.connect(func() -> void: _view_approvals(false))
	approvals_view.open_crate.connect(func(item_id: int) -> void: _select(item_id))
	approvals_view.open_pr.connect(func(item_id: int) -> void:
		var url := str(pr_urls.get(item_id, ""))
		if not url.is_empty():
			OS.shell_open(url))
	approvals_view.decide.connect(_decide_many)

	# Throughput, failures and cost (left dock).
	stats_view = StatsView.new()
	ui.add_child(stats_view)
	stats_view.position = Vector2(0, TOP_BAR)
	stats_view.visible = false
	stats_view.closed.connect(func() -> void: _view_stats(false))
	stats_view.refresh.connect(func(days: int) -> void: client.send({"type": "stats", "days": days}))

	# The factory's own GitHub / Jira accounts (left dock).
	accounts_view = AccountsView.new()
	ui.add_child(accounts_view)
	accounts_view.position = Vector2(0, TOP_BAR)
	accounts_view.visible = false
	accounts_view.closed.connect(func() -> void: _view_accounts(false))
	accounts_view.save_account.connect(func(service: String, account: Dictionary) -> void:
		if not client.send({"type": "save_account", "service": service, "account": account}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	accounts_view.remove_account.connect(func(service: String, id: String) -> void:
		client.send({"type": "remove_account", "service": service, "id": id}))
	accounts_view.set_default.connect(func(service: String, id: String) -> void:
		client.send({"type": "set_default_account", "service": service, "id": id}))
	accounts_view.check.connect(func() -> void: client.send({"type": "check_accounts"}))
	accounts_view.agent_defaults_changed.connect(func(model: String, effort: String) -> void:
		if not client.send({"type": "agent_defaults", "model": model, "effort": effort}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	accounts_view.force_delete_changed.connect(func(on: bool) -> void:
		if not client.send({"type": "force_delete", "on": on}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	accounts_view.branch_pattern_changed.connect(func(pattern: String) -> void:
		if not client.send({"type": "branch_pattern", "pattern": pattern}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	accounts_view.repos_changed.connect(func(repos: Array) -> void:
		if not client.send({"type": "set_repos", "repos": repos}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))

	# The dashboard: everything at a glance, with controls (over the floor, under the top bar).
	dashboard_view = DashboardView.new()
	dashboard_view.main = self
	ui.add_child(dashboard_view)
	dashboard_view.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	dashboard_view.offset_top = TOP_BAR
	dashboard_view.visible = false
	dashboard_view.closed.connect(func() -> void: _view_dashboard(false))
	dashboard_view.show_crate.connect(func(item_id: int) -> void:
		_view_dashboard(false)
		_select(item_id))
	dashboard_view.inspect_machine.connect(func(machine_id: String) -> void:
		_view_dashboard(false)
		_inspect(machine_id))
	dashboard_view.open_storage.connect(func(pid: String, store_key: String) -> void:
		_view_dashboard(false)
		_view_intake(pid, store_key))
	dashboard_view.show_on_floor.connect(func(key: String) -> void:
		_view_dashboard(false)
		_show_on_floor(key))

	# Pop-ups for crates that need you, ship or are scrapped (top right, under the top bar).
	toasts = VBoxContainer.new()
	ui.add_child(toasts)
	toasts.set_anchors_and_offsets_preset(Control.PRESET_TOP_RIGHT)
	toasts.offset_left = -312
	toasts.offset_top = TOP_BAR + 8
	toasts.add_theme_constant_override("separation", 8)
	toasts.offset_right = -12
	toasts.mouse_filter = Control.MOUSE_FILTER_IGNORE

	hover_tip = PanelContainer.new()
	hover_tip.theme_type_variation = &"PanelTooltip"
	hover_tip.mouse_filter = Control.MOUSE_FILTER_IGNORE
	hover_tip.visible = false
	ui.add_child(hover_tip)
	hover_label = Label.new()
	hover_label.add_theme_font_size_override("font_size", 13)
	hover_label.custom_minimum_size = Vector2(260, 0)
	hover_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hover_tip.add_child(hover_label)

	build_panel = BuildPanel.new()
	ui.add_child(build_panel)
	build_panel.set_anchors_and_offsets_preset(Control.PRESET_RIGHT_WIDE)
	build_panel.offset_left = -BUILD_PANEL
	build_panel.offset_top = TOP_BAR
	build_panel.visible = false
	build_panel.save_requested.connect(_on_save_requested)
	build_panel.cancelled.connect(func() -> void: _close_build())
	build_panel.belt_speed_changed.connect(func(v: float) -> void:
		if not client.send({"type": "belt_speed", "speed": v}):
			_toast("Not connected. Is the backend running?", Color("E5484D")))
	build_panel.settings_opened.connect(_keep_clear_of_settings)
	_share_undo()
	build_panel.check_repo_requested.connect(func(pid: String, repo: Dictionary) -> void:
		if not client.send({"type": "check_repo", "pipeline": pid, "repo": repo}):
			build_panel.show_repo_check({"pipeline": pid, "ok": false, "message": "Not connected. Is the backend running?"}))


func _action_button(parent: Control, label: String, on_press: Callable) -> Button:
	var button := Button.new()
	button.text = label
	button.pressed.connect(on_press)
	parent.add_child(button)
	return button


## Show only the buttons that make sense for the selected crate.
func _refresh_actions() -> void:
	var state := ""
	var sends_back := false
	var redo_here := false  # the machine itself does it again (the Planner)
	if crates.has(selected_id):
		var crate: Crate = crates[selected_id]
		state = crate.state
		var red := factory_floor.on_fail(crate.location)
		sends_back = not red.is_empty() and not FactoryFloor.is_bin(red)
		redo_here = bool(_layout_machine(crate.location).get("rework_here", false))
	var waiting := state == "awaiting_approval"
	var held := state == "held"  # failed with nowhere to go: run it again or scrap it
	approve_button.visible = waiting
	# Where the machine sends failures back, Reject means "fix it" and Scrap throws it away.
	reject_button.visible = waiting
	reject_button.icon = load("res://assets/ui/icon_send_back.svg" if sends_back or redo_here else "res://assets/ui/icon_cross.svg")
	reject_button.tooltip_text = ("Send back: send it back to the Coder with your note" if sends_back
			else "Send back: have it done again, with your note" if redo_here
			else "Reject: close the PR and scrap the crate")
	scrap_button.visible = (waiting and (sends_back or redo_here)) or held
	note_input.placeholder_text = ("Notes for the %s (optional)" % str(_layout_machine(crates[selected_id].location).get("name", "machine"))
			if redo_here else "Notes for the Coder (optional)")
	if held:
		note_input.placeholder_text = "Notes for the %s when it runs again (optional)" % str(
				_layout_machine(crates[selected_id].location).get("name", "machine"))
	retry_button.visible = state == "scrapped" or held
	retry_button.tooltip_text = ("Run again: run this machine again on the crate, with your note" if held
			else "Retry: send the crate back to the start of the line")
	note_input.visible = state == "awaiting_approval" or state == "scrapped" or held
	gate_row.visible = note_input.visible
	pr_button.visible = str(pr_urls.get(selected_id, "")) != ""
	bottom_panel.offset_top = -(BOTTOM_PANEL + (GATE_ROW if gate_row.visible else 0.0))


func _send_action(kind: String) -> void:
	if not crates.has(selected_id):
		return
	var msg := {"type": kind, "item_id": selected_id, "note": note_input.text.strip_edges()}
	if client.send(msg):
		note_input.clear()
		_toast("Sent %s for crate #%d" % [kind, selected_id], Color(0.8, 0.82, 0.86))
	else:
		_toast("Not connected. Is the backend running?", Color("E5484D"))


func _open_pr() -> void:
	var url := str(pr_urls.get(selected_id, ""))
	if not url.is_empty():
		OS.shell_open(url)


## Tell each machine how many crates are waiting at it for you.
func _refresh_waiting() -> void:
	var counts := {}
	var in_bins := {}
	for id in crates:
		var crate: Crate = crates[id]
		if crate.state == "awaiting_approval" or crate.state == "held":
			counts[crate.location] = int(counts.get(crate.location, 0)) + 1
		elif (crate.state == "scrapped" and FactoryFloor.is_bin(crate.location)) or \
				(crate.state == "shipped" and FactoryFloor.is_dock(crate.location)):
			in_bins[crate.location] = int(in_bins.get(crate.location, 0)) + 1
	factory_floor.set_waiting(counts)
	factory_floor.set_bin_counts(in_bins)
	_refresh_held()
	_refresh_counters()


## Crates whose step is waiting on something outside (a review on GitHub), per machine.
func _refresh_held() -> void:
	var held := {}
	for id in crates:
		var crate: Crate = crates[id]
		if crate.state == "running" and not crate.waiting_for.is_empty():
			var reasons: Array = held.get(crate.location, [])
			reasons.append(crate.waiting_for)
			held[crate.location] = reasons
	factory_floor.set_held(held)


## Zoom and centre the camera so the whole factory fits between the top bar, the log panel
## and (in build mode) the build panel on the right.
func _fit_camera(force := false) -> void:
	if manual_camera and not force:
		return
	if force:
		manual_camera = false
	var view := get_viewport_rect().size
	var right := BUILD_PANEL if building else 0.0
	var left := LEFT_DOCK if _left_dock_open() else 0.0
	var bottom := -bottom_panel.offset_top if bottom_panel.visible else 0.0
	var content := factory_floor.world_rect()
	var zoom := minf(1.0, minf((view.x - right - left - 32.0) / content.size.x,
			(view.y - TOP_BAR - bottom - 16.0) / content.size.y))
	zoom = maxf(zoom, ZOOM_MIN)
	camera.zoom = Vector2(zoom, zoom)
	var screen_dx := left + (view.x - right - left) / 2.0 - view.x / 2.0
	var screen_dy := (TOP_BAR + (view.y - TOP_BAR - bottom) / 2.0) - view.y / 2.0
	camera.position = content.get_center() - Vector2(screen_dx, screen_dy) / zoom


# Build mode ------------------------------------------------------------------

func _toggle_build() -> void:
	if building:
		_close_build()
		return
	if last_layout.is_empty():
		_toast("Not connected. Is the backend running?", Color("E5484D"))
		return
	if not last_layout.has("pipelines"):
		_toast(OLD_BACKEND, Color("E5484D"))
		return
	_inspect("")
	_view_stats(false)
	_view_accounts(false)
	_view_intake("")
	_view_bin("")
	_view_approvals(false)
	building = true
	pending_save = false
	build_button.tooltip_text = "Close build: leave build mode"
	build_button.set_pressed_no_signal(true)
	build_panel.visible = true
	bottom_panel.visible = false
	crates_layer.visible = false
	_place_toasts()
	build_panel.open(last_layout.get("catalog", []), last_layout.get("pipelines", []),
			last_layout.get("sources", []), factory_floor, last_layout.get("default_repo", {}))
	_fit_camera(true)


func _close_build() -> void:
	building = false
	pending_save = false
	build_button.tooltip_text = "Build: lay out machines and belts"
	build_button.set_pressed_no_signal(false)
	build_panel.visible = false
	build_panel.close()
	bottom_panel.visible = crates.has(selected_id)
	crates_layer.visible = true
	_place_toasts()
	_show_layout()
	_fit_camera(true)


## Open or close the Stats window (one left dock at a time).
func _view_stats(show: bool) -> void:
	if show == stats_view.visible:
		return
	if show:
		if building:
			return
		_inspect("")
		_view_intake("")
		_view_bin("")
		_view_accounts(false)
		_view_approvals(false)
		stats_view.open()
	else:
		stats_view.visible = false
	stats_button.modulate = Color("5AB0FF") if show else Color.WHITE
	_layout_docks()


## The factory's repo list (snapshot or "repos" message): the gear's Repos section and the
## build panel's repo pickers.
func _set_repos(repos) -> void:
	var list: Array = repos if repos is Array else []
	last_layout["repos"] = list
	accounts_view.show_repos(list)
	build_panel.set_known_repos(list)


## Open or close the Accounts window (one left dock at a time).
func _view_accounts(show: bool) -> void:
	if show == accounts_view.visible:
		return
	if show:
		if building:
			return
		_inspect("")
		_view_intake("")
		_view_bin("")
		_view_stats(false)
		_view_approvals(false)
		accounts_view.open()
		client.send({"type": "get_accounts"})
	else:
		accounts_view.visible = false
	accounts_button.modulate = Color("5AB0FF") if show else Color.WHITE
	_layout_docks()


func _left_dock_open() -> bool:
	return inspector.visible or intake_view.visible or bin_view.visible or stats_view.visible or accounts_view.visible or \
			approvals_view.visible or \
			(building and build_panel.settings_window != null and build_panel.settings_window.visible)


## The left dock (one window at a time) runs full height under the top bar; the log dock
## and the banner start at its right edge.
func _layout_docks() -> void:
	var view := get_viewport_rect().size
	for dock in [inspector, intake_view, bin_view, stats_view, accounts_view, approvals_view]:
		if dock.visible:
			dock.position = Vector2(0, TOP_BAR)
			dock.size = Vector2(LEFT_DOCK, view.y - TOP_BAR)
	var left := LEFT_DOCK if _left_dock_open() else 0.0
	bottom_panel.offset_left = left
	agent_banner.offset_left = left + 12.0
	agent_banner.offset_right = -12.0 - (BUILD_PANEL if building else 0.0)
	_place_toasts()


## Pop-ups sit top right, left of the build panel while it is open.
func _place_toasts() -> void:
	var right := -12.0 - (BUILD_PANEL if building else 0.0)
	toasts.offset_right = right
	toasts.offset_left = right - 300.0
	toasts.offset_top = TOP_BAR + 8 + ((BANNER_HEIGHT + 8) if agent_banner.visible else 0.0)


## Slide the floor right if the thing you clicked is hidden under the settings window.
## A change you describe becomes a crate on that line (storage: a Custom storage's id, whose
## queue it joins; "" puts it on the intake). The Custom storage window and the dashboard use it.
func describe_change(pid: String, text: String, storage: String) -> bool:
	var msg := {"type": "submit_work", "text": text, "pipeline": pid}
	if not storage.is_empty():
		msg["storage"] = storage
	if client.send(msg):
		_toast(("Change queued for the %s" if not storage.is_empty() else "Change added to the %s") % _pipeline_name(pid),
				Color("5ABF66"))
		client.send({"type": "intake_list", "pipeline": pid})
		return true
	_toast("Not connected. Is the backend running?", Color("E5484D"))
	return false


## Switch a storage on or off (the storage window's and the dashboard's Turn on / Turn off).
func _set_storage_enabled(pid: String, storage: String, on: bool) -> void:
	if not client.send({"type": "storage_enabled", "pipeline": pid, "storage": storage, "enabled": on}):
		_toast("Not connected. Is the backend running?", Color("E5484D"))
	client.send({"type": "intake_list", "pipeline": pid})


## Pan to something on the floor ("<pid>:store_1", a machine id...) and highlight it.
func _show_on_floor(key: String) -> void:
	if not factory_floor.footprints.has(key):
		_toast("It isn't on the floor", Color("F5A524"))
		return
	manual_camera = true
	var world := (Vector2(factory_floor.footprints[key]) + Vector2.ONE) * FactoryFloor.CELL
	var tween := create_tween()
	tween.tween_property(camera, "position", world, 0.3)
	factory_floor.set_highlight(key)


func _keep_clear_of_settings(world: Vector2) -> void:
	var view := get_viewport_rect().size
	var screen_x := (world.x - camera.position.x) * camera.zoom.x + view.x / 2.0
	var clear := LEFT_DOCK + 80.0
	if screen_x < clear:
		var tween := create_tween()
		tween.tween_property(camera, "position:x", camera.position.x - (clear - screen_x) / camera.zoom.x, 0.25)


func _show_layout() -> void:
	factory_floor.set_pipelines(last_layout.get("pipelines", []).duplicate(true), last_layout.get("catalog", []))
	for crate in crates.values():
		crate.position = _target_for(crate.item_id, crate.location, crate.state, crate.came_back)


func _on_save_requested(pipelines: Array) -> void:
	if client.send({"type": "save_pipelines", "pipelines": pipelines}):
		pending_save = true
		_save_wait = 0.0
		build_panel.show_message("Saving...")
	else:
		build_panel.show_message("Not connected. Is the backend running?")


## Show or clear the "Claude is unavailable" banner and pause marks on its machines.
func _set_agent(status: Dictionary, from_snapshot: bool) -> void:
	var available := bool(status.get("available", true))
	factory_floor.set_agent_down(not available)
	agent_text = ""
	if not available:
		var reason := str(status.get("reason", ""))
		agent_text = ("Claude is unavailable. %s\nMachines that use Claude are paused and carry on by "
				+ "themselves when it's back. Checked %s.") % [reason, _clock(float(status.get("checked_at", 0.0)))]
		agent_label.tooltip_text = str(status.get("detail", ""))
		if agent_available and not from_snapshot:
			_toast("Claude is unavailable: machines that use it are paused", Color("E5484D"))
	elif not agent_available and not from_snapshot:
		_toast("Claude is back. The factory is running again", Color("5ABF66"))
	agent_available = available
	_update_banner()


## Broken tooling (gh signed out, no network, a crashed script...): crates that need it wait.
func _set_tools(outages: Array, from_snapshot: bool) -> void:
	if not from_snapshot:  # the banner says what broke; only say when it's fixed
		if outages.is_empty() and not tool_outages.is_empty():
			_toast("Fixed. The factory is running again", Color("5ABF66"))
	tool_outages = outages
	_update_banner()


func _update_banner() -> void:
	var parts := PackedStringArray()
	if not agent_text.is_empty():
		parts.append(agent_text)
	var manual := false
	var details := PackedStringArray()
	for o in tool_outages:
		parts.append("%s\nCrates that need %s wait at their machine and carry on once it's fixed (since %s)."
				% [str(o.get("reason", "")), str(o.get("label", "")), _clock(float(o.get("since", 0.0)))])
		manual = manual or not bool(o.get("can_check", true))
		details.append(str(o.get("detail", "")))
	agent_banner.visible = not parts.is_empty()
	# One line on the strip; everything, with the raw error, in its tooltip.
	agent_label.text = "  ·  ".join(parts).replace("\n", " ")
	agent_label.tooltip_text = "\n\n".join(parts) + ("\n\n" + "\n\n".join(details) if not tool_outages.is_empty() else "")
	banner_check.tooltip_text = ("Try again: check whether it's fixed now" if manual
			else "Check now: see whether Claude and the tools are back")


## A Unix time as the local time of day, e.g. "09:41".
func _clock(unix: float) -> String:
	if unix <= 0.0:
		return "just now"
	var bias := int(Time.get_time_zone_from_system().get("bias", 0)) * 60
	var t := Time.get_datetime_dict_from_unix_time(int(unix) + bias)
	return "%02d:%02d" % [int(t["hour"]), int(t["minute"])]


# Machine status window ----------------------------------------------------------

func _in_pile(location: String) -> bool:
	return FactoryFloor.is_bin(location) or FactoryFloor.is_dock(location)


func _view_bin(key: String) -> void:
	var opening := not key.is_empty() and key != viewed_bin
	if not key.is_empty():
		_inspect("")
		_view_intake("")
		_view_stats(false)
		_view_accounts(false)
		_view_approvals(false)
	viewed_bin = key if factory_floor.footprints.has(key) and not building else ""
	bin_view.visible = not viewed_bin.is_empty()
	if viewed_bin.is_empty():
		if _in_pile(factory_floor.highlighted):
			factory_floor.set_highlight("")
		return
	factory_floor.set_highlight(key)
	if opening:
		manual_camera = true
		_keep_clear_of_settings(Vector2(factory_floor.footprints[key]) * FactoryFloor.CELL)
	_refresh_bin()


## Rebuild the bin's list from the crates (the view only redraws when something changed).
func _refresh_bin() -> void:
	var scrap := FactoryFloor.is_bin(viewed_bin)
	var rows := []
	for crate in crates.values():
		if crate.location != viewed_bin:
			continue
		rows.append({"id": crate.item_id, "title": crate.text.get_slice("\n", 0).left(100),
				"source": crate.source, "retry": scrap,
				"detail": (_kind_words(crate.scrap_kind) + _claude_says(crate.item_id, _scrap_reason(crate.item_id))) if scrap
						else _ship_note(crate.item_id)})
	rows.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return int(a["id"]) > int(b["id"]))
	var name := _pipeline_name(viewed_bin.get_slice(":", 0))
	bin_view.show_crates(viewed_bin, "%s %s" % [name, "scrap bin" if scrap else "shipping dock"], rows,
			"Nothing in the scrap bin." if scrap else "Nothing shipped yet.")
	_layout_docks()


## Open or close the dashboard. It covers the floor, so it closes the left docks (and closes
## itself when one opens, or build mode starts).
func _view_dashboard(show: bool) -> void:
	if show == dashboard_view.visible:
		return
	if show:
		if building:
			return
		_inspect("")
		_view_intake("")
		_view_bin("")
		_view_stats(false)
		_view_accounts(false)
		_view_approvals(false)
		dashboard_view.open()
	else:
		dashboard_view.visible = false
	dashboard_button.modulate = Color("5AB0FF") if show else Color.WHITE


## Open or close the holding bay: every crate waiting for you (one left dock at a time).
func _view_approvals(show: bool) -> void:
	if show == approvals_view.visible:
		return
	if show:
		if building:
			return
		_inspect("")
		_view_intake("")
		_view_bin("")
		_view_stats(false)
		_view_accounts(false)
		approvals_view.open()
		_refresh_approvals()
	else:
		approvals_view.visible = false
	_refresh_counters()
	_layout_docks()


## Rebuild the holding bay's list from the crates (the view only redraws on a change).
func _refresh_approvals() -> void:
	var rows := []
	for crate in crates.values():
		if crate.state != "awaiting_approval" and crate.state != "held":
			continue
		var red := factory_floor.on_fail(crate.location)
		var machine := _layout_machine(crate.location)
		rows.append({"id": crate.item_id, "title": crate.text.get_slice("\n", 0).left(100), "state": crate.state,
				"at": _place_name(crate.location), "line": _line_name_of(crate.location), "source": crate.source,
				"risk": crate.risk, "has_pr": str(pr_urls.get(crate.item_id, "")) != "",
				"sends_back": (not red.is_empty() and not FactoryFloor.is_bin(red)) or bool(machine.get("rework_here", false)),
				"held_why": _scrap_reason(crate.item_id) if crate.state == "held" else ""})
	rows.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return int(a["id"]) < int(b["id"]))
	approvals_view.show_crates(rows)
	_layout_docks()


## The name of the line a machine is on ("" if there's only one line).
func _line_name_of(machine_id: String) -> String:
	var lines: Array = last_layout.get("pipelines", [])
	if lines.size() < 2:
		return ""
	for p in lines:
		for m in p.get("machines", []):
			if str(m.get("id", "")) == machine_id:
				return str(p.get("name", p.get("id", "")))
	return ""


## Decisions from the holding bay: one command per crate, each with the same note.
func _decide_many(item_ids: Array, kind: String, note: String) -> void:
	var sent := 0
	for id in item_ids:
		if client.send({"type": kind, "item_id": int(id), "note": note}):
			sent += 1
	if sent == 0:
		_toast("Not connected. Is the backend running?", Color("E5484D"))
		return
	var words := {"approve": "Approved", "reject": "Sent back", "retry": "Running again", "scrap": "Scrapped"}
	_toast("%s %s" % [words.get(kind, kind), ("crate #%d" % int(item_ids[0])) if sent == 1 else ("%d crates" % sent)],
			Color(0.8, 0.82, 0.86))


## Why a crate was scrapped, from its log: the failed step, a rejection, or running out of reworks.
func _scrap_reason(id: int) -> String:
	var lines: Array = logs.get(id, [])
	var fallback := "Scrapped"
	for i in range(lines.size() - 1, -1, -1):
		var line := str(lines[i])
		if line.begins_with("[factory] scrapped: ") and line.ends_with(")") and fallback == "Scrapped":
			fallback = line.substr(line.find("(") + 1).trim_suffix(")")  # "[factory] scrapped: Blocked (Coder: Commit)"
		if line.begins_with("[factory] step '") and "failed" in line:
			var step := line.get_slice("'", 1)
			var at := ""
			for j in range(i, -1, -1):  # the step header names the machine: "== Coder 5/5: Commit =="
				var header := str(lines[j])
				if header.begins_with("== ") and header.ends_with(": %s ==" % step):
					at = header.trim_prefix("== ").get_slice(" ", 0) + ": "
					break
			return "Failed at %s%s" % [at, step]
		if line.begins_with("[factory] rejected at"):
			return line.trim_prefix("[factory] ").capitalize().left(1) + line.trim_prefix("[factory] ").substr(1)
		if line.begins_with("[factory] refused"):
			return line.trim_prefix("[factory] ")
	return fallback


## The first line of Claude's explanation when it changed nothing, else `fallback`.
func _claude_says(id: int, fallback: String) -> String:
	var lines: Array = logs.get(id, [])
	for i in range(lines.size() - 1, -1, -1):
		if str(lines[i]) == "Its explanation:":
			for j in range(i + 1, lines.size()):
				var line := str(lines[j]).strip_edges()
				if line.is_empty() or line.to_lower().trim_prefix("**").begins_with("outcome"):
					continue
				return line.left(160)
	return fallback


func _kind_words(kind: String) -> String:
	var words := {"needs_info": "Needs info", "already_done": "Already done", "blocked": "Blocked",
			"work_failed": "Work failed", "rejected": "Rejected", "wrong_line": "Not for this line"}
	return ("%s: " % words[kind]) if words.has(kind) else ""


func _ship_note(id: int) -> String:
	if not pr_urls.has(id):
		return "Shipped"
	match str(pr_states.get(id, "")):
		"merged":
			return "PR merged"
		"open":
			return "Shipped with its PR still open (not merged)"
		"closed":
			return "Shipped, but its PR was closed without merging"
	return "Shipped with a PR (couldn't check if it merged)"


# Pausing ---------------------------------------------------------------------

func _set_belt_speed(speed: float) -> void:
	factory_floor.set_belt_speed(speed)
	build_panel.belt_speed_input.set_value_no_signal(speed)


func _send_pause(target: String, id: String, on: bool) -> void:
	if not client.send({"type": "pause", "target": target, "id": id, "paused": on}):
		_toast("Not connected. Is the backend running?", Color("E5484D"))
		return
	if on:
		return
	# Resuming doesn't start a line whose layout stops it: say so, or it looks like nothing happened.
	for pl in last_layout.get("pipelines", []):
		var why := str(pl.get("problem", ""))
		if why.is_empty() or (target == "line" and str(pl.get("id", "")) != id):
			continue
		_toast("%s still can't run: %s (fix it in build mode)" % [str(pl.get("name", "")), why.substr(why.find(": ") + 2) if ": " in why else why],
				Color("F5A524"))


## What's paused (the factory, lines, machines): the floor, the top bar and the open windows.
func _set_pauses(value) -> void:
	var p: Dictionary = value if value is Dictionary else {}
	pauses = {"factory": bool(p.get("factory", false)), "lines": p.get("lines", []), "machines": p.get("machines", [])}
	factory_floor.set_pauses(pauses)
	var all := bool(pauses["factory"])
	pause_button.icon = load("res://assets/ui/icon_play.svg" if all else "res://assets/ui/icon_pause.svg")
	pause_button.tooltip_text = ("Resume: the factory is paused. Start it again" if all else
			"Pause: stop the whole factory. Nothing new comes in, and crates wait on the belts until you resume it. Machines finish the crate inside.")
	pause_button.modulate = Color("5AB0FF") if all else Color.WHITE
	intake_view.set_paused(viewed_intake in pauses["lines"])
	_refresh_inspector()


func _view_intake(pid: String, store_key := "") -> void:
	var opening := not pid.is_empty() and (pid != viewed_intake or store_key != viewed_store)
	if not pid.is_empty():
		_inspect("")
		_view_bin("")
		_view_stats(false)
		_view_accounts(false)
		_view_approvals(false)
	var key := store_key if not store_key.is_empty() else "%s:intake" % pid
	viewed_intake = pid if factory_floor.footprints.has(key) and not building else ""
	viewed_store = store_key if not viewed_intake.is_empty() else ""
	intake_view.visible = not viewed_intake.is_empty()
	if viewed_intake.is_empty():
		if factory_floor.highlighted.ends_with(":intake") or FactoryFloor.is_storage(factory_floor.highlighted):
			factory_floor.set_highlight("")
		return
	var source := ""
	for st in FactoryFloor.storages_of(_layout_pipeline(pid)):
		if "%s:%s" % [pid, str(st.get("id", ""))] == store_key:
			source = str(st.get("source", ""))
	intake_view.only_source = source
	intake_view.only_storage = store_key.get_slice(":", 1) if not store_key.is_empty() else ""
	intake_view.store_name = "%s storage" % str(FactoryFloor.STORAGE_NAMES.get(source, ""))
	intake_view.set_paused(pid in pauses["lines"])
	factory_floor.set_highlight(key)
	if opening:
		manual_camera = true
		_keep_clear_of_settings(Vector2(factory_floor.footprints[key]) * FactoryFloor.CELL)
		intake_view.show_loading(viewed_intake, _pipeline_name(viewed_intake))
		_layout_intake_view()
		client.send({"type": "intake_list", "pipeline": viewed_intake})
		_intake_tick = 0.0


func _layout_intake_view() -> void:
	_layout_docks()


func _layout_pipeline(pid: String) -> Dictionary:
	for p in last_layout.get("pipelines", []):
		if str(p.get("id", "")) == pid:
			return p
	return {}


## From an intake listing: how many of each storage's issues or PRs aren't crates yet.
func _count_storages(msg: Dictionary) -> void:
	var pid := str(msg.get("pipeline", ""))
	var counts := factory_floor.storage_counts.duplicate()
	for st in FactoryFloor.storages_of(_layout_pipeline(pid)):
		var key := "%s:%s" % [pid, str(st.get("id", ""))]
		counts.erase(key)
		for s in msg.get("sources", []):
			var mine_ := str(s.get("storage", "")) == str(st.get("id", "")) if s.has("storage") \
					else str(s.get("source", "")) == str(st.get("source", ""))
			if mine_ and bool(s.get("enabled", false)) \
					and str(s.get("error", "")).is_empty() and FactoryFloor.fed_storages(_layout_pipeline(pid)).has(st):
				counts[key] = (s.get("items", []) as Array).filter(func(i) -> bool: return i.get("item_id") == null).size()
	factory_floor.set_storage_counts(counts)


## Ask every line with a storage what's in it, for the counts on the floor.
func _ask_storages() -> void:
	_store_tick = 0.0
	for p in last_layout.get("pipelines", []):
		if not FactoryFloor.fed_storages(p).is_empty():
			client.send({"type": "intake_list", "pipeline": str(p.get("id", ""))})


func _pipeline_name(pid: String) -> String:
	for p in last_layout.get("pipelines", []):
		if str(p.get("id", "")) == pid:
			return str(p.get("name", pid))
	return pid


func _inspect(id: String) -> void:
	var opening := not id.is_empty() and id != inspected
	if not id.is_empty() and not viewed_intake.is_empty():
		_view_intake("")
	if not id.is_empty() and not viewed_bin.is_empty():
		_view_bin("")
	if not id.is_empty() and stats_view.visible:
		_view_stats(false)
	if not id.is_empty() and accounts_view.visible:
		_view_accounts(false)
	if not id.is_empty() and approvals_view.visible:
		_view_approvals(false)
	inspected = id if factory_floor.machines.has(id) and not building else ""
	inspector.visible = not inspected.is_empty()
	if opening and not inspected.is_empty():
		manual_camera = true  # keep the floor where the slide leaves it
		_keep_clear_of_settings(Vector2(factory_floor.footprints[inspected]) * FactoryFloor.CELL)
	factory_floor.set_highlight(inspected)
	_refresh_inspector()


func _count_run(id: String, outcome: String) -> void:
	var stats: Dictionary = machine_stats.get(id, {"passed": 0, "failed": 0})
	stats[outcome] = int(stats.get(outcome, 0)) + 1
	machine_stats[id] = stats
	var run: Dictionary = machine_runs.get(id, {})
	if not run.is_empty():
		run["finished"] = outcome == "passed"
		run["failed"] = outcome == "failed"


func _refresh_inspector() -> void:
	if inspected.is_empty() or not factory_floor.machines.has(inspected):
		inspector.visible = false
		inspected = ""
		return
	inspector.show_status(_machine_status(inspected))
	inspector.set_paused(inspected in pauses["machines"], factory_floor.machine_paused_why(inspected))
	_layout_docks()


## The machine as placed (with its catalogue fields) from the last layout.
func _layout_machine(id: String) -> Dictionary:
	for p in last_layout.get("pipelines", []):
		for m in p.get("machines", []):
			if str(m.get("id", "")) == id:
				return m
	return {}


func _machine_status(id: String) -> Dictionary:
	var m: Dictionary = factory_floor.machines[id]
	var info := _layout_machine(id)
	var run: Dictionary = machine_runs.get(id, {})
	var now := Time.get_ticks_msec() / 1000.0
	var uses_agent := bool(m.get("uses_agent", false))
	var paused := not agent_available and uses_agent

	var working: Crate = null
	var held: Array = []  # crates here waiting on GitHub, not being worked on
	var approvals: Array = []
	var stuck: Array = []  # crates held here: failed with nowhere to go
	var queue: Array = []
	for crate in crates.values():
		if crate.location == id and crate.state == "running" and not crate.waiting_for.is_empty():
			held.append("#%d %s: %s" % [crate.item_id, crate.text.get_slice("\n", 0).left(50),
					crate.waiting_for.trim_prefix("Waiting for ")])
		elif crate.location == id and crate.state == "running" and working == null:
			working = crate
		elif crate.location == id and crate.state == "awaiting_approval":
			approvals.append("#%d %s" % [crate.item_id, crate.text.get_slice("\n", 0).left(60)])
		elif crate.location == id and crate.state == "held":
			stuck.append("#%d %s: %s" % [crate.item_id, crate.text.get_slice("\n", 0).left(40),
					crate.waiting_for.get_slice(" Held here: ", 0)])
		elif factory_floor.machine_ahead(crate.location) == id and crate.state not in ["shipped", "scrapped"]:
			queue.append("#%d %s" % [crate.item_id, crate.text.get_slice("\n", 0).left(60)])

	var data := {
		"id": id, "name": str(m.get("name", id)), "description": str(info.get("description", "")),
		"steps": info.get("steps", []), "gate": FactoryFloor.gated(m, info), "uses_agent": uses_agent,
		"parallel": bool(info.get("parallel", false)), "queue": queue, "approvals": approvals, "held": held, "stuck": stuck,
		"stats": machine_stats.get(id, {}), "auto_approve": str(m.get("auto_approve", "")), "max_rework": int(last_layout.get("max_rework", 0)),
		"output": machine_output.get(id, []), "elapsed": -1.0, "step_elapsed": -1.0,
	}
	var target := factory_floor.on_fail(id)
	if not target.is_empty() and factory_floor.machines.has(target) \
			and int(factory_floor.depth.get(target, 0)) > int(factory_floor.depth.get(id, 0)):
		data["on_fail"] = "on to %s, and on from there" % str(factory_floor.machines[target]["name"])
	elif not target.is_empty() and factory_floor.machines.has(target):
		data["on_fail"] = "back to %s for rework" % str(factory_floor.machines[target]["name"])
	elif FactoryFloor.is_dock(target):
		data["on_fail"] = "to the shipping dock"
	elif bool(info.get("rework_here", false)):
		data["on_fail"] = "back into this machine to try again"
	elif FactoryFloor.is_bin(target):
		var when = info.get("scrap_when", null)
		data["on_fail"] = "into the scrap bin" if when == null or (when as Array).size() >= SCRAP_WORDS.size() else \
				"into the scrap bin for %s; otherwise they wait here for you" % ", ".join(
				(when as Array).map(func(k) -> String: return str(SCRAP_WORDS.get(k, k))))
	else:
		data["on_fail"] = "nowhere: no red belt, so they wait here for you"

	var step := str(run.get("step", m.get("step", "")))
	var steps: Array = data["steps"]
	if working != null:
		data["crate"] = {"id": working.item_id, "title": working.text.get_slice("\n", 0), "loops": working.loops,
				"risk": working.risk, "risk_reason": working.risk_reason}
		if step == "Waiting for Claude":
			data["status"] = "Paused: waiting for Claude to come back"
			data["status_color"] = Inspector.C_AMBER
		else:
			data["status"] = "Working"
			data["status_color"] = Inspector.C_BLUE
		data["step_index"] = steps.find(step)
		data["progress"] = float(run.get("progress", m.get("progress", 0.0)))
		if not run.is_empty() and int(run["item_id"]) == working.item_id:
			data["elapsed"] = now - float(run["started"])
			data["step_elapsed"] = now - float(run["step_started"])
	elif not approvals.is_empty():
		var first: Crate = null
		for crate in crates.values():
			if crate.location == id and crate.state == "awaiting_approval":
				first = crate
				break
		data["crate"] = {"id": first.item_id, "title": first.text.get_slice("\n", 0), "loops": first.loops,
				"risk": first.risk, "risk_reason": first.risk_reason}
		data["status"] = "Done; waiting for you to approve or reject"
		data["status_color"] = Inspector.C_AMBER
		data["finished"] = true
	elif paused:
		data["status"] = "Paused until Claude is back"
		data["status_color"] = Inspector.C_AMBER
	elif bool(run.get("failed", false)) or str(m.get("state", "")) == "failed":
		data["status"] = "Idle. The last crate (#%d) failed at %s" % [int(run.get("item_id", 0)), step]
		data["status_color"] = Inspector.C_RED
		data["step_index"] = steps.find(step)
		data["failed"] = true
	elif bool(run.get("finished", false)):
		data["status"] = "Idle. The last crate (#%d) passed" % int(run.get("item_id", 0))
		data["status_color"] = Inspector.C_GREEN
		data["finished"] = true
	elif not held.is_empty():
		data["status"] = "Nothing ready; %d waiting on GitHub" % held.size()
		data["status_color"] = Inspector.C_AMBER
	else:
		data["status"] = "Idle"
		data["status_color"] = Inspector.C_QUIET
	if paused and working == null and not queue.is_empty():
		data["status"] = "Paused until Claude is back; %d crate%s waiting" % [queue.size(), "" if queue.size() == 1 else "s"]
	return data


func _set_status(text: String) -> void:
	status_label.text = text


func _input(event: InputEvent) -> void:
	# Space is for panning: don't let it press whichever button was clicked last.
	var key := event as InputEventKey
	if key and key.keycode == KEY_SPACE and get_viewport().gui_get_focus_owner() is BaseButton:
		get_viewport().gui_release_focus()
		get_viewport().set_input_as_handled()


func _unhandled_input(event: InputEvent) -> void:
	if _camera_input(event):
		get_viewport().set_input_as_handled()
		return
	if building:
		if build_panel.handle_floor_input(event, get_global_mouse_position()):
			get_viewport().set_input_as_handled()
		return
	var esc := event as InputEventKey
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and dashboard_view.visible:
		_view_dashboard(false)
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and stats_view.visible:
		_view_stats(false)
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and accounts_view.visible:
		_view_accounts(false)
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and approvals_view.visible:
		_view_approvals(false)
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and not viewed_bin.is_empty():
		_view_bin("")
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and not viewed_intake.is_empty():
		_view_intake("")
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and not inspected.is_empty():
		_inspect("")
		get_viewport().set_input_as_handled()
		return
	if esc and esc.pressed and esc.keycode == KEY_ESCAPE and selected_id != -1:
		_select(-1)
		get_viewport().set_input_as_handled()
		return
	var click := event as InputEventMouseButton
	if click == null or not click.pressed or click.button_index != MOUSE_BUTTON_LEFT:
		return
	var p := get_global_mouse_position()
	for id in crates:
		var crate: Crate = crates[id]
		# Crates in a bin or at a dock lie in a pile: clicking it opens their list instead.
		if crate.hit(p) and not _in_pile(crate.location):
			_select(id)
			get_viewport().set_input_as_handled()
			return
	# An intake: open what's waiting to come in. A machine: its status window. Empty floor
	# closes them.
	var node := factory_floor.node_at(p)
	if node.is_empty():
		for id in crates:  # a pile can spill a little past its station
			var crate: Crate = crates[id]
			if crate.hit(p) and _in_pile(crate.location):
				node = crate.location
				break
	if _in_pile(node):
		_view_bin(node)
		get_viewport().set_input_as_handled()
		return
	if not viewed_bin.is_empty():
		_view_bin("")
		get_viewport().set_input_as_handled()
	if node.ends_with(":intake"):
		_view_intake(node.get_slice(":", 0))
		get_viewport().set_input_as_handled()
		return
	if FactoryFloor.is_storage(node):
		_view_intake(node.get_slice(":", 0), node)
		get_viewport().set_input_as_handled()
		return
	if not viewed_intake.is_empty():
		_view_intake("")
		get_viewport().set_input_as_handled()
	var machine := factory_floor.machine_at(p)
	if not machine.is_empty() or not inspected.is_empty():
		_inspect(machine)
		get_viewport().set_input_as_handled()


## One Undo for the build panel and the room bar: room edits take their turn in the build
## panel's history, and both Undo buttons (and Ctrl+Z) step back through it.
func _share_undo() -> void:
	var rooms: Node = null
	for child in get_children():
		if child.has_method("set_tool") and "floors" in child:
			rooms = child
	if rooms == null:
		return
	build_panel.rooms_undo = Callable(rooms, "undo")
	if rooms.has_signal("undo_pushed"):
		build_panel.rooms_tracked = true
		rooms.connect("undo_pushed", build_panel.note_room_edit)
	if rooms.has_signal("undo_cleared"):
		rooms.connect("undo_cleared", build_panel.forget_room_edits)
	if "undo_router" in rooms:
		rooms.set("undo_router", build_panel.undo)
	if "can_undo_check" in rooms:
		rooms.set("can_undo_check", build_panel.can_undo)
	if rooms.has_method("refresh_undo"):
		build_panel.undo_changed.connect(Callable(rooms, "refresh_undo"))


## Scroll to zoom (towards the mouse), drag with the right or middle button (or the left one
## on empty floor, or anywhere while holding Space) to pan, F to fit.
func _camera_input(event: InputEvent) -> bool:
	var key := event as InputEventKey
	if key and key.pressed and not key.echo and key.keycode == KEY_F:
		if get_viewport().gui_get_focus_owner() is LineEdit or get_viewport().gui_get_focus_owner() is TextEdit:
			return false
		_fit_camera(true)
		return true
	var button := event as InputEventMouseButton
	if button:
		if button.button_index in [MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE]:
			_panning = button.pressed
			return true
		# Left-drag on empty floor pans too. In build mode only where there's nothing to grab
		# (no machine, port or belt); holding Space makes any left-drag pan.
		if button.button_index == MOUSE_BUTTON_LEFT:
			if button.pressed:
				# A click on the floor leaves any text box, so the arrow keys and WASD pan again.
				get_viewport().gui_release_focus()
				var at := get_global_mouse_position()
				var space := Input.is_physical_key_pressed(KEY_SPACE) and not _typing()
				var empty := build_panel.is_empty_floor(at) if building else \
						(factory_floor.node_at(at).is_empty() and not _crate_at(at))
				if space or empty:
					_left_pan = true
					_left_dragged = space  # with Space held, a click never selects
					_left_pan_from = button.position
					return true  # a click without a drag is handled on release
			elif _left_pan:
				_left_pan = false
				if not _left_dragged:
					if building:
						build_panel.click_empty_floor()
					else:
						_click_empty_floor()
				return true
		if button.pressed and button.button_index in [MOUSE_BUTTON_WHEEL_UP, MOUSE_BUTTON_WHEEL_DOWN]:
			if get_viewport().gui_get_hovered_control() != null:
				return true  # over a panel: a scroll that ran past its end mustn't zoom the floor
			var factor := 1.1 if button.button_index == MOUSE_BUTTON_WHEEL_UP else 1.0 / 1.1
			var before := _mouse_world_at(button.position)
			var z := clampf(camera.zoom.x * factor, ZOOM_MIN, ZOOM_MAX)
			camera.zoom = Vector2(z, z)
			# Keep the point under the mouse where it was.
			camera.position += before - _mouse_world_at(button.position)
			manual_camera = true
			return true
	var motion := event as InputEventMouseMotion
	if motion and _left_pan:
		if not _left_dragged and motion.position.distance_to(_left_pan_from) < PAN_THRESHOLD:
			return false
		_left_dragged = true
		camera.position -= motion.relative / camera.zoom.x
		manual_camera = true
		return true
	if motion and _panning:
		camera.position -= motion.relative / camera.zoom.x
		manual_camera = true
		return true
	return false


func _typing() -> bool:
	var focus := get_viewport().gui_get_focus_owner()
	return focus is LineEdit or focus is TextEdit


func _crate_at(world: Vector2) -> bool:
	for crate in crates.values():
		if crate.hit(world):
			return true
	return false


## A click on empty floor (no drag): close whichever window is open.
func _click_empty_floor() -> void:
	if not viewed_bin.is_empty():
		_view_bin("")
	if not viewed_intake.is_empty():
		_view_intake("")
	if not inspected.is_empty():
		_inspect("")


## Arrow keys or WASD pan the floor, unless you're typing.
func _key_pan(delta: float) -> void:
	var focus := get_viewport().gui_get_focus_owner()
	if focus is LineEdit or focus is TextEdit:
		return
	var dir := Vector2.ZERO
	if Input.is_physical_key_pressed(KEY_LEFT) or Input.is_physical_key_pressed(KEY_A):
		dir.x -= 1
	if Input.is_physical_key_pressed(KEY_RIGHT) or Input.is_physical_key_pressed(KEY_D):
		dir.x += 1
	if Input.is_physical_key_pressed(KEY_UP) or Input.is_physical_key_pressed(KEY_W):
		dir.y -= 1
	if Input.is_physical_key_pressed(KEY_DOWN) or Input.is_physical_key_pressed(KEY_S):
		dir.y += 1
	if dir != Vector2.ZERO:
		camera.position += dir.normalized() * KEY_PAN_SPEED * delta / camera.zoom.x
		manual_camera = true


func _mouse_world_at(screen: Vector2) -> Vector2:
	return camera.position + (screen - get_viewport_rect().size / 2.0) / camera.zoom.x


# Backend events --------------------------------------------------------------

func _on_connected() -> void:
	_set_status("[color=#5ABF66]●[/color] Connected")
	client.send({"type": "get_accounts"})  # the build panel's account pickers need them


func _on_disconnected() -> void:
	_set_status("[color=#E5484D]●[/color] Backend offline, retrying")


func _on_message(msg: Dictionary) -> void:
	var kind: String = msg.get("type", "")
	if dashboard_view:
		dashboard_view.on_message(msg)
	if kind == "snapshot":
		_apply_snapshot(msg)
		return
	if kind == "agent_status":
		_set_agent(msg, false)
		return
	if kind == "tool_status":
		_set_tools(msg.get("outages", []), false)
		return
	if kind == "pauses":
		_set_pauses(msg.get("pauses", {}))
		return
	if kind == "belt_speed":
		_set_belt_speed(float(msg.get("speed", 1.5)))
		return
	if kind == "agent_defaults":
		accounts_view.show_agent_defaults(msg)
		build_panel.set_agent_settings(msg)
		return
	if kind == "force_delete":
		accounts_view.show_force_delete(bool(msg.get("on", false)))
		return
	if kind == "branch_pattern":
		accounts_view.show_branch_pattern(str(msg.get("pattern", "")))
		build_panel.factory_branch_pattern = str(msg.get("pattern", ""))
		_toast("Branch naming saved: new crates' branches look like %s" % AccountsView.branch_preview(str(msg.get("pattern", ""))), Color("5ABF66"))
		return
	if kind == "repos":
		_set_repos(msg.get("repos", []))
		return
	if kind == "repo_check":
		build_panel.show_repo_check(msg)
		return
	if kind == "stats":
		stats_view.show_stats(msg)
		return
	if kind == "accounts":
		accounts_view.show_accounts(msg)
		build_panel.set_accounts(msg)  # for the account pickers in build mode
		return
	if kind == "item_log":
		_got_full_log(int(msg.get("item_id", -1)), msg.get("log", []))
		return
	if kind == "intake_list":
		_count_storages(msg)
		if str(msg.get("pipeline", "")) == viewed_intake and intake_view.visible:
			intake_view.show_listing(msg, _pipeline_name(viewed_intake))
			_layout_intake_view()
		return
	if kind == "intake_closed":
		intake_view.close_done(msg)
		var shut: Array = msg.get("closed", [])
		var failed: Array = msg.get("failed", [])
		if not shut.is_empty():
			_toast("Closed %s" % ", ".join(PackedStringArray(shut.map(func(r) -> String: return _short_ref(str(r))))),
					Color("5ABF66"))
		for f in failed:
			_toast("Couldn't close %s: %s" % [_short_ref(str(f.get("ref", ""))), str(f.get("error", ""))], Color("E5484D"))
		return
	if kind == "intake_close_options":
		intake_view.show_close_options(msg)
		if (msg.get("transitions", []) as Array).is_empty():
			_toast("That Jira issue can't move anywhere right now", Color("E5484D"))
		return
	if kind == "error":
		if intake_view.visible:
			intake_view.pull_failed()
		var text := str(msg.get("message", ""))
		if not (building and pending_save):
			_toast(text.substr(0, 1).to_upper() + text.substr(1), Color("E5484D"))
		if building and pending_save:
			pending_save = false
			build_panel.show_message("Not saved: %s" % text)
		return

	var seq := int(msg.get("seq", 0))
	if seq <= last_seq:
		return
	last_seq = seq

	match kind:
		"item_created":
			_add_item(msg["item"])
			_ride_from_storage(msg["item"])
			if intake_view.visible:
				_intake_tick = INTAKE_REFRESH - 1.5  # show it as a crate soon
		"item_moved":
			_on_item_moved(msg)
		"job_started":
			factory_floor.set_machine_state(str(msg.get("machine", "")), "running", 0.0)
			var now := Time.get_ticks_msec() / 1000.0
			machine_runs[str(msg.get("machine", ""))] = {"item_id": int(msg["item_id"]), "started": now,
					"step": "", "step_started": now, "progress": 0.0, "failed": false, "finished": false}
			machine_output[str(msg.get("machine", ""))] = []
		"job_log":
			_append_log(int(msg["item_id"]), str(msg.get("line", "")))
			var lines: Array = machine_output.get(str(msg.get("machine", "")), [])
			lines.append(str(msg.get("line", "")))
			machine_output[str(msg.get("machine", ""))] = lines.slice(-MACHINE_OUTPUT_LINES)
		"job_progress":
			factory_floor.set_machine_state(str(msg.get("machine", "")), "running",
					float(msg.get("progress", 0.0)), str(msg.get("step", "")))
			var run: Dictionary = machine_runs.get(str(msg.get("machine", "")), {})
			if not run.is_empty():
				var step := str(msg.get("step", ""))
				if not step.is_empty() and step != run["step"]:
					run["step"] = step
					run["step_started"] = Time.get_ticks_msec() / 1000.0
				run["progress"] = float(msg.get("progress", 0.0))
		"job_finished":
			factory_floor.set_machine_state(str(msg.get("machine", "")), "done", 1.0)
			_count_run(str(msg.get("machine", "")), "passed")
		"job_failed":
			var machine_id := str(msg.get("machine", ""))
			factory_floor.set_machine_state(machine_id, "failed",
					factory_floor.machine_progress(machine_id), str(msg.get("step", "")))
			_count_run(machine_id, "failed")
		"item_triaged":
			var tid := int(msg["item_id"])
			if crates.has(tid):
				crates[tid].scrap_kind = str(msg.get("kind", ""))
		"item_held":
			var hid := int(msg["item_id"])
			if crates.has(hid):
				crates[hid].waiting_for = str(msg.get("reason", ""))
				_toast("Crate #%d is held at %s: %s" % [hid, _place_name(str(msg.get("machine", ""))),
						str(msg.get("reason", "")).get_slice(" Held here: ", 1).trim_suffix(".")], Color("E5484D"))
				_refresh_waiting()
				if hid == selected_id:
					_select(hid)
		"item_waiting":
			var wid := int(msg["item_id"])
			if crates.has(wid):
				crates[wid].waiting_for = str(msg.get("reason", ""))
				_refresh_held()
		"item_risk":
			var id := int(msg["item_id"])
			if crates.has(id):
				var crate: Crate = crates[id]
				crate.set_risk(str(msg.get("risk", "")), str(msg.get("reason", "")))
				if id == selected_id:
					_select(id)
			for mid in machine_runs:  # the machine that stamped it shows the decision
				if int(machine_runs[mid].get("item_id", -1)) == id:
					factory_floor.set_machine_result(str(mid), str(msg.get("risk", "")))
		"pr_state":
			pr_states[int(msg["item_id"])] = str(msg.get("state", ""))
		"item_cost":
			costs[int(msg["item_id"])] = float(msg.get("cost_usd", 0.0))
			if int(msg["item_id"]) == selected_id:
				_select_subtitle(selected_id)
		"budget_spent":
			_toast("Today's Claude budget ($%.2f) is spent: crates wait in front of Claude machines until midnight."
					% float(msg.get("daily_usd", 0.0)), Color("F5A524"))
		"pr_opened":
			pr_urls[int(msg["item_id"])] = str(msg.get("url", ""))
			if crates.has(int(msg["item_id"])):
				crates[int(msg["item_id"])].has_pr = true
			_toast("PR opened for crate #%d" % int(msg["item_id"]), Color("8CBFFF"))
			_refresh_actions()


func _apply_snapshot(snap: Dictionary) -> void:
	for crate in crates.values():
		crate.queue_free()
	crates.clear()
	logs.clear()
	logs_partial.clear()
	costs.clear()
	pr_urls.clear()
	pr_states.clear()
	last_seq = int(snap.get("seq", 0))

	var layout: Dictionary = snap.get("layout", {})
	last_layout = layout
	_set_pauses(snap.get("pauses", {}))
	if layout.has("belt_speed"):
		_set_belt_speed(float(layout["belt_speed"]))
	accounts_view.show_force_delete(bool(layout.get("force_delete", false)))
	accounts_view.show_branch_pattern(str(layout.get("branch_pattern", "")))
	build_panel.factory_branch_pattern = str(layout.get("branch_pattern", ""))
	var agent_defaults: Dictionary = layout.get("agent_defaults", {}) if layout.get("agent_defaults") is Dictionary else {}
	var agent_choices: Dictionary = layout.get("agent_choices", {}) if layout.get("agent_choices") is Dictionary else {}
	accounts_view.show_agent_defaults(agent_defaults, agent_choices)
	build_panel.set_agent_settings(agent_defaults, agent_choices)
	_set_repos(layout.get("repos", []))
	_store_tick = STORE_REFRESH - 1.0  # the storages' counts, in a moment
	_set_agent(snap.get("agent", {}), true)
	_set_tools(snap.get("tools", []), true)
	if not layout.has("pipelines"):
		_toast(OLD_BACKEND, Color("E5484D"))
	if building and pending_save:
		# The backend accepted the pipelines and sent them back: keep building on what it saved.
		pending_save = false
		build_panel.reload(layout.get("pipelines", []))
		_toast("Factory saved", Color("5ABF66"))
	elif not building:
		_show_layout()
		_fit_camera()

	for item in snap.get("items", []):
		_add_item(item)
		if str(item.get("state", "")) == "running":
			factory_floor.set_machine_state(str(item.get("location", "")), "running", 0.0)
	_select(selected_id if crates.has(selected_id) else -1)
	_refresh_waiting()


func _add_item(item: Dictionary) -> void:
	var id := int(item["id"])
	var crate := Crate.new()
	crate.item_id = id
	crate.text = str(item.get("text", ""))
	crate.source = _source_label(item)
	crate.set_state(str(item.get("state", "queued")))
	crate.location = str(item.get("location", ""))
	crate.loops = int(item.get("loops", 0))
	crate.set_risk(str(item.get("risk", "")), str(item.get("risk_reason", "")))
	crate.scrap_kind = str(item.get("scrap_kind", ""))
	crate.waiting_for = str(item.get("waiting_for", ""))
	crate.position = _target_for(id, crate.location, crate.state)
	crate.visible = not crate.state in Crate.OFF_FLOOR  # in the bin, at the dock or at a gate
	if str(item.get("pr_url", "")) != "":
		pr_urls[id] = str(item["pr_url"])
		crate.has_pr = true
	if str(item.get("pr_state", "")) != "":
		pr_states[id] = str(item["pr_state"])
	crates_layer.add_child(crate)
	crates[id] = crate
	crate_times[id] = float(item.get("created_at", 0.0))
	var lines: Array = []
	for line in item.get("log", []):
		lines.append(str(line))
	logs[id] = lines
	if int(item.get("log_lines", lines.size())) > lines.size():
		logs_partial[id] = true  # the rest comes when you open the crate
	else:
		logs_partial.erase(id)
	costs[id] = float(item.get("cost_usd", 0.0))


## A new crate out of a storage rides its belt from the storage to the intake, at the belt's
## own speed, instead of appearing there (moves that come in meanwhile wait their turn).
func _ride_from_storage(item: Dictionary) -> void:
	var id := int(item["id"])
	var storage := str(item.get("storage", ""))
	if storage.is_empty() or not crates.has(id):
		return
	var crate: Crate = crates[id]
	if not crate.location.ends_with(":intake") or not crate.visible:
		return
	var path: Array = factory_floor.storage_path(crate.location.get_slice(":", 0), storage)
	if path.size() < 2:
		return
	var target := crate.position
	crate.position = path[0]
	path = path.slice(1)
	path.append(target)
	var length := 0.0
	var at := crate.position
	for p in path:
		length += at.distance_to(p)
		at = p
	crate.move_path(path, length / maxf(factory_floor.belt_fps * 2.0, 1.0))


func _on_item_moved(msg: Dictionary) -> void:
	var id := int(msg["item_id"])
	if not crates.has(id):
		return
	var crate: Crate = crates[id]
	var from := crate.location
	var was_returning := _is_return(from)
	if msg.has("state"):
		crate.set_state(str(msg["state"]))
	crate.location = str(msg.get("to", ""))
	if _is_return(crate.location) and not was_returning and not FactoryFloor.is_bin(factory_floor.on_fail(from)):
		crate.loops += 1
		if id == selected_id:
			_select(id)
	crate.came_back = was_returning and ":belt_" in crate.location
	# Along the belts the floor laid out; the last point is where the crate settles.
	var path: Array = factory_floor.waypoints(from, crate.location, crate.position)
	path[-1] = _target_for(id, crate.location, crate.state, crate.came_back)
	if FactoryFloor.is_bin(crate.location) and was_returning:
		# Along the red belt into the bin; it leaves the floor as it arrives.
		crate.move_path(path, float(msg.get("duration", 0.0)))
	elif FactoryFloor.is_bin(crate.location):
		# Scrapped where it stood (you pressed Scrap): it drops out of sight, and the bin shows
		# it landing.
		crate.vanish()
	elif bool(msg.get("carried", false)):
		# Not this machine's work: a worker carries it straight across to the right machine.
		crate.move_path([path[-1]], float(msg.get("duration", 0.0)))
		_toast("A worker carries Crate #%d to the %s" % [id, _place_name(str(msg.get("machine", "")))],
				Color("7FB2E5"))
	elif FactoryFloor.is_bin(from):
		crate.move_path(path, 0.0)  # retried: it reappears at the intake, not flying out of the bin
	else:
		var duration := float(msg.get("duration", 0.0))
		if duration > 0.0 and ":belt_" in crate.location:
			# Ride at the belt's own speed, so it sits still on the moving chevrons.
			var length := 0.0
			var at := crate.position
			for p in path:
				length += at.distance_to(p)
				at = p
			duration = length / (factory_floor.belt_fps * 2.0)
		crate.move_path(path, duration)
	if ":belt_" in from and from != crate.location:
		# The crates queued behind it on that belt move up.
		for other in crates.values():
			if other != crate and other.location == from and not other.came_back and not other.is_moving():
				other.move_path([_target_for(other.item_id, from, other.state)], 0.4)
	_toast_for_move(crate, msg)
	_refresh_waiting()
	if id == selected_id:
		_refresh_actions()


func _is_return(location: String) -> bool:
	return ":return_" in location


func _toast_for_move(crate: Crate, msg: Dictionary) -> void:
	var state := str(msg.get("state", ""))
	var name := "Crate #%d" % crate.item_id
	match state:
		"awaiting_approval":
			_toast("%s is waiting for you at %s" % [name, _place_name(crate.location)], Color("F2B84C"))
		"shipped":
			_toast("%s shipped" % name, Color("5ABF66"))
		"scrapped":
			_toast("%s went to the scrap bin" % name, Color("E5484D"))


## A PR url as owner/name#n, for toasts; issue numbers and Jira keys as they are.
func _short_ref(ref: String) -> String:
	if ref.begins_with("https://github.com/"):
		var bits := ref.trim_prefix("https://github.com/").split("/")
		if bits.size() >= 4:
			return "%s/%s#%s" % [bits[0], bits[1], bits[3]]
	return ("#" + ref) if ref.is_valid_int() else ref


func _toast(text: String, col: Color) -> void:
	var panel := PanelContainer.new()
	panel.mouse_filter = Control.MOUSE_FILTER_IGNORE
	# The coloured bar on the left carries the state; the text stays plain.
	match col.to_html(false).to_upper():
		"E5484D":
			panel.theme_type_variation = &"PanelToastError"
		"F2B84C":
			panel.theme_type_variation = &"PanelToastWarn"
		"5ABF66":
			panel.theme_type_variation = &"PanelToastSuccess"
		_:
			panel.theme_type_variation = &"PanelToast"
	var label := Label.new()
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	panel.add_child(label)
	toasts.add_child(panel)
	while toasts.get_child_count() > 4:
		toasts.get_child(0).free()
	var tween := panel.create_tween()
	tween.tween_interval(TOAST_SECONDS)
	tween.tween_property(panel, "modulate:a", 0.0, 0.6)
	tween.tween_callback(panel.queue_free)


## A location as a person would say it: a machine's name, "the intake", "a belt" and so on.
func _place_name(location: String) -> String:
	if factory_floor.machines.has(location):
		return str(factory_floor.machines[location]["name"])
	var place := location.get_slice(":", 1) if ":" in location else location
	if place.begins_with("belt_"):
		return "a belt"
	if place.begins_with("return_"):
		return "the rework belt"
	if FactoryFloor.is_bin(place):
		return "the scrap bin"
	if FactoryFloor.is_dock(place):
		return "the shipping dock"
	match place:
		"intake":
			return "the intake"
		"dock":
			return "the shipping dock"
		"scrap":
			return "the scrap bin"
	return location


const STATE_WORDS := {
	"queued": "queued", "waiting": "waiting for the next machine", "running": "being worked on",
	"awaiting_approval": "waiting for you", "held": "held at the machine for you", "moving": "on the move", "shipped": "shipped",
	"scrapped": "scrapped",
}


func _update_hover() -> void:
	var p := get_global_mouse_position()
	var found: Crate = null
	if not building and not dashboard_view.visible:
		for crate in crates.values():
			if crate.visible and crate.hit(p):
				found = crate
	hover_tip.visible = found != null
	if found == null:
		return
	var lines := PackedStringArray()
	var head := "Crate #%d" % found.item_id
	if not found.source.is_empty():
		head += "  (%s)" % found.source
	lines.append(head)
	lines.append(found.text.get_slice("\n", 0).left(120))
	var where := str(STATE_WORDS.get(found.state, found.state))
	if found.state not in ["shipped", "scrapped"]:
		where += " at " + _place_name(found.location)
	lines.append(where.substr(0, 1).to_upper() + where.substr(1))
	if found.loops > 0:
		lines.append("Sent back for rework %d time%s" % [found.loops, "" if found.loops == 1 else "s"])
	if not found.risk.is_empty():
		lines.append("%s risk: %s" % [found.risk_words(), found.risk_reason.left(120)] if not found.risk_reason.is_empty()
				else "%s risk" % found.risk_words())
	hover_label.text = "\n".join(lines)
	var mouse := get_viewport().get_mouse_position()
	var view := get_viewport_rect().size
	var size := hover_tip.get_combined_minimum_size()
	hover_tip.size = size
	hover_tip.position = Vector2(minf(mouse.x + 16, view.x - size.x - 4), minf(mouse.y + 16, view.y - size.y - 4))


func _refresh_counters() -> void:
	var counts := {"line": 0, "waiting": 0, "shipped": 0, "scrapped": 0}
	for crate in crates.values():
		match crate.state:
			"shipped":
				counts["shipped"] += 1
			"scrapped":
				counts["scrapped"] += 1
			"awaiting_approval", "held":
				counts["waiting"] += 1
			_:
				counts["line"] += 1
	counters_label.text = "%d in line  ·  %d shipped  ·  %d scrapped" % [
			counts["line"], counts["shipped"], counts["scrapped"]]
	var waiting := int(counts["waiting"])
	inbox_button.text = "%d waiting for you" % waiting
	inbox_button.modulate = Color("5AB0FF") if approvals_view.visible else (Color("F2B84C") if waiting > 0 else Color.WHITE)


## Keep the belts running while crates are in the line, and the return belt while one rides it.
func _process(delta: float) -> void:
	if building and pending_save:
		var before := _save_wait
		_save_wait += delta
		if before < SAVE_PATIENCE and _save_wait >= SAVE_PATIENCE:
			build_panel.show_message("Still saving: the backend hasn't answered for %d seconds. It may be fetching a repo from GitHub; its window says what it's doing. You can keep editing and press Save again." % int(SAVE_PATIENCE))
	_update_hover()
	_layout_docks()
	_key_pan(delta)
	_inspect_tick += delta
	if inspector.visible and _inspect_tick >= 0.5:
		_inspect_tick = 0.0
		_refresh_inspector()
	if approvals_view.visible:
		_approvals_tick += delta
		if _approvals_tick >= 0.5:
			_approvals_tick = 0.0
			_refresh_approvals()
	if bin_view.visible:
		_bin_tick += delta
		if _bin_tick >= 0.5:
			_bin_tick = 0.0
			_refresh_bin()
	if not building:
		_store_tick += delta
		if _store_tick >= STORE_REFRESH:
			_ask_storages()
	if intake_view.visible:
		_intake_tick += delta
		if _intake_tick >= INTAKE_REFRESH:
			_intake_tick = 0.0
			client.send({"type": "intake_list", "pipeline": viewed_intake})
		_layout_intake_view()
	var active := false
	var returning := false
	for crate in crates.values():
		if crate.state in ["shipped", "scrapped", "awaiting_approval", "held"]:
			# Still riding its last belt to the dock, or a red belt into a bin: that belt keeps
			# running until the crate is off it.
			if crate.is_moving() and crate.state in ["shipped", "scrapped"]:
				active = true
				returning = returning or FactoryFloor.is_bin(crate.location)
			continue
		active = true
		if _is_return(crate.location) or crate.came_back:
			returning = true
	factory_floor.set_belt_motion(active and not building, returning and not building)


func _target_for(id: int, location: String, state := "", came_back := false) -> Vector2:
	if came_back and ":belt_" in location:
		# Back from rework: wait at the end of the rework belt, by the machine's lower port.
		var rise := location.replace(":belt_", ":rework_in_")
		if factory_floor.has_point(rise):
			return factory_floor.point(rise)
	var base := factory_floor.point(location)
	if state == "awaiting_approval":
		# Crates waiting for you queue up in a row under the gate machine (below its step text).
		return base + Vector2((float(id % 5) - 2.0) * 22.0, 112.0)
	if ":belt_" in location:
		# Crates waiting on a belt queue up along it, nose to tail, the first at its end.
		var rank := 0
		for other in crates.values():
			if other.item_id < id and other.location == location and not other.came_back \
					and other.state not in ["shipped", "scrapped"]:
				rank += 1
		return factory_floor.back_along(location, rank * 30.0)
	if location.ends_with(":intake") or FactoryFloor.is_dock(location) or FactoryFloor.is_bin(location):
		# Spread crates that share a spot so they don't hide each other.
		base += Vector2(float((id * 37) % 29) - 14.0, float((id * 53) % 29) - 14.0)
	return base


func _append_log(id: int, line: String) -> void:
	if not logs.has(id):
		logs[id] = []
	logs[id].append(line)
	if id == selected_id:
		_write_log_line(line)


func _select(id: int) -> void:
	if crates.has(selected_id):
		var old: Crate = crates[selected_id]
		old.selected = false
	selected_id = id
	log_view.clear()
	_refresh_actions()
	var show_log := crates.has(id) and not building
	if bottom_panel.visible != show_log:
		bottom_panel.visible = show_log
		_fit_camera()
	if not crates.has(id):
		log_title.text = LOG_HINT
		log_subtitle.text = ""
		return
	var crate: Crate = crates[id]
	crate.selected = true
	log_title.text = "Crate #%d" % id
	_select_subtitle(id)
	if logs_partial.has(id):
		log_view.append_text("[color=#%s]… loading the start of the log[/color]\n" % LOG_COLORS["factory"])
		client.send({"type": "get_log", "item_id": id})
	for line in logs.get(id, []):
		_write_log_line(str(line))


func _select_subtitle(id: int) -> void:
	if not crates.has(id):
		return
	var crate: Crate = crates[id]
	var title := crate.text.get_slice("\n", 0)
	if crate.loops > 0:
		title += "  (sent back %d time%s)" % [crate.loops, "" if crate.loops == 1 else "s"]
	if not crate.risk.is_empty():
		title += "  [%s risk]" % crate.risk_words()
	if float(costs.get(id, 0.0)) > 0.0:
		title += "  ·  Claude %s" % StatsView._usd(float(costs[id]))
	log_subtitle.text = title + (("  ·  " + crate.source) if not crate.source.is_empty() else "")


## The whole log of a crate, asked for when you opened it.
func _got_full_log(id: int, full: Array) -> void:
	if not logs_partial.has(id):
		return
	logs_partial.erase(id)
	var lines: Array = []
	for line in full:
		lines.append(str(line))
	# Lines that arrived after the backend read it are already at the end of ours.
	var have: Array = logs.get(id, [])
	var tail_start := have.size()  # (no overlap found: keep the backend's)
	for k in range(have.size(), 0, -1):
		if k <= lines.size() and lines.slice(lines.size() - k) == have.slice(0, k):
			tail_start = k
			break
	logs[id] = lines + have.slice(tail_start)
	if id == selected_id:
		log_view.clear()
		for line in logs[id]:
			_write_log_line(str(line))


## One log line, coloured by what it is: step headers, factory messages, failures, commands.
func _write_log_line(line: String) -> void:
	var safe := line.replace("[", "[lb]")
	var lower := line.to_lower()
	if line.begins_with("== "):
		log_view.append_text("[b][color=#%s]%s[/color][/b]\n" % [LOG_COLORS["header"], safe])
	elif "failed" in lower or line.begins_with("Traceback") or "error" in lower or line.begins_with("FAIL"):
		log_view.append_text("[color=#%s]%s[/color]\n" % [LOG_COLORS["fail"], safe])
	elif line.begins_with("[factory]"):
		log_view.append_text("[color=#%s]%s[/color]\n" % [LOG_COLORS["factory"], safe])
	elif line.begins_with("$ "):
		log_view.append_text("[color=#%s]%s[/color]\n" % [LOG_COLORS["command"], safe])
	else:
		log_view.append_text(safe + "\n")


func _source_label(item: Dictionary) -> String:
	var ref := str(item.get("source_ref", ""))
	match str(item.get("source", "")):
		"github":
			return "GitHub #" + ref
		"jira":
			return "Jira " + ref
		"prd":
			return "PRD #" + ref if not ref.is_empty() else "PRD"
		"github_pr":
			return "PR #" + ref
	return ""
