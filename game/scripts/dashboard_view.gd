extends PanelContainer
## The factory dashboard: everything at a glance on one screen, over the floor.
##   Overview: headline numbers and charts (from the backend's "stats" message, the same numbers
##     as the Stats window), and every crate waiting for you, with buttons to decide them.
##   Lines & machines: each line and machine, what it's doing, its runs, failures and cost, and
##     buttons to pause or resume it (or the whole factory).
##   Crates: every crate in a table you can search, filter, sort and tick, then approve, send
##     back, run again or scrap together.
##   Storage: what's waiting in each storage (issues, PRs, Jira issues) and the repo it's on, to
##     pull in or close; each storage's own actions (turn on/off, check now, its window, show it
##     on the floor); and New crate, to queue changes in a Custom storage.
##   Worktrees & branches: every worktree and local branch in the lines' repos, whether each is
##     stale (and why), and Clean / Force clean for the ticked ones (the backend's janitor.py
##     decides what's stale and what may go).
## It reads the game's live state from main.gd (crates, layout, pauses) and sends commands
## through it. The backend decides everything, as everywhere else.

const FactoryFloor := preload("res://scripts/factory_floor.gd")

signal closed
signal show_crate(item_id: int)  # close the dashboard and select the crate on the floor
signal inspect_machine(machine_id: String)  # close the dashboard and open the machine's window
signal open_storage(pipeline: String, store_key: String)  # close it and open the storage's window
signal show_on_floor(key: String)  # close it and pan to that storage on the floor

const C_TEXT := "E6E9EF"
const C_QUIET := "9AA1AD"
const C_RED := "E5484D"
const C_GREEN := "5ABF66"
const C_AMBER := "F5A524"
const C_YELLOW := "F2B84C"
const C_BLUE := "5AB0FF"
const C_SKY := "8CBFFF"
const C_PURPLE := "B48CFF"
const DAY_CHOICES := [1, 7, 30]
const STATS_SECONDS := 20.0  # how often the numbers are asked for while it's open
const LIVE_SECONDS := 1.0  # how often the tables catch up with the floor
const STORE_SECONDS := 60.0  # how often the storages are listed again while it's open

const LIVE_STATES := ["queued", "waiting", "moving", "running"]
const STATE_ORDER := ["queued", "waiting", "moving", "running", "awaiting_approval", "held", "shipped", "scrapped"]
const STATE_WORDS := {"queued": "Queued", "waiting": "Waiting", "moving": "Moving", "running": "Being worked on",
		"awaiting_approval": "Waiting for you", "held": "Held for you", "shipped": "Shipped", "scrapped": "Scrapped"}
const STATE_COLORS := {"queued": C_QUIET, "waiting": C_SKY, "moving": C_SKY, "running": C_BLUE,
		"awaiting_approval": C_YELLOW, "held": C_RED, "shipped": C_GREEN, "scrapped": "B0676A"}
const CRATE_FILTERS := [["Every crate", []], ["On the line", ["queued", "waiting", "moving", "running"]],
		["Waiting for you", ["awaiting_approval", "held"]], ["Shipped", ["shipped"]], ["Scrapped", ["scrapped"]]]
const SCRAP_WORDS := {"needs_info": "Needs info", "already_done": "Already done", "blocked": "Blocked",
		"work_failed": "Work failed", "rejected": "Rejected", "wrong_line": "Not for this line", "other": "Other"}
const RISK_WORDS := {"low": "Low", "medium": "Medium", "high": "High"}
const RISK_COLORS := {"low": C_GREEN, "medium": C_YELLOW, "high": C_RED}
const SOURCE_TITLES := {"custom": "Changes you describe", "github": "GitHub issues", "jira": "Jira issues",
		"pr_review": "Pull requests to review", "prd": "PRDs", "github_pr": "Open PRs", "notion": "Notion pages"}
const CLOSABLE := ["github", "github_pr", "pr_review", "jira", "prd"]  # if a listing doesn't say (can_close)
const CRATE_COLUMNS := ["#", "Title", "Source", "Line", "Where", "State", "Age", "Reworks", "Risk", "Claude", "PR", ""]
const CRATE_SORT_KEYS := ["id", "title", "source", "line", "at", "state_rank", "created", "loops", "risk_rank", "cost", "pr", ""]

const BTN_PAUSE := 0
const BTN_INSPECT := 1
const BTN_SHOW := 2
const BTN_PR := 3
const BTN_APPROVE := 4
const BTN_REJECT := 5
const BTN_RETRY := 6
const BTN_SCRAP := 7
const BTN_OPEN_URL := 8
const BTN_PULL := 9
const BTN_FOLDER := 10
const BTN_POWER := 11
const BTN_CHECK := 12
const BTN_WINDOW := 13
const BTN_FLOOR := 14
const BTN_NEW_CRATE := 15
const STORE_BUTTONS := 6  # the Storage tree's button column
const TAB_STORAGE := 3
const TAB_WORKTREES := 4

var main: Node  # main.gd: crates, last_layout, pauses, pr_urls, costs, machine_runs, client...
var days := 7

var _stats: Dictionary = {}
var _listings := {}  # pipeline id -> its latest intake_list message
var _stats_tick := 0.0
var _live_tick := 0.0
var _store_tick := 0.0
var _icons := {}

var _summary: Label
var _range: OptionButton
var _tabs: TabContainer
# Overview
var _tiles: HFlowContainer
var _tile_labels := {}  # key -> [value Label, caption Label]
var _chart_days: Chart
var _chart_cost: Chart
var _chart_states: Chart
var _chart_scrap: Chart
var _chart_machines: Chart
var _chart_lines: Chart
var _chart_agents: Chart
var _waiting_tree: Tree
var _waiting_empty: Label
# Lines & machines
var _factory_label: RichTextLabel
var _factory_button: Button
var _lines_tree: Tree
# Crates
var _search: LineEdit
var _state_filter: OptionButton
var _line_filter: OptionButton
var _line_filter_ids: Array = [""]
var _crate_tree: Tree
var _crate_count: Label
var _crate_note: LineEdit
var _crate_picked := {}  # item_id -> true
var _act_buttons := {}  # kind -> Button
var _sort_key := "id"
var _sort_desc := true
# Storage
var _store_tree: Tree
var _store_status: RichTextLabel
var _store_comment: LineEdit
var _store_picked := {}  # row key -> true
var _store_rows := {}  # row key -> {pipeline, source, storage, ref, url, can_pull, item_id}
var _store_heads := {}  # storage row key -> {pipeline, storage, source, disabled, enabled, title}
var _new_button: Button
var _crate_form: VBoxContainer  # New crate: a change typed into a Custom storage's queue
var _crate_where: OptionButton
var _crate_where_keys: Array = []  # storage row keys, in _crate_where's order
var _crate_text: TextEdit
var _crate_add: Button
var _close_button: Button
var _pull_button: Button
var _confirm: ConfirmationDialog
var _pending_close: Array = []  # the intake_close messages waiting for your OK
# Worktrees & branches
var _wt_tree: Tree
var _wt_status: RichTextLabel
var _wt_show: OptionButton
var _wt_clean_button: Button
var _wt_force_button: Button
var _wt_confirm: ConfirmationDialog
var _wt_list: Dictionary = {}  # the latest "worktrees" message
var _wt_rows := {}  # key -> row from the backend
var _wt_picked := {}  # key -> true
var _wt_pending: Dictionary = {}  # the worktrees_clean message waiting for your OK
var _wt_tick := 0.0
var _wt_result := ""  # what the last Clean did


func _ready() -> void:
	theme_type_variation = &"PanelDock"
	for name in ["pause", "play", "open", "pr", "check", "send_back", "retry", "bin", "pull_in", "window", "refresh",
			"add", "floor"]:
		var path := "res://assets/ui/icon_%s.svg" % name
		if ResourceLoader.exists(path):
			_icons[name] = load(path)

	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 8)
	add_child(col)
	var header := HBoxContainer.new()
	header.add_theme_constant_override("separation", 10)
	col.add_child(header)
	var title := Label.new()
	title.text = "Factory dashboard"
	title.theme_type_variation = &"LabelHeading"
	header.add_child(title)
	_summary = Label.new()
	_summary.theme_type_variation = &"LabelMuted"
	_summary.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_summary.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	_summary.clip_text = true
	_summary.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	header.add_child(_summary)
	_range = OptionButton.new()
	for d in DAY_CHOICES:
		_range.add_item("Today" if d == 1 else "Last %d days" % d)
	_range.select(DAY_CHOICES.find(days))
	_range.focus_mode = Control.FOCUS_NONE
	_range.tooltip_text = "The time the numbers and charts cover"
	_range.item_selected.connect(func(i: int) -> void:
		days = int(DAY_CHOICES[i])
		_ask_stats())
	header.add_child(_range)
	var refresh := Button.new()
	refresh.theme_type_variation = &"ButtonGhost"
	refresh.icon = load("res://assets/ui/icon_refresh.svg")
	refresh.tooltip_text = "Refresh everything now"
	refresh.focus_mode = Control.FOCUS_NONE
	refresh.pressed.connect(func() -> void:
		_ask_stats()
		_ask_storages()
		if _tabs.current_tab == TAB_WORKTREES:
			_ask_worktrees()
		_refresh_live())
	header.add_child(refresh)
	var esc := Label.new()
	esc.text = "Esc"
	esc.theme_type_variation = &"LabelKey"
	esc.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	header.add_child(esc)
	var close := Button.new()
	close.theme_type_variation = &"ButtonGhost"
	close.icon = load("res://assets/ui/theme/icons/close.svg")
	close.tooltip_text = "Close (Esc)"
	close.focus_mode = Control.FOCUS_NONE
	close.pressed.connect(func() -> void: closed.emit())
	header.add_child(close)

	_tabs = TabContainer.new()
	_tabs.size_flags_vertical = Control.SIZE_EXPAND_FILL
	col.add_child(_tabs)
	_tabs.add_child(_build_overview())
	_tabs.set_tab_title(0, "Overview")
	_tabs.add_child(_build_lines())
	_tabs.set_tab_title(1, "Lines & machines")
	_tabs.add_child(_build_crates())
	_tabs.set_tab_title(2, "Crates")
	_tabs.add_child(_build_storage())
	_tabs.set_tab_title(3, "Storage")
	_tabs.add_child(_build_worktrees())
	_tabs.set_tab_title(TAB_WORKTREES, "Worktrees & branches")
	_tabs.tab_changed.connect(func(_i: int) -> void:
		_refresh_live()
		if _tabs.current_tab == TAB_STORAGE:
			_refresh_storage()
		elif _tabs.current_tab == TAB_WORKTREES:
			_ask_worktrees())

	_confirm = ConfirmationDialog.new()
	_confirm.title = "Close on GitHub / Jira?"
	_confirm.ok_button_text = "Close them"
	_confirm.confirmed.connect(_send_close)
	add_child(_confirm)
	_wt_confirm = ConfirmationDialog.new()
	_wt_confirm.confirmed.connect(_send_worktrees_clean)
	add_child(_wt_confirm)


func open() -> void:
	visible = true
	_ask_stats()
	# Shipped crates whose PR was still open: the backend checks GitHub again (batched, and at
	# most every 30s) and sends pr_state for any now merged or closed.
	if main:
		main.client.send({"type": "refresh_pr_states"})
	_ask_storages()
	_refresh_live()
	if _tabs.current_tab == TAB_WORKTREES:
		_ask_worktrees()


func _process(delta: float) -> void:
	if not visible:
		return
	if main and (main.building or main._left_dock_open()):
		closed.emit()  # another window opened (they open under it), or build mode started
		return
	_stats_tick += delta
	_live_tick += delta
	_store_tick += delta
	_wt_tick += delta
	if _tabs.current_tab == TAB_WORKTREES and _wt_tick >= STORE_SECONDS:
		_ask_worktrees()
	if _stats_tick >= STATS_SECONDS:
		_ask_stats()
	if _store_tick >= STORE_SECONDS:
		_ask_storages()
	if _live_tick >= LIVE_SECONDS:
		_refresh_live()


## Every backend message passes through here; the dashboard picks out what it shows.
func on_message(msg: Dictionary) -> void:
	match str(msg.get("type", "")):
		"stats":
			if int(msg.get("days", days)) == days:
				_stats = msg
				if visible:
					_refresh_stats()
		"intake_list":
			_listings[str(msg.get("pipeline", ""))] = msg
			if visible and _tabs.current_tab == TAB_STORAGE:
				_refresh_storage()
		"worktrees":
			_wt_list = msg
			_refresh_worktrees()
		"worktrees_cleaned":
			_show_worktrees_cleaned(msg)
		"intake_closed":
			_show_closed(msg)
		"pauses":
			if visible:
				_live_tick = LIVE_SECONDS  # show it at once


