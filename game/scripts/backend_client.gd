extends Node
## Keeps a WebSocket open to the backend, reconnecting every couple of seconds if it drops.
## Emits each JSON message from the backend as a Dictionary.

signal connected
signal disconnected
signal message_received(msg: Dictionary)

const RETRY_SECONDS := 2.0

var url := "ws://127.0.0.1:8765/ws"

var _ws := WebSocketPeer.new()
var _was_open := false
var _retry_timer := 0.0


func _ready() -> void:
	_open()


func _open() -> void:
	_ws = WebSocketPeer.new()
	_ws.inbound_buffer_size = 4 * 1024 * 1024  # snapshots can be large
	_retry_timer = RETRY_SECONDS
	_ws.connect_to_url(url)


func _process(delta: float) -> void:
	_ws.poll()
	var state := _ws.get_ready_state()
	if state == WebSocketPeer.STATE_OPEN:
		if not _was_open:
			_was_open = true
			connected.emit()
		while _ws.get_available_packet_count() > 0:
			var text := _ws.get_packet().get_string_from_utf8()
			var data = JSON.parse_string(text)
			if data is Dictionary:
				message_received.emit(data)
	elif state == WebSocketPeer.STATE_CLOSED:
		if _was_open:
			_was_open = false
			disconnected.emit()
		_retry_timer -= delta
		if _retry_timer <= 0.0:
			_open()


func send(msg: Dictionary) -> bool:
	if _ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return false
	return _ws.send_text(JSON.stringify(msg)) == OK
