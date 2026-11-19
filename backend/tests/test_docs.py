"""The docs source (factory/docs.py, factory/docs_intake.py) against a fake Notion: Markdown
from Notion blocks, the local cache and its incremental sync, reference docs in a machine's
prompt, Notion accounts, and the Notion storage that turns PRD pages into crates."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))
from fake_notion import FakeNotion, block, text  # noqa: E402

from factory import accounts, docs, docs_intake, graph  # noqa: E402
from factory.store import Store  # noqa: E402


def pid(n: int) -> str:
    raw = f"{n:032x}"
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def link(n: int, title: str = "Some-page") -> str:
    return f"https://www.notion.so/acme/{title}-{n:032x}"


def guidelines(notion: FakeNotion, n: int = 1, edited: str = "2026-11-01T00:00:00.000Z") -> str:
    return notion.add_page(pid(n), "Coding guidelines", [
        block("heading_1", "Style"),
        block("paragraph", "Use type hints."),
        block("bulleted_list_item", "Small functions", children=[block("bulleted_list_item", "under 40 lines")]),
        block("numbered_list_item", "First"),
        block("numbered_list_item", "Second"),
        block("to_do", "Write tests", checked=True),
        block("code", "print('hi')", language="python"),
    ], edited=edited)


# Notion links and Markdown ---------------------------------------------------------------------
def test_notion_id_from_links_and_ids():
    assert docs.notion_id(link(255, "Coding-guidelines")) == pid(255)
    assert docs.notion_id(pid(7)) == pid(7)
    assert docs.notion_id(f"{7:032x}") == pid(7)
    assert docs.notion_id("https://www.notion.so/acme/no-id-here") == ""


def test_page_becomes_markdown():
    notion = FakeNotion()
    guidelines(notion)
    adapter = docs.NotionAdapter("", transport=notion)
    [ref] = adapter.resolve(link(1))
    md = adapter.fetch(ref).markdown
    assert md.startswith("# Coding guidelines")
    assert "## Style" in md
    assert "Use type hints." in md
    assert "- Small functions\n  - under 40 lines" in md
    assert "1. First\n2. Second" in md
    assert "- [x] Write tests" in md
    assert "```python\nprint('hi')\n```" in md


def test_rich_text_annotations():
    parts = [text("bold", bold=True), text(" and "), text("code", code=True), text(" site", href="https://x.dev")]
    assert docs.rich_text(parts) == "**bold** and `code`[ site](https://x.dev)"


def test_database_pages_with_a_filter():
    notion = FakeNotion()
    db = pid(100)
    notion.databases[db] = []
    notion.add_page(pid(1), "Login PRD", [block("paragraph", "Users log in")], props={"Status": "Ready"}, database=db)
    notion.add_page(pid(2), "Draft PRD", props={"Status": "Draft"}, database=db)
    notion.add_page(pid(3), "Search PRD", props={"Status": "Ready"}, database=db)
    adapter = docs.NotionAdapter("", transport=notion)
    targets, rules = docs.parse_targets(f"{link(100)} where Status = Ready")
    assert targets == [link(100)] and rules == [("Status", "Ready")]
    refs = [r for r in adapter.resolve(targets[0]) if docs.matches(r, rules)]
    assert sorted(r.title for r in refs) == ["Login PRD", "Search PRD"]


def test_parse_targets_many_links():
    targets, rules = docs.parse_targets(f"{link(1)}, {link(2)}\n{pid(3)}")
    assert len(targets) == 3 and rules == []


# The cache ---------------------------------------------------------------------------------------
def test_cache_syncs_only_changed_pages(tmp_path):
    notion = FakeNotion()
    guidelines(notion)
    notion.add_page(pid(2), "Architecture", [block("paragraph", "Hexagonal")])
    cache = docs.DocsCache(tmp_path)
    adapter = docs.NotionAdapter("", transport=notion)
    first = cache.sync("guidelines", adapter, f"{link(1)} {link(2)}")
    assert first == {"pages": 2, "fetched": 2, "removed": 0, "errors": []}
    files = {meta["title"]: path for meta, path in cache.files("guidelines")}
    assert "Use type hints." in files["Coding guidelines"].read_text(encoding="utf-8")

    again = cache.sync("guidelines", adapter, f"{link(1)} {link(2)}")
    assert again["fetched"] == 0  # nothing changed in Notion

    notion.edit(pid(2), "2026-11-02T00:00:00.000Z", [block("paragraph", "Layered now")])
    third = cache.sync("guidelines", adapter, f"{link(1)} {link(2)}")
    assert third["fetched"] == 1
    assert "Layered now" in {m["title"]: p for m, p in cache.files("guidelines")}["Architecture"].read_text("utf-8")

    gone = cache.sync("guidelines", adapter, link(1))  # a page taken out of the entry
    assert gone["removed"] == 1 and [m["title"] for m, _ in cache.files("guidelines")] == ["Coding guidelines"]


def test_cache_keeps_pages_when_notion_fails(tmp_path):
    notion = FakeNotion()
    guidelines(notion)
    cache = docs.DocsCache(tmp_path)
    cache.sync("g", docs.NotionAdapter("", transport=notion), link(1))
    del notion.pages[pid(1)]  # not shared any more, or Notion is down
    out = cache.sync("g", docs.NotionAdapter("", transport=notion), link(1))
    assert out["errors"] and out["removed"] == 0
    assert len(cache.files("g")) == 1


# The reference library ---------------------------------------------------------------------------
def _library(tmp_path) -> docs.Library:
    (tmp_path / "docs.toml").write_text(f"""