func _ask_stats() -> void:
	_stats_tick = 0.0
	if main:
		main.client.send({"type": "stats", "days": days})


func _ask_storages() -> void:
	_store_tick = 0.0
	if not main:
		return
	for p in _pipelines():
		if not _storages_fed(p).is_empty():
			main.client.send({"type": "intake_list", "pipeline": str(p.get("id", ""))})


func _refresh_live() -> void:
	_live_tick = 0.0
	if not main:
		return
	_refresh_tiles()
	_refresh_state_chart()
	match _tabs.current_tab:
		0:
			_refresh_waiting()
		1:
			_refresh_lines()
		2:
			_refresh_crates()


# The game's state, as rows -------------------------------------------------------

func _pipelines() -> Array:
	var layout: Dictionary = main.last_layout
	var raw = layout.get("pipelines", [])
	return raw if raw is Array else []


func _storages_fed(p: Dictionary) -> Array:
	return FactoryFloor.fed_storages(p)


## machine id -> line id, from the layout.
func _machine_lines() -> Dictionary:
	var out := {}
	for p in _pipelines():
		for m in p.get("machines", []):
			if m is Dictionary:
				out[str(m.get("id", ""))] = str(p.get("id", ""))
	return out


func _line_names() -> Dictionary:
	var out := {}
	for p in _pipelines():
		out[str(p.get("id", ""))] = str(p.get("name", p.get("id", "")))
	return out


func _line_of(location: String, machine_lines: Dictionary) -> String:
	if machine_lines.has(location):
		return str(machine_lines[location])
	if ":" in location:
		return location.get_slice(":", 0)
	return ""


## One row per crate, with everything the tables show.
func _crate_rows() -> Array:
	var rows: Array = []
	var machine_lines := _machine_lines()
	var names := _line_names()
	var crates: Dictionary = main.crates
	var pr_urls: Dictionary = main.pr_urls
	var pr_states: Dictionary = main.pr_states
	var costs: Dictionary = main.costs
	var times = main.get("crate_times")
	var created: Dictionary = times if times is Dictionary else {}
	for crate in crates.values():
		var id: int = crate.item_id
		var location: String = crate.location
		var line := _line_of(location, machine_lines)
		var state: String = crate.state
		var url := str(pr_urls.get(id, ""))
		rows.append({
			"id": id, "title": str(crate.text).get_slice("\n", 0).strip_edges().left(140), "source": str(crate.source),
			"line_id": line, "line": str(names.get(line, line)), "location": location, "at": str(main._place_name(location)),
			"state": state, "state_rank": STATE_ORDER.find(state), "loops": int(crate.loops),
			"risk": str(crate.risk), "risk_rank": ["", "low", "medium", "high"].find(str(crate.risk)),
			"cost": float(costs.get(id, 0.0)), "pr_url": url, "pr": str(pr_states.get(id, "open" if url != "" else "")),
			"created": float(created.get(id, 0.0)), "waiting_for": str(crate.waiting_for), "scrap_kind": str(crate.scrap_kind),
		})
	return rows


## Approvals can send a crate back (Reject) when the machine has somewhere to send it.
func _sends_back(location: String) -> bool:
	var red: String = main.factory_floor.on_fail(location)
	var machine: Dictionary = main._layout_machine(location)
	return (not red.is_empty() and not FactoryFloor.is_bin(red)) or bool(machine.get("rework_here", false))


# Overview ---------------------------------------------------------------------

func _build_overview() -> Control:
	var scroll := ScrollContainer.new()
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	var col := VBoxContainer.new()
	col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	col.add_theme_constant_override("separation", 12)
	scroll.add_child(col)
	_tiles = HFlowContainer.new()
	_tiles.add_theme_constant_override("h_separation", 10)
	_tiles.add_theme_constant_override("v_separation", 10)
	col.add_child(_tiles)
	var grid := GridContainer.new()
	grid.columns = 2
	grid.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	grid.add_theme_constant_override("h_separation", 12)
	grid.add_theme_constant_override("v_separation", 12)
	col.add_child(grid)
	_chart_days = _card(grid, "Crates finished per day", "Green: shipped. Red: scrapped.")
	_chart_cost = _card(grid, "Claude cost per day", "What Claude cost on all lines, by day.")
	_chart_states = _card(grid, "Crates right now", "Every crate the factory knows, by where it is.")
	_chart_scrap = _card(grid, "Why crates were scrapped", "In the time chosen above.")
	_chart_machines = _card(grid, "Machine runs", "Green: passed. Red: failed. Hover for timings and cost.")
	_chart_lines = _card(grid, "Lines", "Crates shipped and scrapped per line, with Claude cost.")
	_chart_agents = _card(grid, "Agents by model and effort", "Machine runs per agent, model and effort, with cost (Claude only), tokens and time.")

	var card := _card_panel()
	col.add_child(card)
	col.move_child(card, _tiles.get_index() + 1)  # right below the headline tiles, above the charts
	var inner := VBoxContainer.new()
	card.add_child(inner)
	var head := HBoxContainer.new()
	inner.add_child(head)
	var label := Label.new()
	label.text = "Waiting for you"
	label.theme_type_variation = &"LabelHeading"
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(label)
	var hint := Label.new()
	hint.text = "Approve, send back, run again or scrap without leaving the dashboard."
	hint.theme_type_variation = &"LabelMuted"
	head.add_child(hint)
	_waiting_empty = Label.new()
	_waiting_empty.theme_type_variation = &"LabelMuted"
	_waiting_empty.text = "Nothing is waiting for you."
	inner.add_child(_waiting_empty)
	_waiting_tree = _tree(["#", "Title", "Line", "At", "Why", ""], [0, 4, 2, 2, 4, 0])
	_waiting_tree.custom_minimum_size = Vector2(0, 220)
	_waiting_tree.set_column_custom_minimum_width(0, 70)
	_waiting_tree.set_column_custom_minimum_width(5, 150)
	_waiting_tree.button_clicked.connect(_on_waiting_button)
	_waiting_tree.item_activated.connect(func() -> void:
		var item := _waiting_tree.get_selected()
		if item:
			show_crate.emit(int(item.get_metadata(0))))
	inner.add_child(_waiting_tree)
	return scroll


func _card_panel() -> PanelContainer:
	var card := PanelContainer.new()
	var box := StyleBoxFlat.new()
	box.bg_color = Color(1, 1, 1, 0.035)
	box.border_color = Color(1, 1, 1, 0.07)
	box.set_border_width_all(1)
	box.set_corner_radius_all(6)
	box.set_content_margin_all(12)
	card.add_theme_stylebox_override("panel", box)
	card.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	return card


func _card(parent: Control, title: String, hint: String) -> Chart:
	var card := _card_panel()
	parent.add_child(card)
	var col := VBoxContainer.new()
	card.add_child(col)
	var head := Label.new()
	head.text = title
	head.theme_type_variation = &"LabelHeading"
	col.add_child(head)
	var sub := Label.new()
	sub.text = hint
	sub.theme_type_variation = &"LabelMuted"
	sub.add_theme_font_size_override("font_size", 12)
	col.add_child(sub)
	var chart := Chart.new()
	chart.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	chart.custom_minimum_size = Vector2(0, 150)
	col.add_child(chart)
	return chart


func _tile(key: String, value: String, caption: String, colour: String = C_TEXT, tip: String = "") -> void:
	if not _tile_labels.has(key):
		var card := _card_panel()
		card.custom_minimum_size = Vector2(150, 0)
		card.size_flags_horizontal = Control.SIZE_FILL
		var col := VBoxContainer.new()
		col.add_theme_constant_override("separation", 0)
		card.add_child(col)
		var v := Label.new()
		v.add_theme_font_size_override("font_size", 24)
		col.add_child(v)
		var c := Label.new()
		c.theme_type_variation = &"LabelMuted"
		col.add_child(c)
		_tiles.add_child(card)
		_tile_labels[key] = [v, c, card]
	var labels: Array = _tile_labels[key]
	labels[0].text = value
	labels[0].add_theme_color_override("font_color", Color(colour))
	labels[1].text = caption
	labels[2].tooltip_text = tip


func _refresh_tiles() -> void:
	var rows := _crate_rows()
	var on_line := 0
	var waiting := 0
	var held := 0
	for r in rows:
		if r["state"] in LIVE_STATES:
			on_line += 1
		elif r["state"] == "awaiting_approval":
			waiting += 1
		elif r["state"] == "held":
			held += 1
	var lines := _pipelines()
	var pauses: Dictionary = main.pauses
	var paused_lines: Array = pauses.get("lines", [])
	var running := 0
	for p in lines:
		if not str(p.get("id", "")) in paused_lines and str(p.get("problem", "")).is_empty():
			running += 1
	var factory_paused := bool(pauses.get("factory", false))
	_tile("line", str(on_line), "on the line now", C_BLUE)
	_tile("waiting", str(waiting + held), "waiting for you" if held == 0 else "waiting for you (%d held)" % held,
			C_YELLOW if waiting + held > 0 else C_QUIET)
	_tile("lines", "Paused" if factory_paused else "%d of %d" % [running, lines.size()],
			"factory paused" if factory_paused else "lines running", C_AMBER if factory_paused or running < lines.size() else C_GREEN)
	var s := _stats
	var range_words := "today" if days == 1 else "in %d days" % days
	if s.is_empty():
		_summary.text = "Working out the numbers…"
		return
	var shipped := int(s.get("shipped", 0))
	var scrapped := int(s.get("scrapped", 0))
	var times: Dictionary = s.get("crate_minutes", {})
	var budget: Dictionary = s.get("budget", {}) if s.get("budget") is Dictionary else {}
	_tile("shipped", str(shipped), "shipped " + range_words, C_GREEN)
	_tile("scrapped", str(scrapped), "scrapped " + range_words, C_RED if scrapped > 0 else C_QUIET,
			"%s of finished crates" % _pct(float(s.get("fail_rate", 0.0))))
	_tile("time", _minutes(float(times.get("median", 0.0))) if shipped else "–", "typical time per crate", C_TEXT,
			"Average %s, longest %s" % [_minutes(float(times.get("mean", 0.0))), _minutes(float(times.get("longest", 0.0)))] if shipped else "")
	_tile("rework", _pct(float(s.get("rework_rate", 0.0))) if shipped else "–", "shipped after rework")
	_tile("cost", _usd(float(s.get("cost_usd", 0.0))), "Claude cost " + range_words, C_PURPLE,
			"%s per shipped crate" % _usd(float(s.get("cost_per_shipped", 0.0))) if shipped else "")
	var today := float(budget.get("today_usd", 0.0))
	var daily := float(budget.get("daily_usd", 0.0))
	_tile("budget", _usd(today), ("spent today of %s" % _usd(daily)) if daily > 0.0 else "spent today (no daily budget)",
			C_RED if daily > 0.0 and today >= daily else (C_AMBER if daily > 0.0 and today >= daily * 0.8 else C_TEXT))
	_summary.text = "%d shipped, %d scrapped %s  ·  %s on Claude  ·  updated %s" % [shipped, scrapped, range_words,
			_usd(float(s.get("cost_usd", 0.0))), Time.get_time_string_from_system().left(5)]


