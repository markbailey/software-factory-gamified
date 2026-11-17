"""Intake adapters: turn issues in GitHub or Jira into crates, and report back on them.

Each tracker has one small adapter with two jobs:
- poll() finds new labelled issues and submits them as crates (each issue only once);
- notify() posts a comment when the crate's PR opens or the crate is scrapped.

ReviewRequestIntake is different: it brings in other people's PRs for you to review, and
never comments on them itself. GitHubPrIntake brings in open PRs (yours by default) for a
Verifier to check they're still needed; scrapped, the factory closes them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from . import graph, plans
from .config import DEFAULT_CONFIG, GitHubIntakeConfig, JiraIntakeConfig
from .engine import Factory

MAX_TEXT = 20000
LIST_LIMIT = 100  # how many issues or PRs the intake window lists per source


def _crate(factory: Factory, item_id: int | None) -> dict:
    """The crate fields an intake listing shows for an issue the factory already took."""
    if item_id is None:
        return {"item_id": None, "item_state": ""}
    try:
        item = factory.store.get_item(item_id)
    except TypeError:  # the crate was cleaned up
        return {"item_id": None, "item_state": ""}
    return {"item_id": item_id, "item_state": item.get("state", "")}


def _spec(title: str, body: str, ref: str) -> str:
    text = f"{title.strip()}\n\n{(body or '').strip()}".strip()
    return f"{text}\n\n(From {ref})"[:MAX_TEXT]


class GitHubIntake:
    """Labelled issues in one pipeline's repo (cfg.repo) become crates on that pipeline. With no
    pipeline, crates go wherever "github" crates go (the first pipeline that takes them)."""
    source = "github"
    storage = ""  # the storage this feed is (its id), set by main.sync_feeds

    def __init__(self, cfg: GitHubIntakeConfig, factory: Factory, pipeline: str = "", default: bool = True,
                 query: str = "", check_only: bool = False):
        self.cfg = cfg
        self.factory = factory
        self.pipeline = pipeline
        # The line's own search (instead of the factory label), and its "only check" tick.
        self.query = query
        self.check_only = check_only
        # Issues already taken are remembered by number; ones from other repos than the
        # default one by owner/name#number, so #3 here and #3 there are different issues. A
        # line that only checks issues remembers its own, so a working line can still take them.
        self.seen_prefix = "" if default else f"{cfg.repo}#"
        if check_only:
            self.seen_prefix = f"check:{pipeline}:{cfg.repo}#"

    def _key(self, ref: str) -> str:
        return self.seen_prefix + ref

    def mine(self, item: dict) -> bool:
        """Is this crate one of this repo's issues?"""
        if item.get("source") != self.source or not item.get("source_ref"):
            return False
        if self.pipeline and item.get("pipeline") and item.get("pipeline") != self.pipeline:
            return False  # another line's crate (maybe the same repo, with its own search)
        url = str(item.get("source_url") or "").lower()
        if url:
            return f"github.com/{self.cfg.repo.lower()}/" in url
        return not self.pipeline or item.get("pipeline") == self.pipeline

    def _gh(self, *args: str, stdin: str | None = None) -> str:
        gh = shutil.which("gh")
        if not gh:
            raise RuntimeError("gh is not installed or not on PATH")
        r = subprocess.run([gh, *args], input=stdin, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        return r.stdout

    async def poll(self) -> int:
        which = ["--search", self.query] if self.query else ["--label", self.cfg.label]
        out = await asyncio.to_thread(
            self._gh, "issue", "list", "--repo", self.cfg.repo, *which,
            "--state", "open", "--limit", "50", "--json", "number,title,body,url",
        )
        new = 0
        for issue in sorted(json.loads(out), key=lambda i: i["number"]):
            ref = str(issue["number"])
            if self.factory.store.seen(self.source, self._key(ref)):
                continue
            item = await self.factory.submit(
                _spec(issue["title"], issue.get("body", ""), f"{self.cfg.repo}#{ref}"),
                source=self.source, source_ref=ref, source_url=issue["url"], pipeline=self.pipeline,
                repo=self.cfg.repo, storage=self.storage,
            )
            if item is None:
                break  # no pipeline takes them now
            self.factory.store.mark_seen(self.source, self._key(ref), item["id"])
            new += 1
        return new

    async def notify(self, item: dict, event: str, detail: str) -> None:
        if not self.mine(item):
            return
        ref = item["source_ref"]
        if self.check_only and event != "scrapped":
            return  # only checked: not ours to assign, label as in progress or comment on
        rollback = event == "scrapped" and item.get("scrap_kind") == "wrong_line"
        if event == "scrapped" and not rollback and self.factory.bin_rules(item.get("location", "")).get("close_issues"):
            await self._close(item, ref, detail)
            return
        body = _message(item, event, detail, self._resubmit_hint())
        if body:
            await asyncio.to_thread(
                self._gh, "issue", "comment", ref, "--repo", self.cfg.repo, "--body-file", "-", stdin=body)
        if event == "picked":
            # A resubmitted issue still carries the label that said why it came back last time.
            on = await asyncio.to_thread(self._labels_on, ref)
            old = [l for l in set(self.cfg.handback_labels.values()) | self.factory.rollback_labels() if l and l in on]
            await self._edit(ref, add_labels=[self.cfg.in_progress_label], remove_labels=old,
                             assign=self.cfg.assign)
        elif event == "scrapped":
            # Hand it back: take it off the factory's list, say why with a label, unassign.
            kind = item.get("scrap_kind") or "work_failed"
            # Rolled back (no machine on this line can do it): back to the to-do list, labelled
            # with the machine it needs so a line that has one can take it.
            label = self.factory.rollback_label(item) if rollback else self.cfg.handback_labels.get(kind, "")
            failed = await self._edit(ref, add_labels=[label],
                                      remove_labels=[self.cfg.label, self.cfg.in_progress_label],
                                      unassign=self.cfg.assign)
            # Only once the factory label is off, or the next check would pick it straight back up.
            # Putting the label back on then sends it in again.
            if ["--remove-label", self.cfg.label] not in failed:
                self.factory.store.forget_seen(self.source, self._key(ref))
        elif event == "shipped":
            await self._edit(ref, remove_labels=[self.cfg.in_progress_label])
            await self._release_planned(int(ref) if ref.isdigit() else 0)

    async def _close(self, item: dict, ref: str, detail: str) -> None:
        """Scrapped into a bin that closes issues: say why, then close it as not planned."""
        body = _message(item, "scrapped", detail, "Reopen it to send it to the factory again.", closing=True)
        await asyncio.to_thread(
            self._gh, "issue", "comment", ref, "--repo", self.cfg.repo, "--body-file", "-", stdin=body)
        await self._edit(ref, remove_labels=[self.cfg.in_progress_label],
                         unassign=self.cfg.assign and not self.check_only)
        try:
            await asyncio.to_thread(self._gh, "issue", "close", ref, "--repo", self.cfg.repo, "--reason", "not planned")
        except RuntimeError as exc:
            print(f"[intake] github: could not close #{ref}: {exc}")
            return
        self.factory.store.forget_seen(self.source, self._key(ref))  # reopened and labelled, it comes back

    async def _release_planned(self, number: int) -> None:
        """An issue from a plan shipped: label the plan's issues that were waiting for it."""
        folder = self.factory.cfg.workspace / "plans"
        if not number or not folder.exists():
            return
        for n in plans.shipped(folder, self.cfg.repo, number):
            failed = await self._edit(str(n), add_labels=[self.cfg.label])
            if not failed:
                plans.labelled(folder, self.cfg.repo, n)
                print(f"[intake] github: #{n} was waiting for #{number}; labelled it '{self.cfg.label}'")

    def _resubmit_hint(self) -> str:
        return f"Put the `{self.cfg.label}` label back on this issue to send it to the factory again."

    async def _edit(self, ref: str, add_labels: list[str] = (), remove_labels: list[str] = (),
                    assign: bool = False, unassign: bool = False) -> list[list[str]]:
        """Change labels and the assignee, one call each, so one missing label can't stop the rest.
        Returns the changes that failed."""
        calls = []
        for label in [l for l in add_labels if l]:
            await self._ensure_label(label)
            calls.append(["--add-label", label])
        calls += [["--remove-label", l] for l in remove_labels if l]
        if assign:
            calls.append(["--add-assignee", "@me"])
        if unassign:
            calls.append(["--remove-assignee", "@me"])
        failed = []
        for args in calls:
            try:
                await asyncio.to_thread(self._gh, "issue", "edit", ref, "--repo", self.cfg.repo, *args)
            except RuntimeError as exc:
                failed.append(args)
                print(f"[intake] github: could not {' '.join(args)} on #{ref}: {exc}")
        return failed

    def _labels_on(self, ref: str) -> set[str]:
        try:
            out = self._gh("issue", "view", ref, "--repo", self.cfg.repo, "--json", "labels")
            return {l["name"] for l in json.loads(out).get("labels", [])}
        except (RuntimeError, ValueError, KeyError):
            return set()

    async def _ensure_label(self, label: str) -> None:
        made = getattr(self, "_labels_made", set())
        if label in made:
            return
        try:
            await asyncio.to_thread(self._gh, "label", "create", label, "--repo", self.cfg.repo,
                                    "--color", LABEL_COLORS.get(label, "C5DEF5"), "--force")
        except RuntimeError as exc:
            print(f"[intake] github: could not create the label '{label}': {exc}")
        made.add(label)
        self._labels_made = made


    def describe(self) -> str:
        if self.query:
            return f"Open issues in {self.cfg.repo} matching: {self.query}. They all come in by themselves."
        return f"Open issues in {self.cfg.repo}. Ones labelled '{self.cfg.label}' come in by themselves."

    async def listing(self) -> list[dict]:
        out = await asyncio.to_thread(
            self._gh, "issue", "list", "--repo", self.cfg.repo, "--state", "open",
            "--limit", str(LIST_LIMIT), "--json", "number,title,url,labels,author",
        )
        rows = []
        for issue in sorted(json.loads(out or "[]"), key=lambda i: i["number"]):
            ref = str(issue["number"])
            labels = [str(l.get("name", "")) for l in issue.get("labels", []) or []]
            rows.append({
                "ref": ref, "label": f"#{ref}", "title": issue.get("title", ""), "url": issue.get("url", ""),
                "by": (issue.get("author") or {}).get("login", ""), "tags": labels,
                "auto": self.cfg.label in labels, "can_pull": True,
                **_crate(self.factory, self.factory.store.seen_item(self.source, self._key(ref))),
            })
        return rows

    async def pull(self, ref: str) -> dict | None:
        """Bring one open issue in now, labelled or not. Returns the new item."""
        if self.factory.store.seen_item(self.source, self._key(ref)) is not None:
            raise RuntimeError(f"issue #{ref} is already in the factory")
        issue = json.loads(await asyncio.to_thread(
            self._gh, "issue", "view", ref, "--repo", self.cfg.repo, "--json", "number,title,body,url,state"))
        if str(issue.get("state", "")).upper() != "OPEN":
            raise RuntimeError(f"issue #{ref} is closed")
        item = await self.factory.submit(
            _spec(issue["title"], issue.get("body", ""), f"{self.cfg.repo}#{ref}"),
            source=self.source, source_ref=ref, source_url=issue["url"], pipeline=self.pipeline,
            repo=self.cfg.repo, storage=self.storage, manual=True,
        )
        if item is not None:
            self.factory.store.mark_seen(self.source, self._key(ref), item["id"])
        return item


class PrdIntake(GitHubIntake):
    """Product requirements docs: GitHub issues labelled `prd` (by default) go to the Planning
    line. Picked up, handed back and labelled like other GitHub issues; the Issue maker
    comments with the plan when it's made."""
    source = "prd"

    def __init__(self, cfg: GitHubIntakeConfig, factory: Factory, pipeline: str = "", default: bool = True):
        super().__init__(cfg, factory, pipeline, default)

    def describe(self) -> str:
        return (f"Product requirements docs: open issues in {self.cfg.repo} labelled '{self.cfg.label}' come in "
                "by themselves. Or paste one below.")

    async def listing(self) -> list[dict]:
        out = await asyncio.to_thread(
            self._gh, "issue", "list", "--repo", self.cfg.repo, "--label", self.cfg.label, "--state", "open",
            "--limit", str(LIST_LIMIT), "--json", "number,title,url,labels,author",
        )
        rows = []
        for issue in sorted(json.loads(out or "[]"), key=lambda i: i["number"]):
            ref = str(issue["number"])
            rows.append({
                "ref": ref, "label": f"#{ref}", "title": issue.get("title", ""), "url": issue.get("url", ""),
                "by": (issue.get("author") or {}).get("login", ""),
                "tags": [str(l.get("name", "")) for l in issue.get("labels", []) or []],
                "auto": True, "can_pull": True,
                **_crate(self.factory, self.factory.store.seen_item(self.source, self._key(ref))),
            })
        return rows

    async def _release_planned(self, number: int) -> None:
        return  # a PRD isn't a task in a plan


class JiraIntake:
    source = "jira"

    def __init__(self, cfg: JiraIntakeConfig, factory: Factory, pipeline: str = "", check_only: bool = False):
        """pipeline: the line whose own JQL this is (cfg.jql is then that), or "" for the
        factory-wide JQL from factory.toml, whose crates go to the line that takes Jira issues."""
        self.cfg = cfg
        self.factory = factory
        self.pipeline = pipeline
        self.check_only = check_only
        self.seen_prefix = f"check:{pipeline}:" if check_only else ""

    def mine(self, item: dict) -> bool:
        if item.get("source") != self.source or not item.get("source_ref"):
            return False
        if self.pipeline:
            return item.get("pipeline") == self.pipeline
        return item.get("pipeline") not in getattr(self.factory, "jira_lines", set())

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
            if self.factory.store.seen(self.source, self.seen_prefix + key):
                continue
            fields = issue.get("fields", {})
            item = await self.factory.submit(
                _spec(fields.get("summary", key), _adf_text(fields.get("description")), key),
                source=self.source, source_ref=key, source_url=f"{self.cfg.base_url}/browse/{key}",
                pipeline=self.pipeline, storage=getattr(self, "storage", ""), repo=getattr(self, "work_repo", ""),
            )
            if item is None:
                break  # no pipeline takes them now
            self.factory.store.mark_seen(self.source, self.seen_prefix + key, item["id"])
            new += 1
        return new

    async def notify(self, item: dict, event: str, detail: str) -> None:
        if not self.mine(item):
            return
        key = item["source_ref"]
        if self.check_only and event != "scrapped":
            return  # only checked: not ours to assign, move to In Progress or comment on
        rollback = event == "scrapped" and item.get("scrap_kind") == "wrong_line"
        rules = self.factory.bin_rules(item.get("location", "")) if event == "scrapped" else {}
        if rules.get("close_issues") and not rollback:
            text = _message(item, event, detail, f"Moved to {rules['jira_status']} by the Software Factory's scrap bin.",
                            closing=True)
            await self._comment(key, text)
            if self.cfg.assign and not self.check_only:
                await self._try(f"unassign {key}", "PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": None})
            await self._transition(key, rules["jira_status"])
            self.factory.store.forget_seen(self.source, self.seen_prefix + key)
            return
        hint = (f"Add the `{self.cfg.trigger_label}` label again to send it to the factory again."
                if self.cfg.trigger_label else "Press Retry on its crate in the game's scrap bin to send it again.")
        text = _message(item, event, detail, hint)
        if text:
            await self._comment(key, text)
        if event == "picked":
            if self.cfg.assign:
                me = await self._myself()
                if me:
                    await self._try(f"assign {key}", "PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": me})
            await self._transition(key, self.cfg.pickup_transition)
            # A resubmitted issue still carries the label that said why it came back last time.
            old = [{"remove": l.replace(" ", "-")} for l in self.factory.rollback_labels()]
            old += [{"remove": l} for l in set(self.cfg.handback_labels.values()) if l]
            if old:
                await self._try(f"label {key}", "PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": old}})
        elif event == "scrapped":
            kind = item.get("scrap_kind") or "work_failed"
            # Rolled back: Jira labels can't have spaces, so "needs-merger".
            label = (self.factory.rollback_label(item).replace(" ", "-") if rollback
                     else self.cfg.handback_labels.get(kind, ""))
            labels = [{"add": l} for l in [label] if l]
            if self.cfg.trigger_label:
                labels.append({"remove": self.cfg.trigger_label})
            labelled = None
            if labels:
                labelled = await self._try(f"label {key}", "PUT", f"/rest/api/3/issue/{key}",
                                           {"update": {"labels": labels}})
            if self.cfg.assign:
                await self._try(f"unassign {key}", "PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": None})
            await self._transition(key, self.cfg.handback_transition)
            # Only with a trigger label that came off: without one, moving it back to To Do could
            # put it straight back in the query, so it stays a crate you retry from the game.
            if self.cfg.trigger_label and labelled is not None:
                self.factory.store.forget_seen(self.source, self.seen_prefix + key)
        elif event == "shipped":
            await self._transition(key, self.cfg.shipped_transition)

    async def _comment(self, key: str, text: str) -> None:
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        body = {"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]}}
        await self._try(f"comment on {key}", "POST", f"/rest/api/3/issue/{key}/comment", body)

    async def _try(self, what: str, method: str, path: str, payload: dict | None = None) -> dict | None:
        try:
            return await asyncio.to_thread(self._request, method, path, payload)
        except Exception as exc:
            print(f"[intake] jira: could not {what}: {exc}")
            return None

    async def _myself(self) -> str:
        if not getattr(self, "_account_id", ""):
            me = await self._try("look up your account", "GET", "/rest/api/3/myself")
            self._account_id = (me or {}).get("accountId", "")
        return self._account_id

    async def _transition(self, key: str, name: str) -> None:
        """Move the issue through the transition with this name (or to the status with it)."""
        if not name:
            return
        data = await self._try(f"read {key}'s transitions", "GET", f"/rest/api/3/issue/{key}/transitions")
        if data is None:
            return
        want = name.strip().lower()
        for t in data.get("transitions", []):
            if want in (str(t.get("name", "")).lower(), str((t.get("to") or {}).get("name", "")).lower()):
                await self._try(f"move {key} to {name}", "POST", f"/rest/api/3/issue/{key}/transitions",
                                {"transition": {"id": t["id"]}})
                return
        names = ", ".join(str(t.get("name")) for t in data.get("transitions", []))
        print(f"[intake] jira: {key} has no transition called '{name}' right now (it has: {names or 'none'})")


    def describe(self) -> str:
        return f"Jira issues matching: {self.cfg.jql}. They all come in by themselves."

    async def listing(self) -> list[dict]:
        data = await asyncio.to_thread(
            self._request, "POST", "/rest/api/3/search/jql",
            {"jql": self.cfg.jql, "fields": ["summary", "status"], "maxResults": LIST_LIMIT},
        )
        rows = []
        for issue in data.get("issues", []):
            key = issue["key"]
            fields = issue.get("fields", {})
            status = (fields.get("status") or {}).get("name", "")
            rows.append({
                "ref": key, "label": key, "title": fields.get("summary", key),
                "url": f"{self.cfg.base_url}/browse/{key}", "by": "", "tags": [status] if status else [],
                "auto": True, "can_pull": False,
                **_crate(self.factory, self.factory.store.seen_item(self.source, self.seen_prefix + key)),
            })
        return rows


@dataclass
class ReviewIntakeConfig:
    """[intake.review_requests] in factory.toml."""
    enabled: bool = False
    reviewer: str = "@me"  # whose review requests to pick up; @me is whoever gh is signed in as
    query: str = ""  # extra GitHub search terms, e.g. "org:my-company -repo:owner/noisy"
    poll_seconds: float = 120.0

    @classmethod
    def from_toml(cls, path: Path = DEFAULT_CONFIG) -> "ReviewIntakeConfig":
        with open(path, "rb") as f:
            raw = tomllib.load(f).get("intake", {}).get("review_requests", {})
        return cls(enabled=bool(raw.get("enabled", False)), reviewer=str(raw.get("reviewer", "@me")),
                   query=str(raw.get("query", "")), poll_seconds=float(raw.get("poll_seconds", 120)))


class ReviewRequestIntake:
    """Pull requests waiting for your review, in any repo, become crates on the review line.

    Picks up open, non-draft PRs where you are a requested reviewer. Each PR becomes one crate
    per head commit: when new commits arrive, an older crate still waiting for you at a gate is
    scrapped (it reviewed old code) and a fresh crate reviews the new head. GitHub drops you
    from the requested reviewers once you post a review, so a PR comes back when its author
    asks you again. Nothing is ever posted on the PR from here; only the review line's poster
    does that, after you approve.
    """
    source = "pr_review"

    def __init__(self, cfg: ReviewIntakeConfig, factory: Factory):
        self.cfg = cfg
        self.factory = factory
        self.warned = False

    def _gh(self, *args: str) -> str:
        return GitHubIntake._gh(self, *args)  # same gh runner, no shared state

    def _line(self):
        """The line whose intake has a storage of PRs to review belted into it (or, in a layout
        saved before storages, the one that ticks them)."""
        for line in self.factory.lines.values():
            if self.source in line.sources:
                return line
        return self.factory.line_for_source(self.source)

    def _filter(self) -> list[str]:
        """The storage's own search terms, on top of factory.toml's."""
        line = self._line()
        return graph.queries_of(line.graph).get(self.source, "").split() if line is not None else []

    async def poll(self) -> int:
        line = self._line()
        if line is None:
            if not self.warned:
                print("[intake] no pipeline takes pull requests to review (source pr_review); "
                      "add one in build mode to start reviewing")
                self.warned = True
            return 0
        self.warned = False
        args = ["search", "prs", f"--review-requested={self.cfg.reviewer}", "--state=open",
                "--draft=false", "--limit", "50", "--json", "url,number,title,repository,author"]
        args += self.cfg.query.split() + self._filter()
        found = json.loads(await asyncio.to_thread(self._gh, *args) or "[]")
        new = 0
        for hit in found:
            url = hit["url"]
            pr = json.loads(await asyncio.to_thread(
                self._gh, "pr", "view", url, "--json", "headRefOid,isDraft,state,title,body,author"))
            sha = str(pr.get("headRefOid", ""))
            if not sha or pr.get("isDraft") or pr.get("state") != "OPEN":
                continue
            repo = (hit.get("repository") or {}).get("nameWithOwner", "")
            ref = f"{repo}#{hit['number']}@{sha}"
            if self.factory.store.seen(self.source, ref):
                continue
            await self._supersede(url, sha)
            author = (pr.get("author") or {}).get("login", "someone")
            text = (f"Review {repo}#{hit['number']}: {pr.get('title', hit.get('title', ''))}\n\n"
                    f"By @{author}, at commit {sha[:7]}.\n\n{(pr.get('body') or '').strip()}")[:MAX_TEXT]
            item = await self.factory.submit(text, source=self.source, source_ref=ref, source_url=url,
                                             pr_url=url, head_sha=sha, pipeline=line.id)
            if item is None:
                return new  # the review pipeline went away; try again next time
            self.factory.store.mark_seen(self.source, ref, item["id"])
            new += 1
        return new

    def describe(self) -> str:
        who = "you" if self.cfg.reviewer == "@me" else self.cfg.reviewer
        return f"Open pull requests waiting for a review from {who}. They all come in by themselves."

    async def listing(self) -> list[dict]:
        args = ["search", "prs", f"--review-requested={self.cfg.reviewer}", "--state=open",
                "--limit", str(LIST_LIMIT), "--json", "url,number,title,repository,author,isDraft"]
        args += self.cfg.query.split() + self._filter()
        found = json.loads(await asyncio.to_thread(self._gh, *args) or "[]")
        latest: dict[str, int] = {}  # PR url -> its newest crate
        for item in self.factory.store.list_items():
            if item.get("source") == self.source and item.get("source_url"):
                latest[item["source_url"]] = max(latest.get(item["source_url"], 0), int(item["id"]))
        rows = []
        for hit in found:
            repo = (hit.get("repository") or {}).get("nameWithOwner", "")
            url = hit.get("url", "")
            rows.append({
                "ref": url, "label": f"{repo}#{hit.get('number', '')}", "title": hit.get("title", ""),
                "url": url, "by": (hit.get("author") or {}).get("login", ""),
                "tags": ["draft"] if hit.get("isDraft") else [],
                "auto": not hit.get("isDraft"), "can_pull": False,
                **_crate(self.factory, latest.get(url)),
            })
        return rows

    async def _supersede(self, url: str, sha: str) -> None:
        """New commits on a PR: scrap its older crate if that one is waiting for you."""
        for item in self.factory.store.list_items():
            if (item.get("source") == self.source and item.get("source_url") == url
                    and item.get("state") == "awaiting_approval"):
                await self.factory.decide(item["id"], "scrap",
                                          f"New commits were pushed (now at {sha[:7]}); a fresh crate reviews them.")

    async def notify(self, item: dict, event: str, detail: str) -> None:
        return None  # never comment on someone else's PR; the poster posts the review


LABEL_COLORS = {"in progress": "FBCA04", "needs info": "D876E3", "already done": "0E8A16",
                "blocked": "B60205", "factory failed": "E99695"}

HANDBACK_HEADLINES = {
    "needs_info": "The Software Factory needs more information before it can do this issue, so it has "
                  "handed it back. Claude's questions:",
    "already_done": "The Software Factory thinks this is already done, so it has handed it back. Claude's "
                    "explanation:",
    "blocked": "The Software Factory is blocked on this issue, so it has handed it back. Claude's explanation:",
    "work_failed": "The Software Factory could not finish this issue and has handed it back. "
                   "The last failure:",
    "rejected": "This issue's change was rejected in the Software Factory, so it has been handed back.",
    "wrong_line": "This issue needs a machine the Software Factory's line doesn't have, so it has been "
                  "rolled back to the to-do list and labelled for a line that does. Claude's explanation:",
}


CLOSING_HEADLINES = {
    "needs_info": "The Software Factory is closing this issue: it needs more information.",
    "already_done": "The Software Factory is closing this issue: it looks already done or a duplicate.",
    "blocked": "The Software Factory is closing this issue: it's blocked.",
    "work_failed": "The Software Factory is closing this issue: it couldn't finish it.",
    "rejected": "The Software Factory is closing this issue: its change was rejected.",
}


def _message(item: dict, event: str, detail: str, resubmit_hint: str = "", closing: bool = False) -> str:
    """The comment posted on the issue for each milestone, or "" for none. closing: the crate
    went into a scrap bin that closes issues."""
    if event == "pr_opened":
        return f"The Software Factory opened a pull request for this issue: {detail}"
    if event == "scrapped":
        where, _, why = detail.partition("\n\n")
        kind = item.get("scrap_kind") or "work_failed"
        headlines = CLOSING_HEADLINES if closing else HANDBACK_HEADLINES
        text = headlines.get(kind, headlines["work_failed"])
        text += f" (Crate #{item['id']}, stopped at {where}.)"
        if why.strip():
            text += "\n\n" + "\n".join("> " + line for line in why.strip().splitlines())
        if resubmit_hint:
            text += "\n\n" + resubmit_hint
        return text
    if event == "shipped" and item.get("source") == "prd":
        return ""  # the Issue maker already commented with the plan
    if event == "shipped":
        return f"The Software Factory merged the pull request for this issue: {item.get('pr_url', '')}"
    return ""


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


DEFAULT_PR_SEARCH = "author:@me"


class GitHubPrIntake(GitHubIntake):
    """Open pull requests in a line's repo (yours, by default, or the ones its search finds) come
    in as crates that already have their PR, for a Verifier to check they're still needed. Only
    checked: nothing is commented, assigned or labelled on pick-up. A crate scrapped into a bin
    that closes PRs gets its PR closed (the branch is kept); one that passes is left open."""
    source = "github_pr"

    def __init__(self, cfg: GitHubIntakeConfig, factory: Factory, pipeline: str = "", query: str = ""):
        super().__init__(cfg, factory, pipeline, default=False, query=query or DEFAULT_PR_SEARCH, check_only=True)
        self.seen_prefix = f"pr:{pipeline}:{cfg.repo}#"

    async def poll(self) -> int:
        out = await asyncio.to_thread(
            self._gh, "pr", "list", "--repo", self.cfg.repo, "--search", self.query, "--state", "open",
            "--limit", "50", "--json", "number,title,body,url,headRefOid",
        )
        new = 0
        for pr in sorted(json.loads(out or "[]"), key=lambda i: i["number"]):
            ref = str(pr["number"])
            if self.factory.store.seen(self.source, self._key(ref)):
                continue
            item = await self._submit(pr)
            if item is None:
                break
            new += 1
        return new

    async def _submit(self, pr: dict, manual: bool = False) -> dict | None:
        ref = str(pr["number"])
        item = await self.factory.submit(
            _spec(f"PR: {pr['title']}", pr.get("body", ""), f"{self.cfg.repo}#{ref}"),
            source=self.source, source_ref=ref, source_url=pr["url"], pr_url=pr["url"], pipeline=self.pipeline,
            repo=self.cfg.repo, storage=self.storage, manual=manual,
        )
        if item is not None:
            self.factory.store.mark_seen(self.source, self._key(ref), item["id"])
        return item

    async def notify(self, item: dict, event: str, detail: str) -> None:
        return  # the factory closes the PR itself when it's scrapped (if the bin says so)

    def describe(self) -> str:
        return (f"Open pull requests in {self.cfg.repo} matching: {self.query}. They come in by themselves, "
                "each once, with their PR, for the machines on the line (CI, Approver, Merger, Verifier...).")

    async def listing(self) -> list[dict]:
        out = await asyncio.to_thread(
            self._gh, "pr", "list", "--repo", self.cfg.repo, "--search", self.query, "--state", "open",
            "--limit", str(LIST_LIMIT), "--json", "number,title,url,labels,author",
        )
        rows = []
        for pr in sorted(json.loads(out or "[]"), key=lambda i: i["number"]):
            ref = str(pr["number"])
            rows.append({
                "ref": ref, "label": f"#{ref}", "title": pr.get("title", ""), "url": pr.get("url", ""),
                "by": (pr.get("author") or {}).get("login", ""),
                "tags": [str(l.get("name", "")) for l in pr.get("labels", []) or []],
                "auto": True, "can_pull": True,
                **_crate(self.factory, self.factory.store.seen_item(self.source, self._key(ref))),
            })
        return rows

    async def pull(self, ref: str) -> dict | None:
        """Check one open PR now (again, if it was checked before)."""
        seen = self.factory.store.seen_item(self.source, self._key(ref))
        if seen is not None and (self.factory.store.get_item(seen) or {}).get("state") not in ("shipped", "scrapped"):
            raise RuntimeError(f"PR #{ref} is already in the factory")
        pr = json.loads(await asyncio.to_thread(
            self._gh, "pr", "view", ref, "--repo", self.cfg.repo, "--json", "number,title,body,url,state"))
        if str(pr.get("state", "")).upper() != "OPEN":
            raise RuntimeError(f"PR #{ref} is {str(pr.get('state', 'closed')).lower()}")
        self.factory.store.forget_seen(self.source, self._key(ref))
        return await self._submit(pr, manual=True)


class Feed:
    """One intake adapter as the game sees it: polled on a timer or straight away on request,
    one poll or pull at a time (so the same issue can't come in twice)."""

    def __init__(self, adapter, seconds: float):
        self.adapter = adapter
        self.source: str = adapter.source
        self.seconds = max(10.0, seconds)
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.last_poll = 0.0
        self.next_poll = 0.0
        self.error = ""
        self.paused = lambda: False  # main sets it: true while its line or the factory is paused

    async def poll_now(self) -> int:
        async with self.lock:
            try:
                new = await self.adapter.poll()
                self.error = ""
            except Exception as exc:
                self.error = str(exc)
                print(f"[intake] {self.source} poll failed: {exc}")
                new = 0
            self.last_poll = time.time()
        if new:
            print(f"[intake] {self.source}: {new} new crate(s)")
        return new

    async def pull(self, ref: str) -> dict | None:
        if not hasattr(self.adapter, "pull"):
            raise RuntimeError("these come in by themselves; press Check now to bring new ones in")
        async with self.lock:
            return await self.adapter.pull(ref)

    async def listing(self) -> dict:
        out = {"source": self.source, "enabled": True, "about": self.adapter.describe(),
               "poll_seconds": self.seconds, "last_poll": self.last_poll, "next_poll": self.next_poll,
               "poll_error": self.error, "items": [], "error": ""}
        try:
            out["items"] = await self.adapter.listing()
        except Exception as exc:
            out["error"] = str(exc)
        return out

    async def run(self) -> None:
        """Poll forever (and again whenever woken), logging failures without stopping."""
        while True:
            if not self.paused():  # a paused factory or line brings nothing new in
                await self.poll_now()
            self.next_poll = time.time() + self.seconds
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.seconds)
            except asyncio.TimeoutError:
                pass
