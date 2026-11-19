"""A stand-in for Notion's REST API, as a transport for docs.NotionAdapter: pages, databases,
block children (paginated) and comments, all in memory."""
from __future__ import annotations

import re


def text(content: str, **ann) -> dict:
    return {"type": "text", "plain_text": content, "text": {"content": content}, "annotations": ann,
            "href": ann.pop("href", None)}


def block(kind: str, content: str = "", children: list | None = None, **extra) -> dict:
    data = {"rich_text": [text(content)] if content else [], **extra}
    return {"object": "block", "type": kind, kind: data, "has_children": bool(children), "_children": children or []}


class FakeNotion:
    def __init__(self, page_size: int = 2):
        self.pages: dict[str, dict] = {}
        self.databases: dict[str, list[str]] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.comments: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.page_size = page_size  # small, so pagination is exercised
        self._n = 0

    def add_page(self, pid: str, title: str, blocks: list[dict] = (), edited: str = "2026-11-01T00:00:00.000Z",
                 props: dict[str, str] | None = None, database: str = "") -> str:
        properties = {"Name": {"type": "title", "title": [text(title)]}}
        for name, value in (props or {}).items():
            properties[name] = {"type": "status", "status": {"name": value}}
        self.pages[pid] = {"object": "page", "id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}",
                           "last_edited_time": edited, "properties": properties, "archived": False}
        self.blocks[pid] = self._own(list(blocks))
        if database:
            self.databases.setdefault(database, []).append(pid)
        return pid

    def _own(self, blocks: list[dict]) -> list[dict]:
        out = []
        for b in blocks:
            self._n += 1
            b = {**b, "id": f"blk-{self._n}"}
            self.blocks[b["id"]] = self._own(b.pop("_children", []))
            out.append(b)
        return out

    def edit(self, pid: str, edited: str, blocks: list[dict] | None = None) -> None:
        self.pages[pid]["last_edited_time"] = edited
        if blocks is not None:
            self.blocks[pid] = self._own(blocks)

    def _paged(self, items: list, cursor: str | None) -> dict:
        start = int(cursor or 0)
        chunk = items[start:start + self.page_size]
        more = start + self.page_size < len(items)
        return {"object": "list", "results": chunk, "has_more": more,
                "next_cursor": str(start + self.page_size) if more else None}

    def __call__(self, method: str, path: str, body: dict | None) -> dict:
        self.calls.append((method, path))
        if path == "/v1/users/me":
            return {"object": "user", "id": "bot-1", "name": "Factory docs"}
        m = re.match(r"^/v1/pages/([\w-]+)$", path)
        if m:
            if m.group(1) not in self.pages:
                raise RuntimeError("Notion said 404: Could not find page.")
            return self.pages[m.group(1)]
        m = re.match(r"^/v1/databases/([\w-]+)/query$", path)
        if m:
            if m.group(1) not in self.databases:
                raise RuntimeError("Notion said 404: Could not find database.")
            return self._paged([self.pages[p] for p in self.databases[m.group(1)]], (body or {}).get("start_cursor"))
        m = re.match(r"^/v1/blocks/([\w-]+)/children\?page_size=\d+(?:&start_cursor=(\w+))?$", path)
        if m:
            return self._paged(self.blocks.get(m.group(1), []), m.group(2))
        if path == "/v1/comments" and method == "POST":
            self.comments.append((body["parent"]["page_id"], "".join(t["text"]["content"] for t in body["rich_text"])))
            return {"object": "comment"}
        raise RuntimeError(f"fake Notion: no route for {method} {path}")