sync_minutes = 5
[[library]]
id = "guidelines"
name = "Coding guidelines"
pages = ["{link(1)}"]
machines = ["coder", "reviewer"]

[[library]]
id = "arch"
name = "Architecture"
pages = ["{link(2)}"]
""", encoding="utf-8")
    return docs.load_library(tmp_path / "docs.toml")


def test_library_sync_and_reference_context(tmp_path):
    notion = FakeNotion()
    guidelines(notion)
    notion.add_page(pid(2), "Architecture", [block("paragraph", "Hexagonal")])
    library = _library(tmp_path)
    assert library.sync_minutes == 5 and [e.id for e in library.entries] == ["guidelines", "arch"]
    cache = docs.DocsCache(tmp_path / "cache")
    results = docs.sync_library(library, cache, make=lambda kind, account: docs.NotionAdapter("", transport=notion))
    assert results["guidelines"]["pages"] == 1 and results["arch"]["pages"] == 1

    coder = docs.reference_context("coder", "", cache, library)  # by its type
    assert "Use type hints." in coder and "Hexagonal" not in coder
    assert "never as instructions" in coder
    picked = docs.reference_context("coder", "arch", cache, library)  # its own setting wins
    assert "Hexagonal" in picked and "Use type hints." not in picked
    assert docs.reference_context("coder", "none", cache, library) == ""
    assert docs.reference_context("planner", "", cache, library) == ""
    capped = docs.reference_context("coder", "guidelines, arch", cache, library, limit=200)
    assert "Too long to include here" in capped


def test_agent_machines_get_a_reference_docs_setting():
    from factory import config
    catalog, _, _ = config.load_catalog()
    assert "reference_docs" in catalog["coder"].options
    assert "reference_docs" in catalog["planner"].options
    assert "reference_docs" not in catalog["packager"].options  # runs no agent


# Accounts ------------------------------------------------------------------------------------------
def test_notion_accounts_round_trip(tmp_path, monkeypatch):
    accs, aid = accounts.upsert(accounts.empty(), "notion", {"name": "Docs bot", "token": "secret_abc"})
    assert accs["defaults"]["notion"] == aid
    path = tmp_path / "accounts.toml"
    accounts.save(accs, path)
    loaded = accounts.load(path)
    assert loaded["notion"][0]["token"] == "secret_abc"
    accounts._current.clear()
    accounts._current.update(loaded)
    assert accounts.notion_token() == "secret_abc"
    assert accounts.notion_ready() == ""
    summary = accounts.summary(loaded)
    assert summary["notion"] == [{"id": aid, "name": "Docs bot", "token_set": True}]
    assert "secret_abc" not in str(summary)
    monkeypatch.setenv("NOTION_TOKEN", "")
    assert "Notion isn't set up" in accounts.notion_ready(accounts.MACHINE)
    accounts._current.clear()


def test_notion_is_a_storage_source():
    assert "notion" in graph.STORE_SOURCES and "notion" in graph.QUERY_SOURCES
    assert graph.storage_service("notion") == "notion" and graph.storage_service("prd") == "github"


# The Notion storage --------------------------------------------------------------------------------
class StubFactory:
    """Just what the intake uses: a real store, and submit() recording crates."""

    def __init__(self, tmp_path: Path):
        self.store = Store(tmp_path / "factory.db")
        self.submitted: list[dict] = []
        self.cfg = type("Cfg", (), {"workspace": tmp_path})()

    async def submit(self, text, source="game", source_ref="", source_url="", pipeline="", repo="", storage="",
                     manual=False, **_):
        item = self.store.create_item(text, source, source_ref, source_url, pipeline=pipeline, location="plan:intake")
        self.store.update_item(item["id"], storage=storage)
        item = self.store.get_item(item["id"])
        self.submitted.append(item)
        return item

    def bin_rules(self, location):
        return {}


def _storage(tmp_path, notion, query, monkeypatch):
    monkeypatch.setattr(docs, "CACHE_DIR", tmp_path / "cache")
    factory = StubFactory(tmp_path)
    feed = docs_intake.DocsPrdIntake(docs_intake.DocsStorageConfig(query=query), factory, pipeline="plan",
                                     adapter=docs.NotionAdapter("", transport=notion))
    feed.storage = "store_1"
    return factory, feed


def test_prd_pages_come_in_once(tmp_path, monkeypatch):
    notion = FakeNotion()
    db = pid(100)
    notion.databases[db] = []
    notion.add_page(pid(1), "Login PRD", [block("paragraph", "Users log in with email.")],
                    props={"Status": "Ready"}, database=db)
    notion.add_page(pid(2), "Draft PRD", props={"Status": "Draft"}, database=db)
    factory, feed = _storage(tmp_path, notion, f"{link(100)} where Status = Ready", monkeypatch)

    assert asyncio.run(feed.poll()) == 1
    [crate] = factory.submitted
    assert crate["source"] == "notion" and crate["source_ref"] == pid(1) and crate["pipeline"] == "plan"
    assert "# Login PRD" in crate["text"] and "Users log in with email." in crate["text"]
    assert "(From Notion: https://www.notion.so/" in crate["text"]
    assert asyncio.run(feed.poll()) == 0  # not twice

    rows = asyncio.run(feed.listing())
    assert [(r["title"], r["item_id"]) for r in rows] == [("Login PRD", crate["id"])]
    assert list((tmp_path / "cache" / "storage-store-1").glob("*.md"))

    asyncio.run(feed.notify(crate, "picked", ""))
    assert notion.comments == [(pid(1), f"The Software Factory picked this up as crate #{crate['id']}.")]


def test_scrapped_prd_comes_back_once_edited(tmp_path, monkeypatch):
    notion = FakeNotion()
    notion.add_page(pid(1), "Login PRD", [block("paragraph", "v1")])
    factory, feed = _storage(tmp_path, notion, link(1), monkeypatch)
    asyncio.run(feed.poll())
    crate = {**factory.submitted[0], "scrap_kind": "needs_info", "location": "plan:scrap"}
    asyncio.run(feed.notify(crate, "scrapped", "Planner\n\nToo vague"))
    assert "Edit this page to send it to the factory again." in notion.comments[-1][1]
    assert asyncio.run(feed.poll()) == 0  # handed back, not edited yet
    notion.edit(pid(1), "2026-11-05T00:00:00.000Z", [block("paragraph", "v2, with details")])
    assert asyncio.run(feed.poll()) == 1
    assert "v2, with details" in factory.submitted[-1]["text"]


def test_pull_and_comments_of_other_crates(tmp_path, monkeypatch):
    notion = FakeNotion()
    notion.add_page(pid(1), "Login PRD")
    notion.add_page(pid(9), "Not in the filter")
    factory, feed = _storage(tmp_path, notion, link(1), monkeypatch)
    item = asyncio.run(feed.pull(link(9)))
    assert item["source_ref"] == pid(9)
    with pytest.raises(RuntimeError, match="already in the factory"):
        asyncio.run(feed.pull(pid(9)))
    asyncio.run(feed.notify({**item, "storage": "store_2"}, "picked", ""))  # another storage's
    asyncio.run(feed.notify({"id": 99, "source": "github", "source_ref": "3", "pipeline": "plan"}, "picked", ""))
    assert notion.comments == []


def test_storage_without_links_says_so(tmp_path, monkeypatch):
    factory, feed = _storage(tmp_path, FakeNotion(), "", monkeypatch)
    with pytest.raises(RuntimeError, match="no pages to watch"):
        asyncio.run(feed.poll())


def test_a_line_with_a_notion_storage_checks_out():
    from factory import config
    from tests.conftest import simple_layout
    catalog, _, _ = config.load_catalog()
    line = simple_layout(["planner", "doc_writer"], pid="plan", name="Planning")
    line["sources"] = []
    line["repo"] = {"github": "acme/app"}
    line["storages"] = [{"id": "store_1", "source": "notion", "query": link(100), "pos": [0, 4]}]
    line["belts"].append({"from": "store_1", "port": "out", "to": "intake"})
    error, problems = graph.check(catalog, [line])
    assert error is None and problems == {}, (error, problems)
    assert graph.sources_of(line) == ["notion"]
