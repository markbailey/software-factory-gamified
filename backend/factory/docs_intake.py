"""A docs storage: pages in a documentation system (Notion) that come in as crates, e.g. PRDs
for the Planning line. The storage's filter is the pages to watch: page links (each is one PRD)
or database links (each of its pages is one), optionally with `where Status = Ready`.

Each page comes in once. When its crate is scrapped it's handed back: edit the page and it
comes in again (with why the last try was scrapped, like a retried issue). Milestones are left
as comments on the page when the integration may comment (best effort). Each page is also
written to the docs cache (docs_cache/storage-<id>/), so you can see what the Planner read.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from . import docs, intake
from .engine import Factory


@dataclass
class DocsStorageConfig:
    query: str  # the storage's filter: links, then optionally "where Prop = Value"
    kind: str = "notion"
    repo: str = ""  # (feeds compare this; a docs storage reads no repo)
    poll_seconds: float = 300.0


SOURCE_KIND = {"notion": "notion"}  # storage source -> docs system


class DocsPrdIntake:
    source = "notion"
    storage = ""
    account = ""  # the Notion account ("" the default, "@machine" NOTION_TOKEN), set by main
    work_repo = ""  # the storage's repo, where its crates' plans are made

    def __init__(self, cfg: DocsStorageConfig, factory: Factory, pipeline: str = "", adapter=None):
        self.cfg = cfg
        self.query = cfg.query
        self.factory = factory
        self.pipeline = pipeline
        self._adapter = adapter  # tests hand in a fake; otherwise one per call, as the storage's account
        self.check_only = False

    def adapter(self):
        return self._adapter or docs.adapter_for(self.cfg.kind, self.account)

    def _key(self, page_id: str) -> str:
        return f"{self.pipeline}:{self.storage}:{page_id}"

    def mine(self, item: dict) -> bool:
        return (item.get("source") == self.source and bool(item.get("source_ref"))
                and item.get("pipeline") == self.pipeline and (item.get("storage") or "") == self.storage)

    def _refs(self) -> list[docs.DocRef]:
        targets, rules = docs.parse_targets(self.cfg.query)
        if not targets:
            raise RuntimeError("no pages to watch: click the storage and paste a Notion page or database link")
        adapter = self.adapter()
        seen: dict[str, docs.DocRef] = {}
        for target in targets:
            for ref in adapter.resolve(target):
                if docs.matches(ref, rules):
                    seen.setdefault(ref.id, ref)
        return list(seen.values())

    async def poll(self) -> int:
        refs = await asyncio.to_thread(self._refs)
        new = 0
        for ref in refs:
            key = self._key(ref.id)
            if self.factory.store.seen(self.source, key) or self.factory.store.seen(self.source, f"rev:{key}@{ref.edited}"):
                continue  # in the factory, or handed back and not edited since
            item = await self._submit(ref)
            if item is None:
                break  # no room on the line now: it comes in later
            new += 1
        return new

    async def _submit(self, ref: docs.DocRef, manual: bool = False) -> dict | None:
        doc = await asyncio.to_thread(self.adapter().fetch, ref)
        self._cache(doc)
        key = self._key(ref.id)
        spec = intake._with_last_try(self.factory, self.source, key,
                                     f"{doc.markdown.strip()}\n\n(From Notion: {ref.url or ref.id})"[:intake.MAX_TEXT])
        item = await self.factory.submit(spec, source=self.source, source_ref=ref.id, source_url=ref.url,
                                         pipeline=self.pipeline, repo=self.work_repo, storage=self.storage, manual=manual)
        if item is not None:
            self.factory.store.mark_seen(self.source, key, item["id"])
            self.factory.store.mark_seen(self.source, f"rev:{key}@{ref.edited}", item["id"])
        return item

    def _cache(self, doc: docs.Doc) -> None:
        try:
            folder = docs.DocsCache().folder(f"storage-{self.storage or self.pipeline}")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{doc.ref.id.replace('-', '')}.md").write_text(doc.markdown + "\n", encoding="utf-8")
        except OSError:
            pass  # only a copy for you to look at

    async def notify(self, item: dict, event: str, detail: str) -> None:
        if not self.mine(item):
            return
        key = self._key(item["source_ref"])
        text = ""
        if event == "picked":
            text = f"The Software Factory picked this up as crate #{item['id']}."
        elif event == "scrapped":
            retry = intake._try_again(self.factory, self.source, key, item)
            if retry.get("again"):
                self.factory.store.add_retry(self.source, key, detail)
                self.factory.store.forget_seen(self.source, key)
                # Not edited, but sent back to try again: let the next poll take it.
                self._forget_revisions(key)
                text = intake._message(item, event, detail, retry["hint"])
            else:
                self.factory.store.forget_seen(self.source, key)  # comes back once you edit the page
                text = intake._message(item, event, detail, retry.get("hint", "") +
                                       "Edit this page to send it to the factory again.")
        elif event == "shipped":
            self.factory.store.clear_retries(self.source, key)
            text = "The Software Factory finished with this page." + (
                f" {link}" if (link := self._plan_link(item)) else "")
        if text:
            await self._comment(item["source_ref"], text)

    def _plan_link(self, item: dict) -> str:
        """The plan the Issue maker made from this PRD (its tracking issue, or its one issue), or
        the crate's PR."""
        try:
            record = json.loads((self.factory.cfg.workspace / "plans" / f"crate-{item['id']}.json")
                                .read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            record = {}
        if record.get("tracking_url"):
            return f"Plan: {record['tracking_url']}"
        urls = [t.get("url") for t in (record.get("tasks") or {}).values() if isinstance(t, dict) and t.get("url")]
        if urls:
            return "Issues: " + ", ".join(urls)
        return str(item.get("pr_url") or "")

    def _forget_revisions(self, key: str) -> None:
        db = self.factory.store.db
        db.execute("DELETE FROM intake_seen WHERE source = ? AND ref LIKE ?", (self.source, f"rev:{key}@%"))
        db.commit()

    async def _comment(self, page_id: str, text: str) -> None:
        try:
            await asyncio.to_thread(self.adapter().comment, page_id, text)
        except Exception as exc:  # noqa: BLE001 - the integration may not be allowed to comment
            print(f"[intake] notion: could not comment on {page_id}: {exc}")

    def describe(self) -> str:
        targets, rules = docs.parse_targets(self.cfg.query)
        where = (" where " + " and ".join(f"{k} = {v}" for k, v in rules)) if rules else ""
        return (f"Notion pages ({len(targets)} link{'s' if len(targets) != 1 else ''}{where}): each page, or each "
                "page of a database, comes in by itself. Edit a handed-back page to send it again.")

    async def listing(self) -> list[dict]:
        refs = await asyncio.to_thread(self._refs)
        rows = []
        for ref in sorted(refs, key=lambda r: r.title.lower()):
            rows.append({
                "ref": ref.id, "label": "Notion", "title": ref.title, "url": ref.url, "by": "",
                "tags": [v for k, v in ref.props.items() if k.lower() in ("status", "stage", "state") and v],
                "auto": True, "can_pull": True,
                **intake._crate(self.factory, self.factory.store.seen_item(self.source, self._key(ref.id))),
            })
        return rows

    async def pull(self, ref: str) -> dict | None:
        """Bring one page in now, whether or not it matches the filter."""
        page_id = docs.notion_id(ref) or ref
        if self.factory.store.seen(self.source, self._key(page_id)):
            raise RuntimeError("that page is already in the factory")
        refs = await asyncio.to_thread(self.adapter().resolve, page_id)
        if not refs:
            raise RuntimeError("Notion has no page like that (is it shared with your integration?)")
        return await self._submit(refs[0], manual=True)