func _refresh_stats() -> void:
	_refresh_tiles()
	var s := _stats
	var per_day: Array = s.get("per_day", [])
	var labels: Array = []
	var good: Array = []
	var bad: Array = []
	var cost: Array = []
	var tips: Array = []
	var cost_tips: Array = []
	for d in per_day:
		var date := str(d.get("date", ""))
		labels.append(date.substr(4) if date.length() > 4 else date)
		good.append(float(d.get("shipped", 0)))
		bad.append(float(d.get("scrapped", 0)))
		cost.append(float(d.get("cost_usd", 0.0)))
		tips.append("%s: %d shipped, %d scrapped" % [date, int(d.get("shipped", 0)), int(d.get("scrapped", 0))])
		cost_tips.append("%s: %s" % [date, _usd(float(d.get("cost_usd", 0.0)))])
	_chart_days.set_columns(labels, [good, bad], [Color(C_GREEN), Color(C_RED)], tips, false)
	_chart_cost.set_columns(labels, [cost], [Color(C_PURPLE)], cost_tips, true)

	var kinds: Dictionary = s.get("scrap_kinds", {}) if s.get("scrap_kinds") is Dictionary else {}
	var scrap_rows: Array = []
	for k in kinds:
		scrap_rows.append({"label": str(SCRAP_WORDS.get(k, str(k).capitalize())), "parts": [[float(kinds[k]), Color(C_RED)]],
				"text": str(int(kinds[k]))})
	scrap_rows.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return a["parts"][0][0] > b["parts"][0][0])
	_chart_scrap.set_bars(scrap_rows, "Nothing was scrapped.")

	var machines: Array = s.get("machines", [])
	var several := _pipelines().size() > 1
	var machine_rows: Array = []
	for m in machines:
		var runs := int(m.get("runs", 0))
		var failed := int(m.get("failed", 0))
		var name := str(m.get("name", m.get("id", "")))
		var tip := "%s: %d run%s, %s each, %s failed, %s" % [name, runs, "" if runs == 1 else "s",
				_seconds(float(m.get("avg_seconds", 0.0))), _pct(float(m.get("fail_rate", 0.0))), _usd(float(m.get("cost_usd", 0.0)))]
		var top: Array = m.get("top_failures", [])
		if not top.is_empty():
			var steps := PackedStringArray()
			for t in top:
				steps.append("%s ×%d" % [str(t.get("step", "")), int(t.get("count", 0))])
			tip += "\nFails at: " + ", ".join(steps)
		machine_rows.append({"label": ("%s · %s" % [name, str(m.get("line", ""))]) if several else name,
				"parts": [[float(runs - failed), Color(C_GREEN)], [float(failed), Color(C_RED)]],
				"text": "%d · %s" % [runs, _seconds(float(m.get("avg_seconds", 0.0)))], "tip": tip})
	_chart_machines.set_bars(machine_rows, "No machine has run in this time.")

	var line_rows: Array = []
	for ln in s.get("lines", []):
		line_rows.append({"label": str(ln.get("name", "")),
				"parts": [[float(ln.get("shipped", 0)), Color(C_GREEN)], [float(ln.get("scrapped", 0)), Color(C_RED)]],
				"text": _usd(float(ln.get("cost_usd", 0.0))),
				"tip": "%s: %d shipped, %d scrapped, %s" % [str(ln.get("name", "")), int(ln.get("shipped", 0)),
						int(ln.get("scrapped", 0)), _usd(float(ln.get("cost_usd", 0.0)))]})
	_chart_lines.set_bars(line_rows, "No line finished a crate in this time.")

	var agent_rows: Array = []
	for a in s.get("agents", []):
		var runs := int(a.get("runs", 0))
		var failed := int(a.get("failed", 0))
		var label := _agent_label(str(a.get("model", "")), str(a.get("effort", "")), str(a.get("agent", "")))
		var cost_text := _usd(float(a.get("cost_usd", 0.0)))
		if str(a.get("agent", "claude")) not in ["", "claude"]:
			cost_text = "%s tokens" % _count(int(a.get("tokens", 0)))
		agent_rows.append({"label": label,
				"parts": [[float(runs - failed), Color(C_GREEN)], [float(failed), Color(C_RED)]],
				"text": "%d · %s · %s" % [runs, cost_text, _seconds(float(a.get("seconds", 0.0)))],
				"tip": "%s: %d run%s (%d failed), %s, %s in all, %s each, %d turns" % [label, runs,
						"" if runs == 1 else "s", failed, cost_text, _seconds(float(a.get("seconds", 0.0))),
						_seconds(float(a.get("avg_seconds", 0.0))), int(a.get("turns", 0))]})
	_chart_agents.set_bars(agent_rows, "No machine ran an agent in this time.")


func _refresh_state_chart() -> void:
	var counts := {}
	for r in _crate_rows():
		var st: String = "running" if r["state"] in ["waiting", "moving"] else r["state"]
		counts[st] = int(counts.get(st, 0)) + 1
	var rows: Array = []
	for st in ["queued", "running", "awaiting_approval", "held", "shipped", "scrapped"]:
		if counts.has(st):
			var words := "On the line" if st == "running" else str(STATE_WORDS[st])
			rows.append({"label": words, "parts": [[float(counts[st]), Color(STATE_COLORS[st])]], "text": str(counts[st])})
	_chart_states.set_bars(rows, "No crates yet.")


func _refresh_waiting() -> void:
	var rows: Array = []
	for r in _crate_rows():
		if r["state"] == "awaiting_approval" or r["state"] == "held":
			rows.append(r)
	rows.sort_custom(func(a: Dictionary, b: Dictionary) -> bool: return int(a["id"]) < int(b["id"]))
	_waiting_empty.visible = rows.is_empty()
	_waiting_tree.visible = not rows.is_empty()
	var keyed: Array = []
	for r in rows:
		keyed.append({"key": "W%d" % int(r["id"]), "parent": "", "row": r})
	_sync_tree(_waiting_tree, keyed, func(item: TreeItem, entry: Dictionary) -> void:
		var r: Dictionary = entry["row"]
		var held: bool = r["state"] == "held"
		item.set_metadata(0, int(r["id"]))
		item.set_text(0, "#%d" % int(r["id"]))
		item.set_text(1, str(r["title"]))
		item.set_tooltip_text(1, str(r["title"]))
		item.set_text(2, str(r["line"]))
		item.set_text(3, str(r["at"]))
		var why := str(main._scrap_reason(int(r["id"]))) if held else (str(r["waiting_for"]) if str(r["waiting_for"]) != "" else "Waiting for your approval")
		item.set_text(4, why)
		item.set_tooltip_text(4, why)
		item.set_custom_color(4, Color(C_RED if held else C_YELLOW))
		_clear_buttons(item, 5)
		if held:
			_button(item, 5, "retry", BTN_RETRY, "Run again: run the machine again on it")
			_button(item, 5, "bin", BTN_SCRAP, "Scrap it")
		else:
			_button(item, 5, "check", BTN_APPROVE, "Approve")
			var back := _sends_back(str(r["location"]))
			_button(item, 5, "send_back", BTN_REJECT, "Send back to be fixed" if back else "Reject: close the PR and scrap it")
			if back:
				_button(item, 5, "bin", BTN_SCRAP, "Scrap it")
		if str(r["pr_url"]) != "":
			_button(item, 5, "pr", BTN_PR, "Open its pull request")
		_button(item, 5, "open", BTN_SHOW, "Show it on the floor, with its log"))


func _on_waiting_button(item: TreeItem, _column: int, id: int, _mouse: int) -> void:
	var item_id := int(item.get_metadata(0))
	match id:
		BTN_APPROVE:
			main._decide_many([item_id], "approve", "")
		BTN_REJECT:
			main._decide_many([item_id], "reject", "")
		BTN_RETRY:
			main._decide_many([item_id], "retry", "")
		BTN_SCRAP:
			main._decide_many([item_id], "scrap", "")
		BTN_PR:
			_open_pr(item_id)
		BTN_SHOW:
			show_crate.emit(item_id)


# Lines & machines -------------------------------------------------------------

func _build_lines() -> Control:
	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 8)
	var bar := HBoxContainer.new()
	col.add_child(bar)
	_factory_label = RichTextLabel.new()
	_factory_label.bbcode_enabled = true
	_factory_label.fit_content = true
	_factory_label.scroll_active = false
	_factory_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_factory_label.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	bar.add_child(_factory_label)
	_factory_button = Button.new()
	_factory_button.theme_type_variation = &"ButtonPrimary"
	_factory_button.focus_mode = Control.FOCUS_NONE
	_factory_button.pressed.connect(func() -> void:
		var pauses: Dictionary = main.pauses
		main._send_pause("factory", "", not bool(pauses.get("factory", false))))
	bar.add_child(_factory_button)
	var hint := Label.new()
	hint.theme_type_variation = &"LabelMuted"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.text = "Runs, times, failures and cost cover the time chosen above. Pause a line or a machine with its button: crates finish the machine they're in, then wait. Double-click a machine to open its window."
	col.add_child(hint)
	_lines_tree = _tree(["Line / machine", "Status", "Crates here", "Runs", "Avg run", "Failed", "Claude", ""], [4, 5, 1, 1, 1, 1, 1, 0])
	_lines_tree.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_lines_tree.hide_root = true
	_lines_tree.set_column_custom_minimum_width(7, 80)
	_lines_tree.button_clicked.connect(_on_lines_button)
	_lines_tree.item_activated.connect(func() -> void:
		var item := _lines_tree.get_selected()
		if item and str(item.get_metadata(0)).begins_with("M:"):
			inspect_machine.emit(str(item.get_metadata(0)).substr(2)))
	col.add_child(_lines_tree)
	return col


func _refresh_lines() -> void:
	var pauses: Dictionary = main.pauses
	var paused_lines: Array = pauses.get("lines", [])
	var paused_machines: Array = pauses.get("machines", [])
	var factory_paused := bool(pauses.get("factory", false))
	_factory_label.text = ("[color=#%s][b]The factory is paused.[/b][/color] Nothing new comes in; crates wait on the belts." % C_AMBER) \
			if factory_paused else ("[color=#%s][b]The factory is running.[/b][/color]" % C_GREEN)
	_factory_button.text = "Resume the factory" if factory_paused else "Pause the factory"
	_factory_button.icon = _icons.get("play" if factory_paused else "pause")

	var machine_lines := _machine_lines()
	var here := {}  # location -> crates there now (not finished)
	var on_line := {}  # line id -> crates on it now
	var working := {}  # machine id -> the crate it's working on
	var waiting_at := {}  # machine id -> crates waiting there for you
	for r in _crate_rows():
		if r["state"] in ["shipped", "scrapped"]:
			continue
		here[r["location"]] = int(here.get(r["location"], 0)) + 1
		on_line[r["line_id"]] = int(on_line.get(r["line_id"], 0)) + 1
		if r["state"] == "running":
			working[r["location"]] = int(r["id"])
		elif r["state"] in ["awaiting_approval", "held"]:
			waiting_at[r["location"]] = int(waiting_at.get(r["location"], 0)) + 1
	var stats_by_id := {}
	for m in _stats.get("machines", []):
		stats_by_id[str(m.get("id", ""))] = m
	var line_stats := {}
	for ln in _stats.get("lines", []):
		line_stats[str(ln.get("id", ""))] = ln
	var machine_runs: Dictionary = main.machine_runs

	var entries: Array = []
	for p in _pipelines():
		var pid := str(p.get("id", ""))
		entries.append({"key": "L:" + pid, "parent": "", "line": p})
		for m in p.get("machines", []):
			if m is Dictionary:
				entries.append({"key": "M:" + str(m.get("id", "")), "parent": "L:" + pid, "machine": m, "pid": pid})
	_sync_tree(_lines_tree, entries, func(item: TreeItem, e: Dictionary) -> void:
		item.set_metadata(0, e["key"])
		_clear_buttons(item, 7)
		if e.has("line"):
			var p: Dictionary = e["line"]
			var pid := str(p.get("id", ""))
			var paused: bool = pid in paused_lines
			var problem := str(p.get("problem", ""))
			item.set_text(0, str(p.get("name", pid)))
			item.set_custom_color(0, Color(C_TEXT))
			var status := "Paused" if paused else ("Can't run: " + (problem.substr(problem.find(": ") + 2) if ": " in problem else problem)
					if not problem.is_empty() else ("Running" if int(on_line.get(pid, 0)) > 0 else "Idle"))
			item.set_text(1, status)
			item.set_tooltip_text(1, problem if not problem.is_empty() else status)
			item.set_custom_color(1, Color(C_AMBER if paused else (C_RED if not problem.is_empty() else (C_GREEN if status == "Running" else C_QUIET))))
			item.set_text(2, str(int(on_line.get(pid, 0))))
			var runs := 0
			var failed := 0
			for m in p.get("machines", []):
				if m is Dictionary and stats_by_id.has(str(m.get("id", ""))):
					runs += int(stats_by_id[str(m.get("id", ""))].get("runs", 0))
					failed += int(stats_by_id[str(m.get("id", ""))].get("failed", 0))
			var ls: Dictionary = line_stats.get(pid, {})
			item.set_text(3, str(runs))
			item.set_text(4, "%d shipped" % int(ls.get("shipped", 0)))
			item.set_tooltip_text(4, "%d shipped, %d scrapped" % [int(ls.get("shipped", 0)), int(ls.get("scrapped", 0))])
			item.set_text(5, str(failed))
			item.set_custom_color(5, Color(C_RED if failed > 0 else C_QUIET))
			item.set_text(6, _usd(float(ls.get("cost_usd", 0.0))))
			_button(item, 7, "play" if paused else "pause", BTN_PAUSE, "Resume this line" if paused else "Pause this line")
			return
		var m: Dictionary = e["machine"]
		var mid := str(m.get("id", ""))
		var paused: bool = mid in paused_machines
		item.set_text(0, str(m.get("name", mid)))
		item.set_custom_color(0, Color("C8CDD6"))
		var status := "Idle"
		var colour := C_QUIET
		if working.has(mid):
			var run: Dictionary = machine_runs.get(mid, {})
			var step := str(run.get("step", "")) if int(run.get("item_id", -1)) == int(working[mid]) else ""
			status = "Working on #%d%s" % [int(working[mid]), (": " + step) if not step.is_empty() else ""]
			colour = C_BLUE
		if int(waiting_at.get(mid, 0)) > 0:
			status = "%d waiting for you" % int(waiting_at[mid]) + ((" · " + status) if working.has(mid) else "")
			colour = C_YELLOW
		if paused:
			status = "Paused" + ((" · " + status) if status != "Idle" else "")
			colour = C_AMBER
		item.set_text(1, status)
		item.set_tooltip_text(1, status)
		item.set_custom_color(1, Color(colour))
		item.set_text(2, str(int(here.get(mid, 0))))
		var ms: Dictionary = stats_by_id.get(mid, {})
		var mruns := int(ms.get("runs", 0))
		var mfailed := int(ms.get("failed", 0))
		item.set_text(3, str(mruns))
		item.set_text(4, _seconds(float(ms.get("avg_seconds", 0.0))) if mruns > 0 else "–")
		item.set_text(5, ("%d (%s)" % [mfailed, _pct(float(ms.get("fail_rate", 0.0)))]) if mfailed > 0 else "0")
		item.set_custom_color(5, Color(C_RED if mfailed > 0 else C_QUIET))
		var top: Array = ms.get("top_failures", [])
		if not top.is_empty():
			var steps := PackedStringArray()
			for t in top:
				steps.append("%s ×%d" % [str(t.get("step", "")), int(t.get("count", 0))])
			item.set_tooltip_text(5, "Fails at: " + ", ".join(steps))
		item.set_text(6, _usd(float(ms.get("cost_usd", 0.0))))
		_button(item, 7, "play" if paused else "pause", BTN_PAUSE, "Resume this machine" if paused else "Pause this machine")
		_button(item, 7, "window", BTN_INSPECT, "Open the machine's window"))


