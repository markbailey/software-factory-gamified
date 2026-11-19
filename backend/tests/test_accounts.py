"""The factory's own GitHub / Jira accounts (backend/accounts.toml), and Custom storages."""
import asyncio
import os

from factory import accounts, graph
from factory.config import JiraIntakeConfig

from .conftest import item, simple_layout


def run(coro):
    return asyncio.run(coro)


def test_named_accounts_and_choosing_one(tmp_path):
    machine = dict(os.environ)
    path = tmp_path / "accounts.toml"
    a, bot = accounts.upsert(accounts.empty(), "github", {"name": "Factory Bot", "token": "ghp_bot",
                                                           "git_name": "Factory Bot"})
    a, other = accounts.upsert(a, "github", {"name": "Work bot", "token": "ghp_work"})
    a, acme = accounts.upsert(a, "jira", {"name": "Acme", "base_url": "acme.atlassian.net/",
                                         "email": "bot@acme.com", "token": "jt"})
    assert (bot, other, acme) == ("factory-bot", "work-bot", "acme")
    assert a["defaults"] == {"github": "factory-bot", "jira": "acme", "notion": ""}  # the first of each
    accounts.save(a, path)
    loaded = accounts.load(path)
    assert loaded == a
    jira = JiraIntakeConfig(base_url="https://your-site.atlassian.net", email="you@example.com")
    accounts.use(loaded, jira)
    try:
        assert os.environ["GH_TOKEN"] == "ghp_bot" and os.environ["GIT_AUTHOR_NAME"] == "Factory Bot"
        work = accounts.process_env(accounts.github_env("work-bot"))
        assert work["GH_TOKEN"] == "ghp_work" and work.get("GIT_AUTHOR_NAME") == machine.get("GIT_AUTHOR_NAME")
        n = int(work["GIT_CONFIG_COUNT"])
        helpers = [work[f"GIT_CONFIG_VALUE_{i}"] for i in range(n) if work[f"GIT_CONFIG_KEY_{i}"] == accounts.GITHUB_HELPER_KEY]
        assert helpers[0] == "" and helpers[-1].endswith("auth git-credential")
        mine = accounts.process_env(accounts.github_env(accounts.MACHINE))
        assert all(mine.get(k) == machine.get(k) for k in set(mine) | set(machine) if k.startswith(("GH_", "GIT")))
        assert accounts.github_env("removed-one")["GH_TOKEN"] == "ghp_bot"  # unknown: the default
        creds = accounts.jira_creds("")
        assert (creds.base_url, creds.email, creds.token) == ("https://acme.atlassian.net", "bot@acme.com", "jt")
        assert accounts.jira_ready(creds) == "" and accounts.jira_ready(accounts.jira_creds(accounts.MACHINE))
        shown = accounts.summary(loaded)
        assert "ghp_" not in str(shown) and "'jt'" not in str(shown) and shown["github"][0]["token_set"]
        # A save without a token keeps it; removing the default falls back to this machine's.
        kept, _ = accounts.upsert(loaded, "github", {"id": bot, "name": "Renamed"})
        assert kept["github"][0]["token"] == "ghp_bot" and kept["github"][0]["name"] == "Renamed"
        gone = accounts.remove(loaded, "github", bot)
        assert gone["defaults"]["github"] == ""
        accounts.use(accounts.set_default(gone, "github", accounts.MACHINE), jira)
        assert os.environ.get("GH_TOKEN") == machine.get("GH_TOKEN")
        assert os.environ.get("GIT_CONFIG_COUNT") == machine.get("GIT_CONFIG_COUNT")
    finally:
        accounts.use(accounts.empty(), jira)
    assert {k: v for k, v in os.environ.items() if k.startswith(("GH_", "GIT"))} == \
        {k: v for k, v in machine.items() if k.startswith(("GH_", "GIT"))}
    # The first version's file (one table per service) still loads, as the default account.
    path.write_text('[github]\ntoken = "x"\n[jira]\nbase_url = ""\nemail = ""\ntoken = ""\n', encoding="utf-8")
    old = accounts.load(path)
    assert old["github"][0]["id"] == "factory" and old["defaults"]["github"] == "factory" and old["jira"] == []


def custom_layout(pid="build", sid="store_1", row=0):
    p = simple_layout(pid=pid, name=pid.title(), row=row)
    p["sources"] = []
    p["storages"] = [{"id": sid, "source": "custom", "pos": [0, row + 3]}]
    p["belts"].append({"from": sid, "port": "out", "to": "intake"})
    return p


def test_two_lines_can_each_have_a_custom_storage():
    from factory.config import load_catalog
    catalog, _, _ = load_catalog()
    b = custom_layout("b", "store_2", 8)
    for m in b["machines"]:
        m["id"] += "_b"
    for belt in b["belts"]:
        for end in ("from", "to"):
            if belt[end] in ("coder", "tester", "packager"):
                belt[end] += "_b"
    error, problems = graph.check(catalog, [custom_layout("a", "store_1", 0), b])
    assert error is None and not problems, (error, problems)


