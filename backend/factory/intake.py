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
import dataclasses
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

from . import accounts, graph, plans
from .config import DEFAULT_CONFIG, GitHubIntakeConfig, JiraIntakeConfig
from .engine import Factory

MAX_TEXT = 20000
LIST_LIMIT = 100  # how many issues or PRs the intake window lists per source
CLOSED_HIDE_SECONDS = 600.0  # a closed issue or PR stays out of the listing while GitHub's search catches up
DONE_NAMES = ("done", "closed", "resolved", "complete", "completed")  # preferred Jira close transitions


def _hide_closed(adapter, rows: list[dict]) -> list[dict]:
    """Leave out the rows closed from the game a moment ago (searches can lag behind)."""
    now = time.time()
    closed = {r: t for r, t in getattr(adapter, "closed_refs", {}).items() if now - t < CLOSED_HIDE_SECONDS}
    adapter.closed_refs = closed
    return [r for r in rows if str(r.get("ref", "")) not in closed]


def _closed_now(adapter, ref: str) -> None:
    refs = getattr(adapter, "closed_refs", {})
    refs[ref] = time.time()
    adapter.closed_refs = refs


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
        # As the GitHub account its line or storage picked (main sets .account), or the default.
        env = accounts.process_env(accounts.github_env(getattr(self, "account", "")))
        r = subprocess.run([gh, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", env=env)
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
                _with_last_try(self.factory, self.source, self._key(ref),
                               _spec(issue["title"], issue.get("body", ""), f"{self.cfg.repo}#{ref}")),
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
        retry = (_try_again(self.factory, self.source, self._key(ref), item)
                 if event == "scrapped" and not rollback and not self.check_only else {})
        if retry.get("again"):
            # Handed back for another go: still labelled for the factory, so the next check takes it.
            body = _message(item, event, detail, retry["hint"])
            await asyncio.to_thread(
                self._gh, "issue", "comment", ref, "--repo", self.cfg.repo, "--body-file", "-", stdin=body)
            await self._edit(ref, add_labels=[] if self.query else [self.cfg.label],
                             remove_labels=[self.cfg.in_progress_label], unassign=self.cfg.assign)
            self.factory.store.add_retry(self.source, self._key(ref), detail)
            self.factory.store.forget_seen(self.source, self._key(ref))
            return
        body = _message(item, event, detail, retry.get("hint", "") + self._resubmit_hint())
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
            self.factory.store.clear_retries(self.source, self._key(ref))
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
        await self._plan_scrapped(int(ref) if ref.isdigit() else 0)

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
        await self._close_finished_plans(number)

    async def _plan_scrapped(self, number: int) -> None:
        """An issue from a plan was scrapped and closed. The tasks waiting for it won't be
        labelled by themselves, so say so on the plan and on each of them, with the way out."""
        folder = self.factory.cfg.workspace / "plans"
        if not number or not folder.exists():
            return
        for hit in plans.scrapped(folder, self.cfg.repo, number):
            blocked = hit["blocked"]
            if blocked:
                refs = ", ".join(f"#{n}" for n in blocked)
                note = (f"#{number} was scrapped, so {refs} {'waits' if len(blocked) == 1 else 'wait'} for it "
                        f"and won't be sent to the factory by itself. Reopen #{number} and label it "
                        f"`{self.cfg.label}` to try it again (the rest follow once it ships), label a "
                        f"waiting issue `{self.cfg.label}` to build it anyway, or close the ones you "
                        "don't want.\n")
                for ref in [str(hit["tracking"])] + [str(n) for n in blocked]:
                    try:
                        await asyncio.to_thread(self._gh, "issue", "comment", ref, "--repo", self.cfg.repo,
                                                "--body-file", "-", stdin=note)
                    except RuntimeError as exc:
                        print(f"[intake] github: could not comment on #{ref}: {exc}")
        await self._close_finished_plans(number)

    async def _close_finished_plans(self, number: int) -> None:
        """Close the tracking issue of each plan whose tasks are now all shipped or scrapped."""
        folder = self.factory.cfg.workspace / "plans"
        if not number or not folder.exists():
            return
        for done in plans.finished(folder, self.cfg.repo, number):
            ref, shipped, scrapped = str(done["tracking"]), done["shipped"], done["scrapped"]
            total = len(shipped) + len(scrapped)
            body = f"All {total} task{'' if total == 1 else 's'} in this plan are done"
            if scrapped:
                body += (": shipped " + (", ".join(f"#{n}" for n in shipped) or "none")
                         + "; scrapped " + ", ".join(f"#{n}" for n in scrapped))
            body += ". Closing the plan.\n"
            reason = "completed" if shipped else "not planned"
            try:
                await asyncio.to_thread(self._gh, "issue", "comment", ref, "--repo", self.cfg.repo,
                                        "--body-file", "-", stdin=body)
                await asyncio.to_thread(self._gh, "issue", "close", ref, "--repo", self.cfg.repo,
                                        "--reason", reason)
            except RuntimeError as exc:
                print(f"[intake] github: could not close the plan #{ref}: {exc}")
                continue
            plans.closed(folder, self.cfg.repo, done["tracking"])
            print(f"[intake] github: every task in the plan #{ref} is done; closed it")

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


    async def close(self, ref: str, comment: str = "", reason: str = "", transition: str = "") -> None:
        """Close one issue from the game (its storage's Close), as the storage's account."""
        args = ["issue", "close", ref, "--repo", self.cfg.repo]
        if reason in ("completed", "not planned"):
            args += ["--reason", reason]
        if comment.strip():
            args += ["--comment", comment.strip()[:MAX_TEXT]]
        await asyncio.to_thread(self._gh, *args)
        _closed_now(self, ref)

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
        return _hide_closed(self, rows)

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
        return _hide_closed(self, rows)

    async def _release_planned(self, number: int) -> None:
        return  # a PRD isn't a task in a plan

    async def _plan_scrapped(self, number: int) -> None:
        return  # nor is a scrapped one


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

    account = ""  # the Jira account to use ("" the default, "@machine" this machine's), set by main

    def _site(self) -> str:
        return accounts.jira_creds(self.account).base_url

    def mine(self, item: dict) -> bool:
        if item.get("source") != self.source or not item.get("source_ref"):
            return False
        if self.pipeline:
            return item.get("pipeline") == self.pipeline
        return item.get("pipeline") not in getattr(self.factory, "jira_lines", set())

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        creds = accounts.jira_creds(self.account)  # the Jira account its line or storage picked
        missing = accounts.jira_ready(creds)
        if missing:
            raise RuntimeError(missing)
        auth = base64.b64encode(f"{creds.email}:{creds.token}".encode()).decode()
        req = urllib.request.Request(
            creds.base_url + path,
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
            {"jql": self.cfg.jql, "fields": ["summary", "description", "status"], "maxResults": 50},
        )
        new = 0
        for issue in data.get("issues", []):
            key = issue["key"]
            if self.factory.store.seen(self.source, self.seen_prefix + key):
                continue
            fields = issue.get("fields", {})
            if self._closed_here(key, fields):
                continue  # closed from the game: not work to do, unless it's reopened
            item = await self.factory.submit(
                _with_last_try(self.factory, self.source, self.seen_prefix + key,
                               _spec(fields.get("summary", key), _adf_text(fields.get("description")), key)),
                source=self.source, source_ref=key, source_url=f"{self._site()}/browse/{key}",
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
        retry = (_try_again(self.factory, self.source, self.seen_prefix + key, item)
                 if event == "scrapped" and not rollback and not self.check_only else {})
        if retry.get("again"):
            # Handed back for another go: back to To Do, keeping (or getting) the trigger label.
            await self._comment(key, _message(item, event, detail, retry["hint"]))
            if self.cfg.trigger_label:
                await self._try(f"label {key}", "PUT", f"/rest/api/3/issue/{key}",
                                {"update": {"labels": [{"add": self.cfg.trigger_label}]}})
            if self.cfg.assign:
                await self._try(f"unassign {key}", "PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": None})
            await self._transition(key, self.cfg.handback_transition)
            self.factory.store.add_retry(self.source, self.seen_prefix + key, detail)
            self.factory.store.forget_seen(self.source, self.seen_prefix + key)
            return
        hint = (f"Add the `{self.cfg.trigger_label}` label again to send it to the factory again."
                if self.cfg.trigger_label else "Press Retry on its crate in the game's scrap bin to send it again.")
        text = _message(item, event, detail, retry.get("hint", "") + hint)
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
            self.factory.store.clear_retries(self.source, self.seen_prefix + key)
            await self._transition(key, self.cfg.shipped_transition)
            await self._release_planned(key)

    async def _release_planned(self, key: str) -> None:
        """An issue a Document writer made from a plan shipped: label the plan's issues that
        were waiting for it, so the line that looks for that label takes them next."""
        folder = self.factory.cfg.workspace / "plans"
        if not folder.exists() or "-" not in key:
            return
        repo = "jira:" + key.rsplit("-", 1)[0].upper()
        hit = plans.plan_of(folder, repo, key)
        label = str((hit or {}).get("label") or self.cfg.trigger_label).replace(" ", "-")
        for ready in plans.shipped(folder, repo, key):
            if not label:
                continue
            done = await self._try(f"label {ready}", "PUT", f"/rest/api/3/issue/{ready}",
                                   {"update": {"labels": [{"add": label}]}})
            if done is not None:
                plans.labelled(folder, repo, ready)
                print(f"[intake] jira: {ready} was waiting for {key}; labelled it '{label}'")

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


    def _closed_here(self, key: str, fields: dict) -> bool:
        """Closed from the game's storage and still in a done status."""
        done = str(((fields.get("status") or {}).get("statusCategory") or {}).get("key", "")) == "done"
        return done and self.factory.store.seen(self.source, f"closed:{self.seen_prefix}{key}")

    async def close_options(self, key: str) -> dict:
        """The transitions this issue can take now, the ones into a done status first, and the
        one Close uses by default."""
        data = await asyncio.to_thread(self._request, "GET", f"/rest/api/3/issue/{key}/transitions")
        options = []
        for t in data.get("transitions", []):
            to = t.get("to") or {}
            done = str((to.get("statusCategory") or {}).get("key", "")) == "done"
            options.append({"id": str(t.get("id", "")), "name": str(t.get("name", "")),
                            "to": str(to.get("name", "")), "done": done})
        options.sort(key=lambda o: not o["done"])
        return {"transitions": options, "default": _pick_done(options)}

    async def close(self, ref: str, comment: str = "", reason: str = "", transition: str = "") -> None:
        """Move one issue to a done status from the game (with a comment first, if any), as the
        storage's account. transition: its name (or the status it goes to); "" picks the done one."""
        options = (await self.close_options(ref))["transitions"]
        want = transition.strip().lower()
        if want:
            pick = next((o for o in options if want in (o["name"].lower(), o["to"].lower(), o["id"])), None)
            if pick is None:
                names = ", ".join(o["name"] for o in options) or "none"
                raise RuntimeError(f"{ref} can't take '{transition}' now (it can: {names})")
        else:
            pick = next((o for o in options if o["name"] == _pick_done(options)), None)
            if pick is None:
                names = ", ".join(o["name"] for o in options) or "none"
                raise RuntimeError(f"{ref} has no transition to a done status now (it has: {names})")
        if comment.strip():
            paragraphs = [p for p in comment.strip()[:MAX_TEXT].split("\n\n") if p.strip()]
            await asyncio.to_thread(self._request, "POST", f"/rest/api/3/issue/{ref}/comment", {"body": {
                "type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]}})
        await asyncio.to_thread(self._request, "POST", f"/rest/api/3/issue/{ref}/transitions",
                                {"transition": {"id": pick["id"]}})
        _closed_now(self, ref)
        # So a JQL that also matches done issues doesn't bring it straight back in as a crate.
        self.factory.store.mark_seen(self.source, f"closed:{self.seen_prefix}{ref}", None)

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
            if self._closed_here(key, fields):
                continue  # closed from the game; it shows again if it's reopened
            status = (fields.get("status") or {}).get("name", "")
            rows.append({
                "ref": key, "label": key, "title": fields.get("summary", key),
                "url": f"{self._site()}/browse/{key}", "by": "", "tags": [status] if status else [],
                "auto": True, "can_pull": False,
                **_crate(self.factory, self.factory.store.seen_item(self.source, self.seen_prefix + key)),
            })
        return _hide_closed(self, rows)


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

    def _repo_terms(self) -> list[str] | None:
        """With the storage's "Only repos in the factory's repo list" ticked: a repo: term per repo
        added under the gear (None: the list is empty, so nothing comes in). Unticked: no terms."""
        line = self._line()
        if line is None or not graph.listed_repos_only(line.graph, self.source):
            return []
        repos = self.factory._saved_repos()
        return [f"--repo={r}" for r in repos] if repos else None

    def _listed(self, hit: dict) -> bool:
        """Whether a search hit is from a repo the storage takes (always, unless it's ticked)."""
        line = self._line()
        if line is None or not graph.listed_repos_only(line.graph, self.source):
            return True
        repo = ((hit.get("repository") or {}).get("nameWithOwner") or "").lower()
        return repo in {r.lower() for r in self.factory._saved_repos()}

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
        repos = self._repo_terms()
        if repos is None:
            return 0  # only the factory's repos, and none are listed yet
        args += self.cfg.query.split() + self._filter() + repos
        found = json.loads(await asyncio.to_thread(self._gh, *args) or "[]")
        new = 0
        for hit in found:
            if not self._listed(hit):
                continue
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
        where = ""
        if self._repo_terms() is None:
            where = " Only the factory's repos are taken, and none are listed yet: add some under the gear (Repos)."
        elif self._repo_terms():
            where = " Only PRs in the factory's repo list are taken."
        return f"Open pull requests waiting for a review from {who}. They all come in by themselves.{where}"

    async def listing(self) -> list[dict]:
        args = ["search", "prs", f"--review-requested={self.cfg.reviewer}", "--state=open",
                "--limit", str(LIST_LIMIT), "--json", "url,number,title,repository,author,isDraft"]
        repos = self._repo_terms()
        if repos is None:
            return []  # only the factory's repos, and none are listed yet
        args += self.cfg.query.split() + self._filter() + repos
        found = [hit for hit in json.loads(await asyncio.to_thread(self._gh, *args) or "[]") if self._listed(hit)]
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
        return _hide_closed(self, rows)

    async def close(self, ref: str, comment: str = "", reason: str = "", transition: str = "") -> None:
        """Close someone's PR from the game (ref is its URL). Asked for by you, so it's allowed here."""
        args = ["pr", "close", ref]
        if comment.strip():
            args += ["--comment", comment.strip()[:MAX_TEXT]]
        await asyncio.to_thread(self._gh, *args)
        _closed_now(self, ref)

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


def _try_again(factory, source: str, key: str, item: dict) -> dict:
    """Should a scrapped issue go back to the factory for another try? Only from a bin that hands
    issues back with retry_issues on, and at most max_retries times (no endless loop): then
    {"again": True, "hint": ...}. Out of tries: {"hint": ...} saying so, and the count starts
    again, so labelling it yourself gives it fresh tries. Otherwise {}."""
    rules = factory.bin_rules(item.get("location", ""))
    if rules.get("close_issues") or not rules.get("retry_issues"):
        return {}
    done, _ = factory.store.retries(source, key)
    most = int(rules.get("max_retries", 0))
    if done < most:
        return {"again": True, "hint": f"Sent back to the factory to try again (retry {done + 1} of {most})."}
    factory.store.clear_retries(source, key)
    if not most:
        return {}
    return {"hint": f"The factory has tried this {done + 1} times now, so it's handed back to you. "}


def _with_last_try(factory, source: str, key: str, spec: str) -> str:
    """A retried issue's spec, with why its last try was scrapped, so the next try can do better."""
    done, note = factory.store.retries(source, key)
    if not done or not note.strip():
        return spec
    extra = f"\n\nRetry {done}: the last try at this was scrapped. What went wrong:\n{note.strip()}"
    return spec[:MAX_TEXT - len(extra[:6000])] + extra[:6000]


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


def _pick_done(options: list[dict]) -> str:
    """The name of the transition Close uses by default: into a done status, Done/Closed/Resolved first."""
    done = [o for o in options if o.get("done")] or [
        o for o in options if o.get("name", "").lower() in DONE_NAMES or o.get("to", "").lower() in DONE_NAMES]
    for name in DONE_NAMES:
        for o in done:
            if name in (o.get("name", "").lower(), o.get("to", "").lower()):
                return o["name"]
    return done[0]["name"] if done else ""


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


def _pr_key(url: str) -> str:
    """owner/name#number for a GitHub PR link (lower-case), or "" if it isn't one."""
    parts = str(url or "").split("github.com/", 1)[-1].strip("/").split("/")
    if len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit():
        return f"{parts[0]}/{parts[1]}#{parts[3]}".lower()
    return ""


def factory_crate_of(factory, repo: str, pr: dict) -> int | None:
    """The live crate (not shipped or scrapped) that this open PR belongs to: one the factory
    opened itself, found by the crate's saved PR link or its saved branch name (crate/<n> for
    crates from before branch naming). Crates that came in from an Open PRs storage don't count:
    their PR is someone's own, brought in to be checked."""
    from . import branch_names  # here, so intake doesn't import git helpers it rarely needs
    from .store import TERMINAL_STATES
    live: dict[int, dict] = {}
    for it in factory.store.list_items(log_tail=0):
        if it.get("state") not in TERMINAL_STATES and it.get("source") != "github_pr":
            live[int(it["id"])] = it
    if not live:
        return None
    key = _pr_key(pr.get("url", "")) or f"{repo}#{pr.get('number', '')}".lower()
    for cid, it in live.items():
        if it.get("pr_url") and _pr_key(it["pr_url"]) == key:
            return cid
    head = str(pr.get("headRefName") or "")
    if not head:
        return None
    cid = branch_names.crate_of(head, branch_names.owners(live))
    if cid is None or cid not in live:
        return None
    it = live[cid]
    if it.get("branch") and str(it["branch"]) != head:
        return None  # crate/<n> naming, but crate n has another branch: not its
    lines = getattr(factory, "lines", {}) or {}
    line = lines.get(it.get("pipeline")) if isinstance(lines, dict) else None
    crate_repo = str(it.get("repo") or getattr(getattr(line, "repo", None), "github", "") or "")
    if crate_repo and crate_repo.lower() != repo.lower():
        return None  # the same branch name in another repo
    return cid


class GitHubPrIntake(GitHubIntake):
    """Open pull requests in a line's repo (yours, by default, or the ones its search finds) come
    in as crates that already have their PR, for a Verifier to check they're still needed. Only
    checked: nothing is commented, assigned or labelled on pick-up. A crate scrapped into a bin
    that closes PRs gets its PR closed (the branch is kept); one that passes is left open."""
    source = "github_pr"

    def __init__(self, cfg: GitHubIntakeConfig, factory: Factory, pipeline: str = "", query: str = "",
                 include_factory_prs: bool = False):
        super().__init__(cfg, factory, pipeline, default=False, query=query or DEFAULT_PR_SEARCH, check_only=True)
        self.seen_prefix = f"pr:{pipeline}:{cfg.repo}#"
        # Off (the default), PRs the factory opened for crates still on a line are left alone:
        # they come in once their crate has shipped or been scrapped, if they're still open.
        self.include_factory_prs = include_factory_prs

    def _factory_crate(self, pr: dict) -> int | None:
        return None if self.include_factory_prs else factory_crate_of(self.factory, self.cfg.repo, pr)

    async def poll(self) -> int:
        out = await asyncio.to_thread(
            self._gh, "pr", "list", "--repo", self.cfg.repo, "--search", self.query, "--state", "open",
            "--limit", "50", "--json", "number,title,body,url,headRefOid,headRefName",
        )
        new = 0
        for pr in sorted(json.loads(out or "[]"), key=lambda i: i["number"]):
            ref = str(pr["number"])
            if self.factory.store.seen(self.source, self._key(ref)):
                continue
            if self._factory_crate(pr) is not None:
                continue  # the factory's own PR, its crate still on a line (not marked seen)
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

    async def close(self, ref: str, comment: str = "", reason: str = "", transition: str = "") -> None:
        """Close one PR from the game (its branch is kept), as the storage's account."""
        args = ["pr", "close", ref, "--repo", self.cfg.repo]
        if comment.strip():
            args += ["--comment", comment.strip()[:MAX_TEXT]]
        await asyncio.to_thread(self._gh, *args)
        _closed_now(self, ref)

    def describe(self) -> str:
        return (f"Open pull requests in {self.cfg.repo} matching: {self.query}. They come in by themselves, "
                "each once, with their PR, for the machines on the line (CI, Approver, Merger, Verifier...)."
                + _own_prs_note(self.include_factory_prs))

    async def listing(self) -> list[dict]:
        out = await asyncio.to_thread(
            self._gh, "pr", "list", "--repo", self.cfg.repo, "--search", self.query, "--state", "open",
            "--limit", str(LIST_LIMIT), "--json", "number,title,url,labels,author,headRefName",
        )
        rows = []
        for pr in sorted(json.loads(out or "[]"), key=lambda i: i["number"]):
            ref = str(pr["number"])
            own = self._factory_crate(pr)
            tags = [str(l.get("name", "")) for l in pr.get("labels", []) or []]
            if own is not None:
                tags.append(f"factory's own (crate #{own})")
            rows.append({
                "ref": ref, "label": f"#{ref}", "title": pr.get("title", ""), "url": pr.get("url", ""),
                "by": (pr.get("author") or {}).get("login", ""), "tags": tags,
                "auto": own is None, "can_pull": True, **({"factory_crate": own} if own is not None else {}),
                **_crate(self.factory, self.factory.store.seen_item(self.source, self._key(ref))),
            })
        return _hide_closed(self, rows)

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


def _own_prs_note(included: bool) -> str:
    if included:
        return " PRs the factory opened itself come in too, even while their crate is still on a line."
    return (" PRs the factory opened itself are left alone while their crate is still on a line"
            " (tick \"Include the factory's own PRs\" to take them too).")


LISTED_REPOS = "*factory-repos*"  # an Open PRs storage ticked "Only repos in the factory's repo list"


class ListedReposPrIntake:
    """An Open PRs storage with "Only repos in the factory's repo list" ticked: open PRs (matching
    its search) in every repo added under the gear (Repos), instead of its one repo. Each repo is
    read as a plain Open PRs storage on it would read it, so a PR is taken once either way. Rows
    and refs are owner/name#number, since numbers repeat across repos."""
    source = "github_pr"
    storage = ""
    check_only = True

    def __init__(self, cfg: GitHubIntakeConfig, factory: Factory, pipeline: str = "", query: str = "",
                 include_factory_prs: bool = False):
        self.cfg = cfg  # cfg.repo is LISTED_REPOS
        self.factory = factory
        self.pipeline = pipeline
        self.query = query or DEFAULT_PR_SEARCH
        self.include_factory_prs = include_factory_prs

    def _one(self, repo: str) -> GitHubPrIntake:
        one = GitHubPrIntake(dataclasses.replace(self.cfg, repo=repo), self.factory, self.pipeline, self.query,
                             self.include_factory_prs)
        one.storage = self.storage
        one.account = getattr(self, "account", "")
        one._gh = self._gh
        return one

    def _gh(self, *args: str, stdin: str | None = None) -> str:
        return GitHubIntake._gh(self, *args, stdin=stdin)  # as this storage's account

    def _repos(self) -> list[str]:
        return self.factory._saved_repos()

    def _split(self, ref: str) -> tuple[GitHubPrIntake, str]:
        repo, _, number = str(ref).rpartition("#")
        match = next((r for r in self._repos() if r.lower() == repo.lower()), "")
        if not match or not number:
            raise RuntimeError(f"{ref} isn't a PR in one of the factory's repos")
        return self._one(match), number

    async def poll(self) -> int:
        new = 0
        for repo in self._repos():
            new += await self._one(repo).poll()
        return new

    async def notify(self, item: dict, event: str, detail: str) -> None:
        return  # the factory closes the PR itself when it's scrapped (if the bin says so)

    def describe(self) -> str:
        repos = self._repos()
        if not repos:
            return ("Open pull requests in the factory's repo list, and none are listed yet: "
                    "add some under the gear (Repos).")
        return (f"Open pull requests matching: {self.query}, in the factory's repos ({', '.join(repos)}). "
                "They come in by themselves, each once, with their PR, for the machines on the line."
                + _own_prs_note(self.include_factory_prs))

    async def listing(self) -> list[dict]:
        rows = []
        for repo in self._repos():
            for row in await self._one(repo).listing():
                ref = f"{repo}#{row['ref']}"
                rows.append({**row, "ref": ref, "label": ref})
        return _hide_closed(self, rows)

    async def close(self, ref: str, comment: str = "", reason: str = "", transition: str = "") -> None:
        one, number = self._split(ref)
        await one.close(number, comment, reason, transition)
        _closed_now(self, ref)

    async def pull(self, ref: str) -> dict | None:
        one, number = self._split(ref)
        return await one.pull(number)


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

    async def close(self, refs: list[str], comment: str = "", reason: str = "", transition: str = "") -> dict:
        """Close these issues or PRs (or move Jira issues to done), one at a time.
        Returns {"closed": [refs], "failed": [{"ref", "error"}]}."""
        if not hasattr(self.adapter, "close"):
            raise RuntimeError("these can't be closed from the game")
        out: dict = {"closed": [], "failed": []}
        async with self.lock:  # not while a poll might be taking the same issue in
            for ref in refs:
                try:
                    await self.adapter.close(ref, comment, reason, transition)
                    out["closed"].append(ref)
                except Exception as exc:
                    out["failed"].append({"ref": ref, "error": str(exc)})
        for ref in out["closed"]:
            print(f"[intake] {self.source}: closed {ref} from the game as {self.account_label()}")
        return out

    async def close_options(self, refs: list[str]) -> dict:
        """How Close would close these: Jira's transitions (for the first one); nothing for GitHub."""
        if hasattr(self.adapter, "close_options") and refs:
            return await self.adapter.close_options(refs[0])
        return {"transitions": [], "default": ""}

    def account_label(self) -> str:
        service = "jira" if self.source == "jira" else "github"
        return accounts.label(service, str(getattr(self.adapter, "account", "") or ""))

    async def listing(self) -> dict:
        out = {"source": self.source, "enabled": True, "about": self.adapter.describe(),
               "can_close": hasattr(self.adapter, "close"), "account": self.account_label(),
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