func _on_lines_button(item: TreeItem, _column: int, id: int, _mouse: int) -> void:
	var key := str(item.get_metadata(0))
	var pauses: Dictionary = main.pauses
	if key.begins_with("L:"):
		var pid := key.substr(2)
		if id == BTN_PAUSE:
			main._send_pause("line", pid, not pid in pauses.get("lines", []))
	elif key.begins_with("M:"):
		var mid := key.substr(2)
		if id == BTN_PAUSE:
			main._send_pause("machine", mid, not mid in pauses.get("machines", []))
		elif id == BTN_INSPECT:
			inspect_machine.emit(mid)


# Crates -------------------------------------------------------------------------

func _build_crates() -> Control:
	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 8)
	var bar := HBoxContainer.new()
	bar.add_theme_constant_override("separation", 8)
	col.add_child(bar)
	_search = LineEdit.new()
	_search.placeholder_text = "Search titles, #ids, sources or places"
	_search.clear_button_enabled = true
	_search.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_search.text_changed.connect(func(_t: String) -> void: _refresh_crates())
	bar.add_child(_search)
	_state_filter = OptionButton.new()
	for f in CRATE_FILTERS:
		_state_filter.add_item(str(f[0]))
	_state_filter.focus_mode = Control.FOCUS_NONE
	_state_filter.item_selected.connect(func(_i: int) -> void: _refresh_crates())
	bar.add_child(_state_filter)
	_line_filter = OptionButton.new()
	_line_filter.add_item("Every line")
	_line_filter.focus_mode = Control.FOCUS_NONE
	_line_filter.item_selected.connect(func(_i: int) -> void: _refresh_crates())
	bar.add_child(_line_filter)
	_crate_count = Label.new()
	_crate_count.theme_type_variation = &"LabelMuted"
	_crate_count.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	bar.add_child(_crate_count)

	_crate_tree = _tree(CRATE_COLUMNS, [0, 9, 3, 3, 3, 3, 0, 0, 0, 0, 0, 0])
	_crate_tree.size_flags_vertical = Control.SIZE_EXPAND_FILL
	for c in [0, 6, 7, 8, 9, 10]:
		_crate_tree.set_column_custom_minimum_width(c, {0: 90, 6: 60, 7: 80, 8: 76, 9: 72, 10: 72}[c])
	_crate_tree.set_column_custom_minimum_width(11, 70)
	_crate_tree.button_clicked.connect(_on_crate_button)
	_crate_tree.item_edited.connect(func() -> void:
		var item := _crate_tree.get_edited()
		if item:
			var id := int(item.get_metadata(0))
			if item.is_checked(0):
				_crate_picked[id] = true
			else:
				_crate_picked.erase(id)
			_sync_actions())
	_crate_tree.item_activated.connect(func() -> void:
		var item := _crate_tree.get_selected()
		if item:
			show_crate.emit(int(item.get_metadata(0))))
	_crate_tree.column_title_clicked.connect(func(column: int, _mouse: int) -> void:
		var key: String = CRATE_SORT_KEYS[column]
		if key.is_empty():
			return
		if key == _sort_key:
			_sort_desc = not _sort_desc
		else:
			_sort_key = key
			_sort_desc = key in ["id", "created", "loops", "risk_rank", "cost"]
		_refresh_crates())
	col.add_child(_crate_tree)

	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation", 8)
	col.add_child(actions)
	var none := Button.new()
	none.text = "Untick all"
	none.theme_type_variation = &"ButtonGhost"
	none.focus_mode = Control.FOCUS_NONE
	none.pressed.connect(func() -> void:
		_crate_picked.clear()
		_refresh_crates())
	actions.add_child(none)
	var shown := Button.new()
	shown.text = "Tick all shown"
	shown.theme_type_variation = &"ButtonGhost"
	shown.focus_mode = Control.FOCUS_NONE
	shown.pressed.connect(func() -> void:
		for r in _filtered_crates():
			_crate_picked[int(r["id"])] = true
		_refresh_crates())
	actions.add_child(shown)
	_crate_note = LineEdit.new()
	_crate_note.placeholder_text = "Note sent with your decision (optional)"
	_crate_note.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	actions.add_child(_crate_note)
	for spec in [["approve", "Approve", &"ButtonApprove", "check"], ["reject", "Send back", &"ButtonReject", "send_back"],
			["retry", "Run again", &"", "retry"], ["scrap", "Scrap", &"", "bin"]]:
		var b := Button.new()
		b.theme_type_variation = spec[2]
		b.icon = _icons.get(spec[3])
		b.focus_mode = Control.FOCUS_NONE
		var kind: String = spec[0]
		b.set_meta("words", spec[1])
		b.pressed.connect(func() -> void: _act_on_picked(kind))
		actions.add_child(b)
		_act_buttons[kind] = b
	_act_buttons["approve"].tooltip_text = "Approve the ticked crates waiting for your approval"
	_act_buttons["reject"].tooltip_text = "Send back (or reject) the ticked crates waiting for your approval, with your note"
	_act_buttons["retry"].tooltip_text = "Run again: held crates run their machine again; scrapped ones go back to the start of the line"
	_act_buttons["scrap"].tooltip_text = "Scrap the ticked crates (ones on a belt or in a machine stop their work first)"
	_sync_actions()
	return col


## Which ticked crates each action applies to.
func _actionable() -> Dictionary:
	var out := {"approve": [], "reject": [], "retry": [], "scrap": []}
	var crates: Dictionary = main.crates if main else {}
	for id in _crate_picked:
		if not crates.has(id):
			continue
		var state: String = crates[id].state
		if state == "awaiting_approval":
			out["approve"].append(id)
			out["reject"].append(id)
			if _sends_back(str(crates[id].location)):
				out["scrap"].append(id)
		elif state == "held":
			out["retry"].append(id)
			out["scrap"].append(id)
		elif state == "scrapped":
			out["retry"].append(id)
		elif state != "shipped":
			out["scrap"].append(id)  # Scrap now: on a belt or in a machine
	return out


func _sync_actions() -> void:
	var can := _actionable()
	for kind in _act_buttons:
		var b: Button = _act_buttons[kind]
		var n: int = (can[kind] as Array).size()
		b.text = "%s%s" % [b.get_meta("words"), (" (%d)" % n) if n > 0 else ""]
		b.disabled = n == 0


func _act_on_picked(kind: String) -> void:
	var ids: Array = _actionable()[kind]
	if ids.is_empty():
		return
	ids.sort()
	main._decide_many(ids, kind, _crate_note.text.strip_edges())
	_crate_note.clear()
	for id in ids:
		_crate_picked.erase(id)
	_refresh_crates()


func _filtered_crates() -> Array:
	var wanted: Array = CRATE_FILTERS[maxi(0, _state_filter.selected)][1]
	var line_id := str(_line_filter_ids[maxi(0, _line_filter.selected)]) if _line_filter.selected < _line_filter_ids.size() else ""
	var needle := _search.text.strip_edges().to_lower()
	var out: Array = []
	for r in _crate_rows():
		if not wanted.is_empty() and not r["state"] in wanted:
			continue
		if not line_id.is_empty() and r["line_id"] != line_id:
			continue
		if not needle.is_empty():
			var hay := ("#%d %s %s %s %s" % [int(r["id"]), r["title"], r["source"], r["at"], r["line"]]).to_lower()
			if not needle in hay:
				continue
		out.append(r)
	var key := _sort_key
	var desc := _sort_desc
	out.sort_custom(func(a: Dictionary, b: Dictionary) -> bool:
		var x = a[key]
		var y = b[key]
		if x == y:
			return int(a["id"]) > int(b["id"])
		return (x > y) if desc else (x < y))
	return out


func _refresh_line_filter() -> void:
	var ids: Array = [""]
	var names: Array = ["Every line"]
	for p in _pipelines():
		ids.append(str(p.get("id", "")))
		names.append(str(p.get("name", p.get("id", ""))))
	if ids == _line_filter_ids:
		return
	var was := str(_line_filter_ids[maxi(0, _line_filter.selected)]) if _line_filter.selected < _line_filter_ids.size() else ""
	_line_filter_ids = ids
	_line_filter.clear()
	for n in names:
		_line_filter.add_item(str(n))
	_line_filter.select(maxi(0, ids.find(was)))
	_line_filter.visible = ids.size() > 2


func _refresh_crates() -> void:
	if not main:
		return
	_refresh_line_filter()
	var rows := _filtered_crates()
	var crates: Dictionary = main.crates
	for id in _crate_picked.keys():
		if not crates.has(id):
			_crate_picked.erase(id)
	_crate_count.text = "%d of %d crates" % [rows.size(), crates.size()]
	for c in CRATE_COLUMNS.size():
		var title: String = CRATE_COLUMNS[c]
		if CRATE_SORT_KEYS[c] == _sort_key:
			title += " ▼" if _sort_desc else " ▲"
		_crate_tree.set_column_title(c, title)
	var now := Time.get_unix_time_from_system()
	var entries: Array = []
	for r in rows:
		entries.append({"key": "C%d" % int(r["id"]), "parent": "", "row": r})
	_sync_tree(_crate_tree, entries, func(item: TreeItem, e: Dictionary) -> void:
		var r: Dictionary = e["row"]
		var id := int(r["id"])
		item.set_cell_mode(0, TreeItem.CELL_MODE_CHECK)
		item.set_editable(0, true)
		item.set_checked(0, _crate_picked.has(id))
		item.set_metadata(0, id)
		item.set_text(0, "#%d" % id)
		item.set_text(1, str(r["title"]))
		item.set_tooltip_text(1, str(r["title"]))
		item.set_text(2, str(r["source"]) if str(r["source"]) != "" else "Typed in")
		item.set_text(3, str(r["line"]))
		item.set_text(4, str(r["at"]))
		var state := str(r["state"])
		var words := str(STATE_WORDS.get(state, state))
		if state == "scrapped" and str(r["scrap_kind"]) != "":
			words += ": " + str(SCRAP_WORDS.get(r["scrap_kind"], r["scrap_kind"]))
		item.set_text(5, words)
		item.set_custom_color(5, Color(STATE_COLORS.get(state, C_QUIET)))
		var tip := words
		if state == "held" or state == "scrapped":
			tip = str(main._scrap_reason(id))
		elif str(r["waiting_for"]) != "":
			tip = str(r["waiting_for"])
		item.set_tooltip_text(5, tip)
		var created := float(r["created"])
		item.set_text(6, _age(now - created) if created > 0.0 else "")
		item.set_text(7, str(int(r["loops"])) if int(r["loops"]) > 0 else "")
		var risk := str(r["risk"])
		item.set_text(8, str(RISK_WORDS.get(risk, "")))
		item.set_custom_color(8, Color(RISK_COLORS.get(risk, C_QUIET)))
		item.set_text(9, _usd(float(r["cost"])) if float(r["cost"]) > 0.0 else "")
		var pr := str(r["pr"])
		item.set_text(10, pr.capitalize() if pr != "" else "")
		item.set_custom_color(10, Color(C_PURPLE if pr == "merged" else (C_GREEN if pr == "open" else C_QUIET)))
		_clear_buttons(item, 11)
		if str(r["pr_url"]) != "":
			_button(item, 11, "pr", BTN_PR, "Open its pull request")
		_button(item, 11, "open", BTN_SHOW, "Show it on the floor, with its log"))
	_sync_actions()