def test_a_change_typed_into_a_custom_storage_ships(make_factory):
    f = make_factory(layout=[custom_layout()])
    f.start()

    async def go():
        async with f.client() as c:
            await c.send(type="submit_work", text="Add a greeting", pipeline="build", storage="store_1")
            event = await c.wait_for(lambda e: e.get("type") == "item_created")
            n = int(event["item"]["id"])
            await c.until_state(n, "shipped", "held", "scrapped")
            await c.send(type="intake_list", pipeline="build")
            listing = await c.wait_for(lambda e: e.get("type") == "intake_list")
            return n, listing
    n, listing = run(go())
    crate = item(f, n)
    assert crate["state"] == "shipped" and crate["storage"] == "store_1"
    custom = [s for s in listing["sources"] if s["source"] == "custom"]
    assert custom and custom[0]["storage"] == "store_1" and custom[0]["items"][0]["item_id"] == n


def test_accounts_are_saved_but_tokens_never_sent_back(make_factory):
    f = make_factory()
    f.start()

    async def go():
        async with f.client() as c:
            await c.send(type="save_account", service="github", account={"name": "Bot", "token": "test-factory-token"})
            first = await c.wait_for(lambda e: e.get("type") == "accounts", timeout=90)
            await c.send(type="save_account", service="jira",
                         account={"name": "Acme", "base_url": "https://acme.atlassian.net", "email": "bot@acme.com"})
            second = await c.wait_for(lambda e: e.get("type") == "accounts", timeout=90)
            return first, second
    first, second = run(go())
    assert "test-factory-token" not in str(first) + str(second)
    assert first["github"][0]["token_set"] and first["defaults"]["github"] == "bot"
    assert first["check"]["github"]["bot"]["login"] == "factory-bot"
    assert first["check"]["github"]["@machine"]["login"] == "machine-user"
    assert "API token" in second["check"]["jira"]["acme"]["message"]
    assert 'token = "test-factory-token"' in (f.backend / "accounts.toml").read_text(encoding="utf-8")


def test_a_line_with_no_repo_is_paused_until_one_is_picked():
    """There's no default repo: a line that works on its own repo but names none can't run.
    One whose crates all bring their own repo (PRs to review, storages with a repo) needs none."""
    from factory.config import load_catalog
    catalog, _, _ = load_catalog()
    bare = simple_layout()
    bare.pop("repo")
    _, problems = graph.check(catalog, [bare])
    assert "pick a repo" in problems.get("build", "")
    picked = simple_layout()
    picked["repo"] = {"github": "someone/app"}
    assert graph.check(catalog, [picked]) == (None, {})
    own = custom_layout()
    own.pop("repo")
    own["storages"][0]["repo"] = "someone/app"
    assert not graph.missing_repo(own)
    review = simple_layout()
    review.pop("repo")
    review["sources"] = []
    review["storages"] = [{"id": "store_1", "source": "pr_review", "pos": [0, 3]}]
    review["belts"].append({"from": "store_1", "port": "out", "to": "intake"})
    assert not graph.missing_repo(review)


def test_lines_saved_on_the_old_default_repo_name_it(monkeypatch, tmp_path):
    """pipelines.json from before there was no default: a line that named no repo worked on
    factory.toml's test project, so it names it now and keeps working on it."""
    from pathlib import Path
    from types import SimpleNamespace
    from factory import config, engine
    monkeypatch.setattr(graph, "write", lambda pipelines, path=None: None)
    ws = tmp_path / "workspace"
    old = config.RepoConfig(github="me/test-project", path=ws / "test-project", base_branch="main",
                            remote="https://github.com/me/test-project.git", is_default=True)
    cfg = SimpleNamespace(default_repo=old, workspace=ws)
    bare, picked = simple_layout(), simple_layout(pid="b", name="B")
    bare.pop("repo")
    picked["repo"] = {"github": "someone/app"}
    engine._name_old_default(cfg, [bare, picked])
    assert bare["repo"] == {"github": "me/test-project"} and picked["repo"] == {"github": "someone/app"}
    local = dataclasses_replace(old, github="", remote="")
    cfg.default_repo = local
    bare.pop("repo")
    engine._name_old_default(cfg, [bare])
    assert bare["repo"] == {"path": "test-project"}


def dataclasses_replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)


def test_bare_jira_site_name_becomes_atlassian_host():
    for typed in ("smarthoggs", "https://smarthoggs", "smarthoggs/"):
        a, _ = accounts.upsert(accounts.empty(), "jira", {"name": "S", "base_url": typed})
        assert a["jira"][0]["base_url"] == "https://smarthoggs.atlassian.net", typed
    a, _ = accounts.upsert(accounts.empty(), "jira", {"name": "S", "base_url": "jira.acme.com"})
    assert a["jira"][0]["base_url"] == "https://jira.acme.com"
