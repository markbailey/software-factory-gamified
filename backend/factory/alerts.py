"""Alerts outside the game: a desktop notification and/or a message to a webhook (ntfy, Discord,
Slack or anything that takes JSON) when a crate waits for your approval, is held or scrapped,
ships, Claude goes away or the day's budget is spent. Set up under [alerts] in factory.toml; everything is off by default.

[alerts]
desktop = true                       # a Windows/macOS/Linux notification on this computer
webhook = "https://ntfy.sh/my-factory-topic"   # or a Discord/Slack incoming webhook URL
on = ["approval", "held", "scrapped", "claude_down", "budget"]   # add "shipped" for every PR
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

DEFAULT_ON = ("approval", "held", "scrapped", "claude_down", "budget")
KINDS = ("approval", "held", "scrapped", "shipped", "claude_down", "claude_back", "budget")
TITLE = "Software Factory"


class Alerts:
    def __init__(self, settings: dict, store, title: str = TITLE):
        self.desktop = bool(settings.get("desktop", False))
        self.webhook = str(settings.get("webhook", "") or "").strip()
        self.on = {str(k) for k in settings.get("on", DEFAULT_ON)}
        self.store = store
        self.title = title
        self._claude_down = False
        self._tasks: set[asyncio.Task] = set()
        self._warned: set[str] = set()

    @property
    def enabled(self) -> bool:
        return (self.desktop or bool(self.webhook)) and bool(self.on)

    # Which events become alerts -----------------------------------------------
    def listen(self, event: dict) -> None:
        if not self.enabled:
            return
        found = self.message_for(event)
        if found and found[0] in self.on:
            task = asyncio.get_running_loop().create_task(self.send(*found))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def message_for(self, event: dict) -> tuple[str, str] | None:
        """(kind, text) for an event worth an alert, else None."""
        kind = event.get("type")
        if kind == "item_held":
            return "held", f"{self._crate(event)} is held: {event.get('reason', '')}".strip()
        if kind == "item_moved" and event.get("state") == "awaiting_approval":
            return "approval", f"{self._crate(event)} is waiting for your approval."
        if kind == "item_moved" and event.get("state") in ("shipped", "scrapped"):
            item = self._item(event)
            if event["state"] == "shipped":
                pr = f" {item.get('pr_url')}" if item.get("pr_url") else ""
                return "shipped", f"{self._crate(event)} shipped.{pr}"
            return "scrapped", f"{self._crate(event)} was scrapped" + (
                f" ({item.get('scrap_kind', '').replace('_', ' ')})." if item.get("scrap_kind") else ".")
        if kind == "agent_status" and event.get("enabled", True):
            down = not event.get("available", True)
            if down == self._claude_down:
                return None
            self._claude_down = down
            if down:
                return "claude_down", f"Claude is unavailable: {event.get('reason', '')}. Crates wait in front of Claude machines."
            return "claude_back", "Claude is back; crates carry on."
        if kind == "budget_spent":
            return "budget", (f"Today's Claude budget (${float(event.get('daily_usd', 0)):.2f}) is spent: crates "
                              "wait in front of Claude machines until midnight.")
        return None

    def _item(self, event: dict) -> dict:
        try:
            return self.store.get_item(int(event.get("item_id"))) or {}
        except Exception:
            return {}

    def _crate(self, event: dict) -> str:
        item = self._item(event)
        title = (str(item.get("text", "")).strip().splitlines() or [""])[0][:80]
        return f"Crate #{event.get('item_id')}" + (f" ({title})" if title else "")

    # Sending ------------------------------------------------------------------
    async def send(self, kind: str, text: str) -> None:
        if self.desktop:
            await asyncio.to_thread(self._desktop, text)
        if self.webhook:
            await asyncio.to_thread(self._post, kind, text)

    def _warn_once(self, key: str, why: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[alerts] {why}", flush=True)

    def _desktop(self, text: str) -> None:
        try:
            if sys.platform == "win32":
                _windows_toast(self.title, text)
            elif sys.platform == "darwin":
                script = f"display notification {json.dumps(text)} with title {json.dumps(self.title)}"
                subprocess.run(["osascript", "-e", script], timeout=15, capture_output=True, check=True)
            elif shutil.which("notify-send"):
                subprocess.run(["notify-send", self.title, text], timeout=15, capture_output=True, check=True)
            else:
                self._warn_once("desktop", "no way to show desktop notifications here (notify-send isn't installed)")
        except Exception as exc:
            self._warn_once("desktop", f"couldn't show a desktop notification: {exc}")

    def _post(self, kind: str, text: str) -> None:
        url = self.webhook
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            data, headers = json.dumps({"content": f"**{self.title}**: {text}"}).encode(), {"Content-Type": "application/json"}
        elif "hooks.slack.com" in url:
            data, headers = json.dumps({"text": f"*{self.title}*: {text}"}).encode(), {"Content-Type": "application/json"}
        elif "ntfy" in url:
            tags = {"held": "warning", "scrapped": "wastebasket", "shipped": "package",
                    "approval": "raised_hand", "claude_down": "rotating_light", "claude_back": "white_check_mark", "budget": "moneybag"}
            data, headers = text.encode("utf-8"), {"Title": self.title, "Tags": tags.get(kind, "factory")}
        else:
            data = json.dumps({"title": self.title, "kind": kind, "text": text}).encode()
            headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(url, data=data, headers={**headers, "User-Agent": "software-factory"},
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except Exception as exc:
            self._warn_once("webhook", f"couldn't send an alert to the webhook: {exc}")


def _windows_toast(title: str, text: str) -> None:
    """A Windows notification through PowerShell's built-in toast API: an inline command, not a
    script file, so it works where running .ps1 scripts is blocked."""
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "''"))
    xml = ('<toast><visual><binding template="ToastGeneric">'
           f'<text>{esc(title)}</text><text>{esc(text)}</text></binding></visual></toast>')
    app = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
    command = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        f"$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('{xml}');"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], timeout=20,
                   capture_output=True, check=True, creationflags=flags)