func _on_crate_button(item: TreeItem, _column: int, id: int, _mouse: int) -> void:
	var item_id := int(item.get_metadata(0))
	if id == BTN_PR:
		_open_pr(item_id)
	elif id == BTN_SHOW:
		show_crate.emit(item_id)


func _open_pr(item_id: int) -> void:
	var pr_urls: Dictionary = main.pr_urls
	var url := str(pr_urls.get(item_id, ""))
	if not url.is_empty():
		OS.shell_open(url)


# Storage ------------------------------------------------------------------------

func _build_storage() -> Control:
	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 8)
	_store_status = RichTextLabel.new()
	_store_status.bbcode_enabled = true
	_store_status.fit_content = true
	_store_status.scroll_active = false
	_store_status.text = "[color=#%s]What's in each storage belted into a line, and the repo it's on: the open issues and PRs out there now. Tick some to pull them in as crates, or to close them on GitHub or Jira. Each storage's buttons turn it on or off, check it now, open its window or show it on the floor; New crate queues changes in a Custom storage.[/color]" % C_QUIET
	col.add_child(_store_status)
	_store_tree = _tree(["Item", "Title", "By", "Labels", "Repo", "In the factory", ""], [0, 6, 1, 2, 2, 1, 0])
	_store_tree.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_store_tree.hide_root = true
	_store_tree.set_column_custom_minimum_width(0, 240)
	_store_tree.set_column_custom_minimum_width(4, 130)
	_store_tree.set_column_custom_minimum_width(5, 110)
	_store_tree.set_column_custom_minimum_width(STORE_BUTTONS, 130)
	_store_tree.button_clicked.connect(_on_store_button)
	_store_tree.item_edited.connect(func() -> void:
		var item := _store_tree.get_edited()
		if item:
			var key := str(item.get_metadata(0))
			if item.is_checked(0):
				_store_picked[key] = true
			else:
				_store_picked.erase(key)
			_sync_store_actions())
	_store_tree.item_activated.connect(func() -> void:
		var item := _store_tree.get_selected()
		if item and _store_rows.has(str(item.get_metadata(0))):
			var url := str(_store_rows[str(item.get_metadata(0))].get("url", ""))
			if not url.is_empty():
				OS.shell_open(url))
	col.add_child(_store_tree)
	col.add_child(_build_crate_form())
	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation", 8)
	col.add_child(actions)
	_new_button = Button.new()
	_new_button.text = "New crate"
	_new_button.icon = _icons.get("add")
	_new_button.focus_mode = Control.FOCUS_NONE
	_new_button.pressed.connect(func() -> void: _open_crate_form(""))
	actions.add_child(_new_button)
	var none := Button.new()
	none.text = "Untick all"
	none.theme_type_variation = &"ButtonGhost"
	none.focus_mode = Control.FOCUS_NONE
	none.pressed.connect(func() -> void:
		_store_picked.clear()
		_refresh_storage())
	actions.add_child(none)
	var reload := Button.new()
	reload.text = "Look again"
	reload.icon = load("res://assets/ui/icon_refresh.svg")
	reload.theme_type_variation = &"ButtonGhost"
	reload.focus_mode = Control.FOCUS_NONE
	reload.tooltip_text = "List every storage again now"
	reload.pressed.connect(_ask_storages)
	actions.add_child(reload)
	_store_comment = LineEdit.new()
	_store_comment.placeholder_text = "Comment posted when closing (optional)"
	_store_comment.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	actions.add_child(_store_comment)
	_pull_button = Button.new()
	_pull_button.icon = _icons.get("pull_in")
	_pull_button.focus_mode = Control.FOCUS_NONE
	_pull_button.tooltip_text = "Bring the ticked items into the factory now, as crates"
	_pull_button.pressed.connect(_pull_picked)
	actions.add_child(_pull_button)
	_close_button = Button.new()
	_close_button.theme_type_variation = &"ButtonReject"
	_close_button.icon = _icons.get("bin")
	_close_button.focus_mode = Control.FOCUS_NONE
	_close_button.tooltip_text = "Close the ticked issues and PRs on GitHub (or move the Jira issues to Done), with your comment"
	_close_button.pressed.connect(_ask_close)
	actions.add_child(_close_button)
	_sync_store_actions()
	return col


func _refresh_storage() -> void:
	if not main:
		return
	var entries: Array = []
	var rows := {}
	var heads := {}
	var several := _pipelines().size() > 1
	for p in _pipelines():
		var pid := str(p.get("id", ""))
		var stores := _storages_fed(p)
		if stores.is_empty():
			continue
		var names := {}
		var repos := {}
		for st in stores:
			names[str(st.get("id", ""))] = str(st.get("name", ""))
			repos[str(st.get("id", ""))] = _storage_repo(p, st)
		var listing: Dictionary = _listings.get(pid, {})
		var parent := ""
		if several:
			parent = "P:" + pid
			entries.append({"key": parent, "parent": "", "kind": "line", "name": str(p.get("name", pid)), "loading": listing.is_empty()})
		if listing.is_empty():
			if not several:
				entries.append({"key": "P:" + pid, "parent": "", "kind": "line", "name": str(p.get("name", pid)), "loading": true})
			continue
		for s in listing.get("sources", []):
			var source := str(s.get("source", ""))
			if source == "game":
				continue
			var storage := str(s.get("storage", ""))
			var skey := "S:%s:%s:%s" % [pid, storage, source]
			var items: Array = s.get("items", [])
			entries.append({"key": skey, "parent": parent, "kind": "storage", "source": source,
					"name": str(names.get(storage, "")), "count": items.size(), "enabled": bool(s.get("enabled", false)),
					"disabled": bool(s.get("disabled", false)), "error": str(s.get("error", "")), "about": str(s.get("about", "")), "account": str(s.get("account", "")),
					"repo": repos.get(storage, ["", ""]), "storage": storage})
			var title := str(SOURCE_TITLES.get(source, source))
			if str(names.get(storage, "")) != "":
				title = "%s (%s)" % [str(names[storage]), title]
			if several:
				title += " · " + str(p.get("name", pid))
			heads[skey] = {"pipeline": pid, "storage": storage, "source": source, "title": title,
					"disabled": bool(s.get("disabled", false)), "enabled": bool(s.get("enabled", false))}
			var can_close := bool(s.get("can_close", source in CLOSABLE))
			for it in items:
				if not it is Dictionary:
					continue
				var ref := str(it.get("ref", ""))
				var key := "I:%s:%s:%s:%s" % [pid, storage, source, ref]
				var row := {"pipeline": pid, "source": source, "storage": storage, "ref": ref, "can_close": can_close,
						"url": str(it.get("url", "")), "can_pull": bool(it.get("can_pull", false)) and it.get("item_id") == null,
						"item_id": int(it.get("item_id")) if it.get("item_id") != null else -1}
				rows[key] = row
				entries.append({"key": key, "parent": skey, "kind": "item", "item": it, "row": row})
	_store_rows = rows
	_store_heads = heads
	for key in _store_picked.keys():
		if not rows.has(key):
			_store_picked.erase(key)
	_sync_tree(_store_tree, entries, _fill_store_row)
	_sync_store_actions()
	_sync_crate_form()


func _fill_store_row(item: TreeItem, e: Dictionary) -> void:
	item.set_metadata(0, e["key"])
	_clear_buttons(item, STORE_BUTTONS)
	match str(e["kind"]):
		"line":
			item.set_text(0, str(e["name"]))
			item.set_custom_color(0, Color(C_TEXT))
			item.set_text(1, "Listing its storages…" if bool(e["loading"]) else "")
			item.set_custom_color(1, Color(C_QUIET))
		"storage":
			var title := str(SOURCE_TITLES.get(e["source"], e["source"]))
			if str(e["name"]) != "":
				title = "%s (%s)" % [str(e["name"]), title]
			item.set_text(0, title)
			item.set_tooltip_text(0, title)
			item.set_custom_color(0, Color(C_QUIET) if bool(e.get("disabled", false)) else Color(C_SKY))
			var note := "%d open" % int(e["count"])
			if str(e["account"]) != "":
				note += " · as " + str(e["account"])
			var colour := C_QUIET
			if not bool(e["enabled"]):
				note = str(e["about"]) if str(e["about"]) != "" else "Turned off"
			elif str(e["error"]) != "":
				note = "Couldn't list it: " + str(e["error"])
				colour = C_RED
			item.set_text(1, note)
			item.set_tooltip_text(1, note)
			item.set_custom_color(1, Color(colour))
			var repo: Array = e["repo"]
			item.set_text(4, str(repo[0]))
			item.set_tooltip_text(4, str(repo[1]))
			item.set_custom_color(4, Color(C_QUIET))
			var off := bool(e.get("disabled", false))
			if str(e["source"]) == "custom" and not off:
				_button(item, STORE_BUTTONS, "add", BTN_NEW_CRATE, "New crate: describe a change; it joins this storage's queue")
			if bool(e["enabled"]) and str(e["source"]) != "custom":
				_button(item, STORE_BUTTONS, "refresh", BTN_CHECK, "Check now: look for new ones straight away and bring in any that come in by themselves")
			_button(item, STORE_BUTTONS, "play" if off else "pause", BTN_POWER,
					"Turn on: it's checked and brings crates in again" if off else
					"Turn off: it stays belted in, but isn't checked and brings nothing in until you turn it on")
			_button(item, STORE_BUTTONS, "window", BTN_WINDOW, "Open its window on the floor")
			_button(item, STORE_BUTTONS, "floor", BTN_FLOOR, "Show it on the floor")
		"item":
			var it: Dictionary = e["item"]
			var row: Dictionary = e["row"]
			var closable: bool = row["can_close"]
			item.set_cell_mode(0, TreeItem.CELL_MODE_CHECK)
			item.set_editable(0, true)
			item.set_checked(0, _store_picked.has(e["key"]))
			item.set_text(0, str(it.get("label", it.get("ref", ""))))
			item.set_text(1, str(it.get("title", "")))
			item.set_tooltip_text(1, str(it.get("title", "")))
			item.set_text(2, str(it.get("by", it.get("status", ""))))
			var tags: Array = it.get("tags", []) if it.get("tags") is Array else []
			item.set_text(3, ", ".join(PackedStringArray(tags.map(func(t) -> String: return str(t)))))
			item.set_text(4, str(it.get("repo", "")))
			item.set_text(5, ("#%d" % int(row["item_id"])) if int(row["item_id"]) >= 0 else ("Comes in by itself" if bool(it.get("auto", false)) else ""))
			item.set_custom_color(5, Color(C_GREEN if int(row["item_id"]) >= 0 else C_QUIET))
			if not closable:
				item.set_tooltip_text(0, "This storage's items can't be closed from here")
			if str(row["url"]) != "":
				_button(item, STORE_BUTTONS, "open", BTN_OPEN_URL, "Open it in the browser")
			if bool(row["can_pull"]):
				_button(item, STORE_BUTTONS, "pull_in", BTN_PULL, "Pull it in now, as a crate")
			if int(row["item_id"]) >= 0:
				_button(item, STORE_BUTTONS, "window", BTN_SHOW, "Show its crate on the floor")


