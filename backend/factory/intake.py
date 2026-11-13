"""Intake adapters: turn issues in GitHub or Jira into crates, and report back on them.

Each tracker has one small adapter with two jobs:
- poll() finds new labelled issues and submits them as crates (each issue only once);
- notify() posts a comment when the crate's PR opens or the crate is scrapped.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import urllib.request

from .config import GitHubIntakeConfig, JiraIntakeConfig
from .engine import Factory

MAX_TEXT = 4000


def _spec(title: str, body: str, ref: str) -> str:
    text = f"{title.strip()}\n\n{(body or '').strip()}".strip()
    return f"{text}\n\n(From {ref})"[:MAX_TEXT]


class GitHubIntake:
    source = "github"

    def __init__(self, cfg: GitHubIntakeConfig, factory: Factory):
        self.cfg = cfg
        self.factory = factory

    def _gh(self, *args: str, stdin: str | None = None) -> str:
        gh = shutil.which("gh")
        if not gh:
            raise RuntimeError("gh is not installed or not on PATH")
        r = subprocess.run([gh, *args], input=stdin, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        return r.stdout

    async def poll(self) -> int:
        out = await asyncio.to_thread(
            self._gh, "issue", "list", "--repo", self.cfg.repo, "--label", self.cfg.label,
            "--state", "open", "--limit", "50", "--json", "number,title,body,url",
        )
        new = 0
        for issue in sorted(json.loads(out), key=lambda i: i["number"]):
            ref = str(issue["number"])
            if self.factory.store.seen(self.source, ref):
                continue
            item = await self.factory.submit(
                _spec(issue["title"], issue.get("body", ""), f"{self.cfg.repo}#{ref}"),
                source=self.source, source_ref=ref, source_url=issue["url"],
            )
            self.factory.store.mark_seen(self.source, ref, item["id"])
            new += 1
        return new

    async def notify(self, item: dict, event: str, detail: str) -> None:
        if item.get("source") != self.source or not item.get("source_ref"):
            return
        if event == "pr_opened":
            body = f"The Software Factory opened a pull request for this issue: {detail}"
        elif event == "scrapped":
            body = f"The Software Factory could not finish this issue: crate #{item['id']} failed at {detail}."
        else:
            return
        await asyncio.to_thread(
            self._gh, "issue", "comment", item["source_ref"], "--repo", self.cfg.repo, "--body-file", "-",
            stdin=body,
        )


class JiraIntake:
    source = "jira"

    def __init__(self, cfg: JiraIntakeConfig, factory: Factory):
        self.cfg = cfg
        self.factory = factory

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        token = os.environ.get(self.cfg.token_env, "")
        if not token:
            raise RuntimeError(f"set the {self.cfg.token_env} environment variable to a Jira API token")
        auth = base64.b64encode(f"{self.cfg.email}:{token}".encode()).decode()
        req = urllib.request.Request(
            self.cfg.base_url + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    async def poll(self) -> int:
        data = await asyncio.to_thread(
            self._request, "POST", "/rest/api/3/search/jql",
            {"jql": self.cfg.jql, "fields": ["summary", "description"], "maxResults": 50},
        )
        new = 0
        for issue in data.get("issues", []):
            key = issue["key"]
            if self.factory.store.seen(self.source, key):
                continue
            fields = issue.get("fields", {})
            item = await self.factory.submit(
                _spec(fields.get("summary", key), _adf_text(fields.get("description")), key),
                source=self.source, source_ref=key, source_url=f"{self.cfg.base_url}/browse/{key}",
            )
            self.factory.store.mark_seen(self.source, key, item["id"])
            new += 1
        return new

    async def notify(self, item: dict, event: str, detail: str) -> None:
        if item.get("source") != self.source or not item.get("source_ref"):
            return
        if event == "pr_opened":
            text = f"The Software Factory opened a pull request for this issue: {detail}"
        elif event == "scrapped":
            text = f"The Software Factory could not finish this issue: crate #{item['id']} failed at {detail}."
        else:
            return
        body = {"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}
        await asyncio.to_thread(self._request, "POST", f"/rest/api/3/issue/{item['source_ref']}/comment", body)


def _adf_text(node) -> str:
    """Flatten Jira's rich text (Atlassian Document Format) into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(n) for n in node)
    if node.get("type") == "text":
        return node.get("text", "")
    inner = _adf_text(node.get("content", []))
    return inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock") else "")


async def run_poller(adapter, seconds: float) -> None:
    """Poll forever, logging failures without stopping."""
    while True:
        try:
            new = await adapter.poll()
            if new:
                print(f"[intake] {adapter.source}: {new} new crate(s)")
        except Exception as exc:
            print(f"[intake] {adapter.source} poll failed: {exc}")
        await asyncio.sleep(max(10.0, seconds))