func _on_store_button(item: TreeItem, _column: int, id: int, _mouse: int) -> void:
	var key := str(item.get_metadata(0))
	if _store_heads.has(key):
		_on_storage_button(key, id)
		return
	var row: Dictionary = _store_rows.get(key, {})
	if row.is_empty():
		return
	match id:
		BTN_OPEN_URL:
			OS.shell_open(str(row["url"]))
		BTN_PULL:
			_pull([row])
		BTN_SHOW:
			show_crate.emit(int(row["item_id"]))


## A storage row's own buttons.
func _on_storage_button(key: String, id: int) -> void:
	var h: Dictionary = _store_heads[key]
	var pid := str(h["pipeline"])
	var store_key := "%s:%s" % [pid, str(h["storage"])]
	match id:
		BTN_NEW_CRATE:
			_open_crate_form(key)
		BTN_CHECK:
			if main.client.send({"type": "intake_check", "pipeline": pid, "source": h["source"], "storage": h["storage"]}):
				main._toast("Checking %s" % str(h["title"]), Color(0.8, 0.82, 0.86))
			else:
				main._toast("Not connected. Is the backend running?", Color(C_RED))
		BTN_POWER:
			main._set_storage_enabled(pid, str(h["storage"]), bool(h["disabled"]))
			main._toast("Turning %s %s" % [str(h["title"]), "on" if bool(h["disabled"]) else "off"], Color(0.8, 0.82, 0.86))
		BTN_WINDOW:
			open_storage.emit(pid, store_key)
		BTN_FLOOR:
			show_on_floor.emit(store_key)


## The repo a storage's crates are worked on (or that it reads), as [short text, tooltip]:
## "All listed repos" when it reads every repo in the factory's repo list.
func _storage_repo(p: Dictionary, st: Dictionary) -> Array:
	var source := str(st.get("source", ""))
	if bool(st.get("listed_repos_only", false)):
		return ["All listed repos", "Every repo in the factory's repo list (gear, Repos)"]
	var own := str(st.get("repo", "")).strip_edges()
	if not own.is_empty():
		return [own, "Its own repo: " + own]
	if source == "pr_review":
		return ["Any repo", "Every pull request waiting for your review, in any repo"]
	var repo: Dictionary = p.get("repo", {}) if p.get("repo") is Dictionary else {}
	var line := str(repo.get("github", "")).strip_edges()
	if line.is_empty():
		line = str(repo.get("path", "")).strip_edges()
	if line.is_empty():
		return ["No repo yet", "Neither it nor its line has a repo picked (build mode)"]
	return [line, "The line's repo: " + line]


# New crate: a change typed into a Custom storage, queued like in its own window --------

func _build_crate_form() -> Control:
	_crate_form = VBoxContainer.new()
	_crate_form.add_theme_constant_override("separation", 6)
	_crate_form.visible = false
	var head := HBoxContainer.new()
	head.add_theme_constant_override("separation", 8)
	_crate_form.add_child(head)
	var label := Label.new()
	label.text = "New crate in"
	head.add_child(label)
	_crate_where = OptionButton.new()
	_crate_where.focus_mode = Control.FOCUS_NONE
	_crate_where.tooltip_text = "The Custom storage whose queue it joins; the line takes queued changes one crate at a time"
	head.add_child(_crate_where)
	var gap := Control.new()
	gap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(gap)
	_crate_text = TextEdit.new()
	_crate_text.placeholder_text = "Describe a change"
	_crate_text.custom_minimum_size = Vector2(0, 90)
	_crate_text.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_crate_form.add_child(_crate_text)
	var bar := HBoxContainer.new()
	bar.add_theme_constant_override("separation", 8)
	_crate_form.add_child(bar)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bar.add_child(spacer)
	var done := Button.new()
	done.text = "Done"
	done.theme_type_variation = &"ButtonGhost"
	done.focus_mode = Control.FOCUS_NONE
	done.tooltip_text = "Close this box (anything typed and not added is kept for next time)"
	done.pressed.connect(func() -> void: _crate_form.visible = false)
	bar.add_child(done)
	_crate_add = Button.new()
	_crate_add.icon = _icons.get("add")
	_crate_add.text = "Add"
	_crate_add.theme_type_variation = &"ButtonPrimary"
	_crate_add.tooltip_text = "Add: queue this change, then type the next one (Ctrl+Enter)"
	_crate_add.disabled = true
	_crate_add.pressed.connect(_add_crate)
	bar.add_child(_crate_add)
	_crate_text.text_changed.connect(func() -> void: _sync_crate_form())
	_crate_text.gui_input.connect(func(event: InputEvent) -> void:
		var k := event as InputEventKey
		if k and k.pressed and k.keycode in [KEY_ENTER, KEY_KP_ENTER] and k.is_command_or_control_pressed():
			_crate_text.accept_event()
			_add_crate())
	return _crate_form


## Open the New crate box, on that Custom storage (its row key) or the one picked last.
func _open_crate_form(key: String) -> void:
	_sync_crate_form()
	if _crate_where_keys.is_empty():
		return
	if not key.is_empty() and key in _crate_where_keys:
		_crate_where.select(_crate_where_keys.find(key))
	_crate_form.visible = true
	_crate_text.grab_focus.call_deferred()


## The Custom storages switched on, in the picker, and the buttons that need one.
func _sync_crate_form() -> void:
	if not _crate_where:
		return
	var keys: Array = []
	for key in _store_heads:
		var h: Dictionary = _store_heads[key]
		if str(h["source"]) == "custom" and not bool(h["disabled"]):
			keys.append(key)
	if keys != _crate_where_keys:
		var was := str(_crate_where_keys[_crate_where.selected]) if _crate_where.selected >= 0 and _crate_where.selected < _crate_where_keys.size() else ""
		_crate_where.clear()
		for key in keys:
			_crate_where.add_item(str(_store_heads[key]["title"]))
		_crate_where_keys = keys
		if not keys.is_empty():
			_crate_where.select(maxi(0, keys.find(was)))
	_new_button.disabled = keys.is_empty()
	_new_button.tooltip_text = ("New crate: describe a change; it joins a Custom storage's queue" if not keys.is_empty() else
			"No Custom storage switched on is belted into a line (add one in build mode)")
	if keys.is_empty():
		_crate_form.visible = false
	_crate_add.disabled = _crate_text.text.strip_edges().is_empty() or keys.is_empty()


func _add_crate() -> void:
	var text := _crate_text.text.strip_edges()
	var at := _crate_where.selected
	if text.is_empty() or at < 0 or at >= _crate_where_keys.size():
		return
	var h: Dictionary = _store_heads.get(_crate_where_keys[at], {})
	if h.is_empty():
		return
	if main.describe_change(str(h["pipeline"]), text, str(h["storage"])):
		_crate_text.text = ""
		_sync_crate_form()
		_crate_text.grab_focus()


func _picked_store_rows() -> Array:
	var out: Array = []
	for key in _store_picked:
		if _store_rows.has(key):
			out.append(_store_rows[key])
	return out


func _sync_store_actions() -> void:
	if not _close_button:
		return
	var rows := _picked_store_rows()
	var closable := rows.filter(func(r: Dictionary) -> bool: return bool(r["can_close"])).size()
	var pullable := rows.filter(func(r: Dictionary) -> bool: return bool(r["can_pull"])).size()
	_close_button.text = "Close" + ((" (%d)" % closable) if closable > 0 else "")
	_close_button.disabled = closable == 0
	_pull_button.text = "Pull in" + ((" (%d)" % pullable) if pullable > 0 else "")
	_pull_button.disabled = pullable == 0


func _pull_picked() -> void:
	_pull(_picked_store_rows().filter(func(r: Dictionary) -> bool: return bool(r["can_pull"])))


func _pull(rows: Array) -> void:
	var sent := 0
	var lines := {}
	for r in rows:
		if main.client.send({"type": "intake_pull", "pipeline": r["pipeline"], "source": r["source"], "ref": r["ref"],
				"storage": r["storage"]}):
			sent += 1
			lines[r["pipeline"]] = true
	if sent == 0:
		main._toast("Not connected. Is the backend running?", Color(C_RED))
		return
	main._toast("Pulling in %s" % ("1 item" if sent == 1 else "%d items" % sent), Color(0.8, 0.82, 0.86))
	for r in rows:
		_store_picked.erase("I:%s:%s:%s:%s" % [r["pipeline"], r["storage"], r["source"], r["ref"]])
	await get_tree().create_timer(2.0).timeout
	for pid in lines:
		main.client.send({"type": "intake_list", "pipeline": pid})


func _ask_close() -> void:
	var groups := {}
	var count := 0
	for r in _picked_store_rows():
		if not bool(r["can_close"]):
			continue
		var gkey := "%s|%s|%s" % [r["pipeline"], r["source"], r["storage"]]
		if not groups.has(gkey):
			groups[gkey] = {"type": "intake_close", "pipeline": r["pipeline"], "source": r["source"],
					"storage": r["storage"], "refs": [], "comment": ""}
		groups[gkey]["refs"].append(r["ref"])
		count += 1
	if count == 0:
		return
	_pending_close = groups.values()
	var comment := _store_comment.text.strip_edges()
	var jira := _pending_close.any(func(g: Dictionary) -> bool: return g["source"] == "jira")
	_confirm.dialog_text = "Close %s?%s%s\n\nYou can reopen them on GitHub or Jira afterwards." % [
			"1 item" if count == 1 else "%d items" % count,
			("\nJira issues move to their Done status." if jira else ""),
			("\nWith the comment: \"%s\"" % comment.left(120)) if not comment.is_empty() else ""]
	_confirm.popup_centered(Vector2i(420, 0))


func _send_close() -> void:
	var comment := _store_comment.text.strip_edges()
	var sent := 0
	for g in _pending_close:
		g["comment"] = comment
		if main.client.send(g):
			sent += 1
	_pending_close = []
	if sent == 0:
		main._toast("Not connected. Is the backend running?", Color(C_RED))
		return
	_store_comment.clear()
	_store_status.text = "[color=#%s]Closing…[/color]" % C_QUIET


func _show_closed(msg: Dictionary) -> void:
	var closed_refs: Array = msg.get("closed", []) if msg.get("closed") is Array else []
	var failed: Array = msg.get("failed", []) if msg.get("failed") is Array else []
	for ref in closed_refs:
		_store_picked.erase("I:%s:%s:%s:%s" % [str(msg.get("pipeline", "")), str(msg.get("storage", "")), str(msg.get("source", "")), str(ref)])
	var bits := PackedStringArray()
	if not closed_refs.is_empty():
		bits.append("[color=#%s]Closed %s.[/color]" % [C_GREEN, ", ".join(PackedStringArray(closed_refs.map(func(r) -> String: return str(r))))])
	for f in failed:
		if f is Dictionary:
			bits.append("[color=#%s]Couldn't close %s: %s[/color]" % [C_RED, str(f.get("ref", "")), str(f.get("error", ""))])
	if not bits.is_empty():
		_store_status.text = "  ".join(bits)
	_sync_store_actions()


# Worktrees & branches -------------------------------------------------------------

func _build_worktrees() -> Control:
	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 8)
	_wt_status = RichTextLabel.new()
	_wt_status.bbcode_enabled = true
	_wt_status.fit_content = true
	_wt_status.scroll_active = false
	_wt_status.text = "[color=#%s]Looking at the repos…[/color]" % C_QUIET
	col.add_child(_wt_status)
	_wt_tree = _tree(["Name", "Repo", "Branch / worktree", "Status", "Last activity", "Why", ""], [4, 2, 3, 2, 1, 5, 0])
	_wt_tree.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_wt_tree.set_column_custom_minimum_width(0, 200)
	_wt_tree.set_column_custom_minimum_width(3, 110)
	_wt_tree.set_column_custom_minimum_width(4, 90)
	_wt_tree.set_column_custom_minimum_width(6, 70)
	_wt_tree.button_clicked.connect(_on_wt_button)
	_wt_tree.item_edited.connect(func() -> void:
		var item := _wt_tree.get_edited()
		if item:
			var key := str(item.get_metadata(0))
			if item.is_checked(0):
				_wt_picked[key] = true
			else:
				_wt_picked.erase(key)
			_sync_wt_actions())
	col.add_child(_wt_tree)
	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation", 8)
	col.add_child(actions)
	_wt_show = OptionButton.new()
	_wt_show.add_item("Show everything")
	_wt_show.add_item("Show stale only")
	_wt_show.focus_mode = Control.FOCUS_NONE
	_wt_show.item_selected.connect(func(_i: int) -> void: _refresh_worktrees())
	actions.add_child(_wt_show)
	var stale := Button.new()
	stale.text = "Tick stale"
	stale.theme_type_variation = &"ButtonGhost"
	stale.focus_mode = Control.FOCUS_NONE
	stale.tooltip_text = "Tick every stale worktree and branch"
	stale.pressed.connect(func() -> void:
		for key in _wt_rows:
			if bool(_wt_rows[key].get("stale", false)):
				_wt_picked[key] = true
		_refresh_worktrees())
	actions.add_child(stale)
	var none := Button.new()
	none.text = "Untick all"
	none.theme_type_variation = &"ButtonGhost"
	none.focus_mode = Control.FOCUS_NONE
	none.pressed.connect(func() -> void:
		_wt_picked.clear()
		_refresh_worktrees())
	actions.add_child(none)
	var reload := Button.new()
	reload.text = "Look again"
	reload.icon = load("res://assets/ui/icon_refresh.svg")
	reload.theme_type_variation = &"ButtonGhost"
	reload.focus_mode = Control.FOCUS_NONE
	reload.tooltip_text = "List every worktree and branch again now"
	reload.pressed.connect(func() -> void:
		_wt_result = ""
		_ask_worktrees())
	actions.add_child(reload)
	var gap := Control.new()
	gap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	actions.add_child(gap)
	_wt_clean_button = Button.new()
	_wt_clean_button.icon = _icons.get("bin")
	_wt_clean_button.focus_mode = Control.FOCUS_NONE
	_wt_clean_button.tooltip_text = ("Remove the ticked stale worktrees and branches. Your own worktrees go only if they have "
			+ "no uncommitted changes, and your own branches only if git says they're merged.")
	_wt_clean_button.pressed.connect(func() -> void: _ask_worktrees_clean(false))
	actions.add_child(_wt_clean_button)
	_wt_force_button = Button.new()
	_wt_force_button.theme_type_variation = &"ButtonReject"
	_wt_force_button.icon = _icons.get("bin")
	_wt_force_button.focus_mode = Control.FOCUS_NONE
	_wt_force_button.tooltip_text = ("Remove the ticked ones even if they aren't stale: throws away uncommitted changes, "
			+ "deletes unmerged branches, unlocks locked worktrees and stops programs running in them. "
			+ "Never touches a crate still in the line, the base branch or a repo's own checkout.")
	_wt_force_button.pressed.connect(func() -> void: _ask_worktrees_clean(true))
	actions.add_child(_wt_force_button)
	_sync_wt_actions()
	return col


func _ask_worktrees() -> void:
	_wt_tick = 0.0
	if main:
		main.client.send({"type": "worktrees"})


func _refresh_worktrees() -> void:
	if not _wt_tree:
		return
	if str(_wt_list.get("error", "")) != "":
		_wt_status.text = "[color=#%s]Couldn't list them: %s[/color]" % [C_RED, str(_wt_list["error"])]
	var stale_only := _wt_show.selected == 1
	var entries: Array = []
	var rows := {}
	var now := Time.get_unix_time_from_system()
	for group in [["worktrees", "Worktrees"], ["branches", "Branches"]]:
		var list: Array = _wt_list.get(group[0], []) if _wt_list.get(group[0]) is Array else []
		var stale := list.filter(func(r) -> bool: return r is Dictionary and bool(r.get("stale", false))).size()
		var gkey := "G:" + str(group[0])
		entries.append({"key": gkey, "parent": "", "kind": "group", "name": str(group[1]), "count": list.size(), "stale": stale})
		for r in list:
			if not r is Dictionary:
				continue
			rows[str(r["key"])] = r
			if stale_only and not bool(r.get("stale", false)):
				continue
			entries.append({"key": str(r["key"]), "parent": gkey, "kind": "row", "row": r, "now": now})
	_wt_rows = rows
	for key in _wt_picked.keys():
		if not rows.has(key) or not bool(rows[key].get("can_force", false)):
			_wt_picked.erase(key)
	_sync_tree(_wt_tree, entries, _fill_wt_row)
	if not _wt_list.is_empty() and str(_wt_list.get("error", "")) == "":
		var stale := rows.values().filter(func(r: Dictionary) -> bool: return bool(r.get("stale", false))).size()
		_wt_status.text = ("[color=#%s]%d worktrees and %d branches, [/color][color=#%s]%d stale[/color][color=#%s]. Stale: a crate's that "
				+ "shipped, was scrapped or has no crate, or whose PR is merged or closed; anything else merged into the base branch, "
				+ "with its remote branch gone, or with no commits for %d days. Crates still in the line are never touched.[/color]") % [
				C_QUIET, (_wt_list.get("worktrees", []) as Array).size(), (_wt_list.get("branches", []) as Array).size(),
				C_AMBER if stale > 0 else C_GREEN, stale, C_QUIET, int(_wt_list.get("idle_days", 14))]
	if _wt_result != "":
		_wt_status.text += "\n" + _wt_result
	_sync_wt_actions()


func _fill_wt_row(item: TreeItem, e: Dictionary) -> void:
	item.set_metadata(0, e["key"])
	_clear_buttons(item, 6)
	if str(e["kind"]) == "group":
		item.set_text(0, "%s (%d)" % [str(e["name"]), int(e["count"])])
		item.set_custom_color(0, Color(C_SKY))
		item.set_text(3, ("%d stale" % int(e["stale"])) if int(e["stale"]) > 0 else "")
		item.set_custom_color(3, Color(C_AMBER))
		return
	var r: Dictionary = e["row"]
	var is_wt := str(r.get("kind", "")) == "worktree"
	var can_force := bool(r.get("can_force", false))
	item.set_cell_mode(0, TreeItem.CELL_MODE_CHECK)
	item.set_editable(0, can_force)
	item.set_checked(0, _wt_picked.has(e["key"]))
	item.set_text(0, str(r.get("name", "")))
	item.set_tooltip_text(0, str(r.get("path", r.get("name", ""))))
	item.set_text(1, str(r.get("repo", "")) if str(r.get("repo", "")) != "" else "None")
	item.set_custom_color(1, Color(C_QUIET))
	var other := str(r.get("branch", "")) if is_wt else str(r.get("worktree", "")).get_file()
	if is_wt and other == "" and not bool(r.get("missing", false)) and str(r.get("repo", "")) != "":
		other = "(detached)"
	item.set_text(2, other)
	item.set_tooltip_text(2, ("Checked out in " + str(r.get("worktree", ""))) if not is_wt and other != "" else other)
	var status := "Active"
	var colour := C_GREEN
	var crate = r.get("crate")
	if bool(r.get("in_use", false)):
		status = "In use"
		colour = C_BLUE
		item.set_tooltip_text(3, "Crate #%d is %s: it's never removed from here" % [int(crate), str(STATE_WORDS.get(r.get("crate_state", ""), r.get("crate_state", ""))).to_lower()])
	elif str(r.get("protected", "")) != "":
		status = "Kept"
		colour = C_QUIET
		item.set_tooltip_text(3, "Never removed: it's " + str(r["protected"]))
	elif bool(r.get("stale", false)):
		status = "Stale"
		colour = C_AMBER
		item.set_tooltip_text(3, "Clean or Force clean removes it")
	else:
		item.set_tooltip_text(3, "Still wanted: only Force clean removes it")
	item.set_text(3, status)
	item.set_custom_color(3, Color(colour))
	var last := float(r.get("last", 0.0))
	item.set_text(4, _age(maxf(0.0, float(e["now"]) - last)) if last > 0.0 else "")
	item.set_tooltip_text(4, Time.get_datetime_string_from_unix_time(int(last), true) if last > 0.0 else "")
	var why := PackedStringArray()
	for reason in r.get("reasons", []):
		why.append(str(reason))
	if crate != null and bool(r.get("in_use", false)):
		why.append("crate #%d is %s" % [int(crate), str(STATE_WORDS.get(r.get("crate_state", ""), r.get("crate_state", ""))).to_lower()])
	if str(r.get("protected", "")) != "":
		why.append(str(r["protected"]))
	if int(r.get("dirty", 0)) > 0:
		why.append("%d uncommitted change%s" % [int(r["dirty"]), "" if int(r["dirty"]) == 1 else "s"])
	if bool(r.get("locked", false)):
		why.append("locked")
	if not is_wt and bool(r.get("merged", false)) and crate != null:
		why.append("merged")
	item.set_text(5, ", ".join(why))
	item.set_tooltip_text(5, ", ".join(why))
	item.set_custom_color(5, Color(C_TEXT if bool(r.get("stale", false)) else C_QUIET))
	if is_wt and not bool(r.get("missing", false)) and str(r.get("path", "")) != "":
		_button(item, 6, "open", BTN_FOLDER, "Open the folder")
	if str(r.get("pr_url", "")) != "":
		_button(item, 6, "pr", BTN_PR, "Open its PR")
	if crate != null and str(r.get("crate_state", "")) != "":
		_button(item, 6, "window", BTN_SHOW, "Show crate #%d on the floor" % int(crate))


func _on_wt_button(item: TreeItem, _column: int, id: int, _mouse: int) -> void:
	var r: Dictionary = _wt_rows.get(str(item.get_metadata(0)), {})
	if r.is_empty():
		return
	match id:
		BTN_FOLDER:
			OS.shell_open(str(r["path"]))
		BTN_PR:
			OS.shell_open(str(r["pr_url"]))
		BTN_SHOW:
			show_crate.emit(int(r["crate"]))


func _picked_wt(force: bool) -> Array:
	var out: Array = []
	for key in _wt_picked:
		var r: Dictionary = _wt_rows.get(key, {})
		if not r.is_empty() and bool(r.get("can_force" if force else "can_clean", false)):
			out.append(r)
	return out


func _sync_wt_actions() -> void:
	if not _wt_clean_button:
		return
	var gentle := _picked_wt(false).size()
	var forced := _picked_wt(true).size()
	_wt_clean_button.text = "Clean" + ((" (%d)" % gentle) if gentle > 0 else "")
	_wt_clean_button.disabled = gentle == 0
	_wt_force_button.text = "Force clean" + ((" (%d)" % forced) if forced > 0 else "")
	_wt_force_button.disabled = forced == 0


func _ask_worktrees_clean(force: bool) -> void:
	var rows := _picked_wt(force)
	if rows.is_empty():
		return
	var wts := rows.filter(func(r: Dictionary) -> bool: return str(r.get("kind", "")) == "worktree").size()
	var brs := rows.size() - wts
	var bits := PackedStringArray()
	if wts > 0:
		bits.append("1 worktree" if wts == 1 else "%d worktrees" % wts)
	if brs > 0:
		bits.append("1 branch" if brs == 1 else "%d branches" % brs)
	var names := PackedStringArray()
	for r in rows.slice(0, 8):
		names.append("  " + str(r.get("name", "")))
	if rows.size() > 8:
		names.append("  …and %d more" % (rows.size() - 8))
	var skipped := _wt_picked.size() - rows.size()
	var warn := ""
	if force:
		var dirty := rows.filter(func(r: Dictionary) -> bool: return int(r.get("dirty", 0)) > 0).size()
		var fresh := rows.filter(func(r: Dictionary) -> bool: return not bool(r.get("stale", false))).size()
		warn = "\n\nForce clean deletes them even with uncommitted changes or unmerged commits, and stops programs running in the folders."
		if fresh > 0:
			warn += "\n%d of them aren't stale." % fresh
		if dirty > 0:
			warn += "\n%d worktree%s uncommitted changes that will be lost." % [dirty, " has" if dirty == 1 else "s have"]
	_wt_confirm.title = "Force clean?" if force else "Clean up?"
	_wt_confirm.ok_button_text = "Force clean" if force else "Clean"
	_wt_confirm.dialog_text = "Delete %s?\n%s%s%s\n\nThis can't be undone (a deleted branch is gone unless it was pushed)." % [
			" and ".join(bits), "\n".join(names), warn,
			("\n\n%d ticked one%s isn't stale, so Clean leaves it." % [skipped, "" if skipped == 1 else "s"]) if skipped > 0 and not force else ""]
	_wt_pending = {"type": "worktrees_clean", "force": force, "keys": rows.map(func(r: Dictionary) -> String: return str(r["key"]))}
	_wt_confirm.popup_centered(Vector2i(460, 0))


func _send_worktrees_clean() -> void:
	if _wt_pending.is_empty():
		return
	var msg := _wt_pending
	_wt_pending = {}
	if not main.client.send(msg):
		main._toast("Not connected. Is the backend running?", Color(C_RED))
		return
	_wt_status.text = "[color=#%s]%s…[/color]" % [C_QUIET, "Force cleaning" if bool(msg["force"]) else "Cleaning"]


func _show_worktrees_cleaned(msg: Dictionary) -> void:
	var removed: Array = msg.get("removed", []) if msg.get("removed") is Array else []
	var failed: Array = msg.get("failed", []) if msg.get("failed") is Array else []
	_wt_picked.clear()
	for f in failed:
		if f is Dictionary:
			_wt_picked[str(f.get("key", ""))] = true  # still ticked, to try again (or force)
	var bits := PackedStringArray()
	if not removed.is_empty():
		bits.append("[color=#%s]Removed %d: %s.[/color]" % [C_GREEN, removed.size(), ", ".join(PackedStringArray(removed.map(func(x) -> String: return str(x))))])
	for f in failed:
		if f is Dictionary:
			bits.append("[color=#%s]Kept %s: %s[/color]" % [C_RED, str(f.get("what", "")), str(f.get("error", ""))])
	if bits.is_empty():
		bits.append("[color=#%s]Nothing to remove.[/color]" % C_QUIET)
	_wt_result = "  ".join(bits)  # shown under the summary until you look again
	main._toast("Removed %d, kept %d" % [removed.size(), failed.size()] if not failed.is_empty() else "Removed %d" % removed.size(),
			Color(0.8, 0.82, 0.86))
	_refresh_worktrees()


# Trees --------------------------------------------------------------------------

func _tree(titles: Array, ratios: Array) -> Tree:
	var tree := Tree.new()
	tree.columns = titles.size()
	tree.column_titles_visible = true
	tree.hide_root = true
	tree.select_mode = Tree.SELECT_ROW
	tree.focus_mode = Control.FOCUS_CLICK
	for i in titles.size():
		tree.set_column_title(i, str(titles[i]))
		tree.set_column_title_alignment(i, HORIZONTAL_ALIGNMENT_LEFT)
		var ratio := int(ratios[i]) if i < ratios.size() else 1
		tree.set_column_expand(i, ratio > 0)
		if ratio > 0:
			tree.set_column_expand_ratio(i, ratio)
		tree.set_column_clip_content(i, true)
	return tree


## Bring a tree up to date with `entries` ({key, parent, ...} in order), filling each row with
## `fill`. While the same rows are shown, they're updated in place (ticks, selection, scrolling
## and folded lines stay); when rows come or go, it's rebuilt, keeping the selection and scroll.
func _sync_tree(tree: Tree, entries: Array, fill: Callable) -> void:
	var keys := PackedStringArray()
	for e in entries:
		keys.append("%s>%s" % [str(e["parent"]), str(e["key"])])
	var signature := "|".join(keys)
	var items: Dictionary = tree.get_meta("items", {})
	if str(tree.get_meta("signature", "")) != signature:
		var selected := ""
		if tree.get_selected():
			selected = str(tree.get_selected().get_metadata(0))
		var folded: Dictionary = {}
		for k in items:
			var old: TreeItem = items[k]
			if is_instance_valid(old) and old.collapsed:
				folded[k] = true
		var bar := _vscroll(tree)
		var scroll := bar.value if bar else 0.0
		tree.clear()
		var root := tree.create_item()
		items = {}
		for e in entries:
			var parent: TreeItem = items.get(str(e["parent"]), root)
			var item := tree.create_item(parent)
			item.collapsed = folded.has(str(e["key"]))
			items[str(e["key"])] = item
		tree.set_meta("items", items)
		tree.set_meta("signature", signature)
		for e in entries:
			fill.call(items[str(e["key"])], e)
		if not selected.is_empty():
			for k in items:
				var it: TreeItem = items[k]
				if str(it.get_metadata(0)) == selected:
					it.select(0)
					break
		if bar:
			bar.value = scroll
			# The scrollbar's range only grows once the tree has laid out its new rows.
			(func() -> void: bar.value = scroll).call_deferred()
		return
	for e in entries:
		fill.call(items[str(e["key"])], e)


func _vscroll(tree: Tree) -> VScrollBar:
	for child in tree.get_children(true):
		if child is VScrollBar:
			return child
	return null


func _clear_buttons(item: TreeItem, column: int) -> void:
	while item.get_button_count(column) > 0:
		item.erase_button(column, 0)


func _button(item: TreeItem, column: int, icon: String, id: int, tip: String) -> void:
	var key := "small_" + icon
	if not _icons.has(key):
		var tex: Texture2D = _icons.get(icon, _icons.get("open"))
		if tex == null:
			return
		var img := tex.get_image()  # the icons are 32 px: rows want them smaller
		img.resize(18, 18, Image.INTERPOLATE_LANCZOS)
		_icons[key] = ImageTexture.create_from_image(img)
	item.add_button(column, _icons[key], id, false, tip)


# Words ---------------------------------------------------------------------------

## "claude-opus-5-5", "high" -> "Opus 5.5 · high". "default" is the claude CLI's own choice;
## "unknown" marks runs from before the dashboard recorded the model.
## Other agents are named first: "gemini", "flash" -> "Gemini · flash · default effort".
static func _agent_label(model: String, effort: String, agent: String = "") -> String:
	if model == "unknown":
		return "Not recorded (older runs)"
	var name := "CLI default model"
	if agent not in ["", "claude"]:
		var agent_names := {"gemini": "Gemini", "antigravity": "Antigravity", "copilot": "Copilot"}
		name = "%s · %s" % [agent_names.get(agent, agent.capitalize()),
				model if model != "default" and not model.is_empty() else "default model"]
	elif model != "default" and not model.is_empty():
		var parts := model.trim_prefix("claude-").split("-")
		name = parts[0].capitalize()
		if parts.size() > 1:
			name += " " + ".".join(parts.slice(1))
	return "%s · %s" % [name, "default effort" if effort in ["default", ""] else effort]


## 1234567 -> "1.2M", 45200 -> "45.2k".
static func _count(n: int) -> String:
	if n >= 1000000:
		return "%.1fM" % (n / 1000000.0)
	if n >= 1000:
		return "%.1fk" % (n / 1000.0)
	return str(n)


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


static func _age(s: float) -> String:
	if s < 60.0:
		return "now"
	if s < 3600.0:
		return "%dm" % int(s / 60.0)
	if s < 86400.0 * 2:
		return "%dh" % int(s / 3600.0)
	return "%dd" % int(s / 86400.0)


# Charts --------------------------------------------------------------------------

## A small chart drawn by hand in the game's colours: stacked columns (one per day) or
## horizontal bars (one per row). Hover a column or bar for its numbers.
class Chart extends Control:
	var kind := "bars"
	var labels: Array = []
	var series: Array = []  # columns: one Array of values per series, stacked
	var colors: Array = []
	var tips: Array = []
	var rows: Array = []  # bars: [{label, parts: [[value, Color]...], text, tip}]
	var usd := false
	var empty_text := "Nothing yet."

	const ROW := 24.0
	const FONT_SIZE := 12

	func _init() -> void:
		mouse_filter = Control.MOUSE_FILTER_PASS
		clip_contents = true

	func set_columns(l: Array, s: Array, c: Array, t: Array, as_usd: bool) -> void:
		kind = "columns"
		labels = l
		series = s
		colors = c
		tips = t
		usd = as_usd
		custom_minimum_size.y = 160
		queue_redraw()

	func set_bars(r: Array, empty: String) -> void:
		kind = "bars"
		rows = r
		empty_text = empty
		custom_minimum_size.y = maxf(48.0, r.size() * ROW)
		queue_redraw()

	func _fmt(v: float) -> String:
		if usd:
			return "$%.2f" % v if v >= 0.1 or v <= 0.0 else "$%.3f" % v
		return str(roundi(v))

	func _draw() -> void:
		var font := get_theme_default_font()
		if kind == "columns":
			_draw_columns(font)
		else:
			_draw_bars(font)

	func _draw_empty(font: Font) -> void:
		draw_string(font, Vector2(0, 20), empty_text, HORIZONTAL_ALIGNMENT_LEFT, size.x, FONT_SIZE, Color("9AA1AD"))

	func _draw_columns(font: Font) -> void:
		var n := labels.size()
		var most := 0.0
		for i in n:
			var total := 0.0
			for s in series:
				total += float(s[i])
			most = maxf(most, total)
		if n == 0 or most <= 0.0:
			empty_text = "Nothing in this time."
			_draw_empty(font)
			return
		var quiet := Color("9AA1AD")
		var top := 18.0
		var bottom := 20.0
		var h := size.y - top - bottom
		var slot := size.x / n
		var bar_w := clampf(slot * 0.68, 2.0, 46.0)
		for k in 3:  # faint guide lines
			var gy := top + h * k / 2.0
			draw_line(Vector2(0, gy), Vector2(size.x, gy), Color(1, 1, 1, 0.05))
		draw_string(font, Vector2(0, 12), _fmt(most), HORIZONTAL_ALIGNMENT_LEFT, -1, FONT_SIZE - 1, quiet)
		var step := maxi(1, ceili(n * 52.0 / maxf(size.x, 1.0)))
		for i in n:
			var x := i * slot + (slot - bar_w) / 2.0
			var y := top + h
			var total := 0.0
			for k in series.size():
				var v := float(series[k][i])
				total += v
				if v <= 0.0:
					continue
				var bh := h * v / most
				y -= bh
				draw_rect(Rect2(x, y, bar_w, bh), colors[k] if k < colors.size() else Color.WHITE)
			if n <= 14 and total > 0.0 and not usd:
				draw_string(font, Vector2(i * slot, y - 3), _fmt(total), HORIZONTAL_ALIGNMENT_CENTER, slot, FONT_SIZE - 1, Color("C8CDD6"))
			if i % step == 0:
				draw_string(font, Vector2(i * slot - slot * (step - 1) / 2.0, size.y - 5), str(labels[i]),
						HORIZONTAL_ALIGNMENT_CENTER, slot * step, FONT_SIZE - 1, quiet)
		draw_line(Vector2(0, top + h), Vector2(size.x, top + h), Color(1, 1, 1, 0.15))

	func _draw_bars(font: Font) -> void:
		if rows.is_empty():
			_draw_empty(font)
			return
		var most := 0.0
		for r in rows:
			var total := 0.0
			for p in r.get("parts", []):
				total += float(p[0])
			most = maxf(most, total)
		var label_w := minf(size.x * 0.36, 190.0)
		var value_w := 76.0
		var bar_w := maxf(10.0, size.x - label_w - value_w)
		var text_col := Color("C8CDD6")
		var quiet := Color("9AA1AD")
		for i in rows.size():
			var r: Dictionary = rows[i]
			var y := i * ROW
			draw_string(font, Vector2(0, y + 16), _fit(font, str(r.get("label", "")), label_w - 8), HORIZONTAL_ALIGNMENT_LEFT,
					-1, FONT_SIZE, text_col)
			var x := label_w
			draw_rect(Rect2(x, y + 5, bar_w, ROW - 10), Color(1, 1, 1, 0.05))
			if most > 0.0:
				for p in r.get("parts", []):
					var pw := bar_w * float(p[0]) / most
					if pw > 0.0:
						draw_rect(Rect2(x, y + 5, pw, ROW - 10), p[1])
						x += pw
			draw_string(font, Vector2(size.x - value_w + 8, y + 16), str(r.get("text", "")), HORIZONTAL_ALIGNMENT_LEFT,
					value_w - 8, FONT_SIZE, quiet)

	func _fit(font: Font, text: String, width: float) -> String:
		if font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, FONT_SIZE).x <= width:
			return text
		while text.length() > 1 and font.get_string_size(text + "…", HORIZONTAL_ALIGNMENT_LEFT, -1, FONT_SIZE).x > width:
			text = text.left(-1)
		return text + "…"

	func _get_tooltip(at: Vector2) -> String:
		if kind == "columns":
			if labels.is_empty():
				return ""
			var i := clampi(int(at.x / (size.x / labels.size())), 0, labels.size() - 1)
			return str(tips[i]) if i < tips.size() else ""
		var row := int(at.y / ROW)
		if row < 0 or row >= rows.size():
			return ""
		var r: Dictionary = rows[row]
		return str(r.get("tip", "%s: %s" % [str(r.get("label", "")), str(r.get("text", ""))]))
