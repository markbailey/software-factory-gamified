"""The factory's own accounts: any number of named GitHub, Jira and Notion accounts, so the
factory can work as bot accounts instead of whoever is signed in on this machine, and different
lines or storages can work as different accounts.

They live in backend/accounts.toml, which is gitignored; tokens are never sent to the game (it
only learns whether one is set). Choosing an account, in a line's settings or a storage's:
    ""          the factory's default for that service (set in the Accounts window)
    "@machine"  this machine's own sign-in (gh auth login, git's credential manager, the
                JIRA_API_TOKEN variable with factory.toml's site and email, the NOTION_TOKEN
                variable)
    an id       that account (one that's since been removed means the default)

A command run as a GitHub account gets GH_TOKEN, a git credential helper for github.com that hands
git that token (through gh) instead of the machine's credential manager, and optionally
GIT_AUTHOR_* / GIT_COMMITTER_*. The default account is also put into this process's environment,
so everything else the factory runs (clones, checks) uses it too.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from .config import BACKEND_DIR, JiraIntakeConfig

ACCOUNTS_FILE = BACKEND_DIR / "accounts.toml"
MACHINE = "@machine"
GITHUB_HELPER_KEY = "credential.https://github.com.helper"
PLACEHOLDER_SITES = ("your-site.atlassian.net",)
SERVICES = ("github", "jira", "notion")
FIELDS = {"github": ("token", "git_name", "git_email"), "jira": ("base_url", "email", "token"),
          "notion": ("token",)}
# The env var a Notion token is read from for this machine's own sign-in ("@machine").
NOTION_TOKEN_ENV = "NOTION_TOKEN"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# The env var a Jira token is handed to scripts in (verify_issue.py, label_issue.py).
JIRA_TOKEN_VAR = "FACTORY_JIRA_TOKEN"
GITHUB_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
               "GIT_COMMITTER_EMAIL")

# What this process's environment had before the default account went into it: the machine's.
_machine_env: dict[str, str | None] = {}
_toml_jira: dict[str, str] = {}  # factory.toml's [intake.jira] site, email and token variable
_current: dict = {}  # the accounts in use (load() result)


def empty() -> dict:
    return {"github": [], "jira": [], "notion": [], "defaults": {"github": "", "jira": "", "notion": ""}}


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "account"
    out, n = base, 2
    while out in taken or out == MACHINE:
        out, n = f"{base}-{n}", n + 1
    return out


def _clean_account(service: str, raw: dict, taken: set[str]) -> dict:
    acc = {k: " ".join(str(raw.get(k) or "").split())[:2000] for k in FIELDS[service]}
    acc["name"] = " ".join(str(raw.get("name") or "").split())[:60]
    aid = str(raw.get("id") or "").strip().lower()
    acc["id"] = aid if ID_RE.match(aid) and aid not in taken else _slug(acc["name"] or service, taken)
    if not acc["name"]:
        acc["name"] = acc["id"]
    if service == "jira" and acc["base_url"]:
        url = acc["base_url"].rstrip("/")
        url = url if "://" in url else "https://" + url
        scheme, _, host = url.partition("://")
        if host and "." not in host.split("/")[0]:  # just the site name, e.g. "acme"
            url = f"{scheme}://{host.split('/')[0]}.atlassian.net"
        acc["base_url"] = url
    return acc


def load(path: Path | None = None) -> dict:
    path = path or ACCOUNTS_FILE
    out = empty()
    if not path.exists():
        return out
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as exc:
        print(f"[accounts] could not read {path.name}: {exc}")
        return out
    for service in SERVICES:
        got = raw.get(service)
        if isinstance(got, dict):  # the first version: one account per service, used for everything
            got = [{"id": "factory", "name": "Factory", **got}] if got.get("token") else []
            out["defaults"][service] = "factory" if got else ""
        taken: set[str] = set()
        for entry in got if isinstance(got, list) else []:
            if isinstance(entry, dict):
                acc = _clean_account(service, entry, taken)
                taken.add(acc["id"])
                out[service].append(acc)
    defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
    for service in SERVICES:
        if service in defaults:
            out["defaults"][service] = str(defaults[service] or "")
    return out


def save(accounts: dict, path: Path | None = None) -> None:
    path = path or ACCOUNTS_FILE
    q = lambda v: json.dumps(str(v))  # noqa: E731  (a JSON string is a TOML basic string)
    lines = ["# The Software Factory's own accounts. Kept out of git (.gitignore): never commit it.",
             "# Set them in the game (Accounts: the gear in the top bar), or edit here and restart the backend.",
             '# defaults: the account each service uses unless a line or storage picks another; "" or',
             '# "@machine" for this machine\'s own sign-in.', "",
             "[defaults]", f"github = {q(accounts['defaults'].get('github', ''))}",
             f"jira = {q(accounts['defaults'].get('jira', ''))}",
             f"notion = {q(accounts['defaults'].get('notion', ''))}", ""]
    for service in SERVICES:
        for acc in accounts.get(service, []):
            lines.append(f"[[{service}]]")
            for k in ("id", "name", *FIELDS[service]):
                lines.append(f"{k} = {q(acc.get(k, ''))}")
            lines.append("")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, 0o600)


def upsert(accounts: dict, service: str, changes: dict) -> tuple[dict, str]:
    """Add an account (no id, or a new one) or change one. A token left out (or null) is kept;
    "" clears it. Returns (accounts, its id)."""
    if service not in SERVICES or not isinstance(changes, dict):
        raise ValueError("unknown service")
    out = {**accounts, service: [dict(a) for a in accounts.get(service, [])]}
    aid = str(changes.get("id") or "")
    old = next((a for a in out[service] if a["id"] == aid), None)
    if old is None:
        taken = {a["id"] for a in out[service]}
        acc = _clean_account(service, {**changes, "id": ""}, taken)
        out[service].append(acc)
        if not out["defaults"].get(service) and len(out[service]) == 1:
            out["defaults"] = {**out["defaults"], service: acc["id"]}  # the first one: the default
        return out, acc["id"]
    merged = {**old, **{k: v for k, v in changes.items() if v is not None and k in (*FIELDS[service], "name")}}
    acc = _clean_account(service, {**merged, "id": ""}, set())
    acc["id"] = old["id"]
    old.clear()
    old.update(acc)
    return out, acc["id"]


def remove(accounts: dict, service: str, aid: str) -> dict:
    out = {**accounts, service: [a for a in accounts.get(service, []) if a["id"] != aid]}
    if out["defaults"].get(service) == aid:
        out["defaults"] = {**out["defaults"], service: ""}
    return out


def set_default(accounts: dict, service: str, aid: str) -> dict:
    if aid not in ("", MACHINE) and not any(a["id"] == aid for a in accounts.get(service, [])):
        raise ValueError("no such account")
    return {**accounts, "defaults": {**accounts["defaults"], service: "" if aid == MACHINE else aid}}


# Choosing ---------------------------------------------------------------------------------------
def account(service: str, choice: str = "") -> dict | None:
    """The account a choice means, or None for this machine's own sign-in."""
    accs = {a["id"]: a for a in _current.get(service, [])}
    if choice == MACHINE:
        return None
    if choice in accs:
        return accs[choice]
    return accs.get(_current.get("defaults", {}).get(service, ""))


def label(service: str, choice: str = "") -> str:
    acc = account(service, choice)
    return f"the {acc['name']} account" if acc else "this machine's sign-in"


def _gh_helper() -> str:
    gh = shutil.which("gh")
    path = Path(gh).as_posix() if gh else "gh"
    return f'!"{path}" auth git-credential'


def github_env(choice: str = "") -> dict[str, str | None]:
    """Environment changes for a command run as this GitHub account (None: unset it)."""
    acc = account("github", choice)
    out: dict[str, str | None] = {name: _machine_env.get(name, os.environ.get(name)) for name in GITHUB_VARS}
    base = _machine_env.get("GIT_CONFIG_COUNT", os.environ.get("GIT_CONFIG_COUNT"))
    out["GIT_CONFIG_COUNT"] = base
    for i in range(int(base or 0), int(base or 0) + 2):  # ours, if the default put them there
        for kv in ("KEY", "VALUE"):
            out[f"GIT_CONFIG_{kv}_{i}"] = _machine_env.get(f"GIT_CONFIG_{kv}_{i}")
    if acc is None or not acc.get("token"):
        return out
    out["GH_TOKEN"] = out["GITHUB_TOKEN"] = acc["token"]
    n = int(base or 0)
    for i, value in enumerate(("", _gh_helper())):
        out[f"GIT_CONFIG_KEY_{n + i}"] = GITHUB_HELPER_KEY
        out[f"GIT_CONFIG_VALUE_{n + i}"] = value
    out["GIT_CONFIG_COUNT"] = str(n + 2)
    for who in ("AUTHOR", "COMMITTER"):
        if acc.get("git_name"):
            out[f"GIT_{who}_NAME"] = acc["git_name"]
        if acc.get("git_email"):
            out[f"GIT_{who}_EMAIL"] = acc["git_email"]
    return out


@dataclass
class JiraCreds:
    base_url: str
    email: str
    token: str
    name: str  # "the X account" / "this machine's sign-in"


def jira_creds(choice: str = "") -> JiraCreds:
    acc = account("jira", choice)
    if acc is None:
        token = os.environ.get(_toml_jira.get("token_env", "JIRA_API_TOKEN"), "")
        return JiraCreds(_toml_jira.get("base_url", ""), _toml_jira.get("email", ""), token or "",
                         "this machine's sign-in")
    return JiraCreds(acc.get("base_url", ""), acc.get("email", ""), acc.get("token", ""), f"the {acc['name']} account")


def jira_ready(creds: JiraCreds) -> str:
    """"" if these can talk to Jira, or what's missing."""
    missing = []
    if not creds.base_url or any(p in creds.base_url for p in PLACEHOLDER_SITES):
        missing.append("the Jira site")
    if not creds.email:
        missing.append("the account's email")
    if not creds.token:
        missing.append("an API token")
    if not missing:
        return ""
    return (f"Jira isn't set up for {creds.name}: add " + ", ".join(missing) +
            " in Accounts (the gear in the top bar), or pick another Jira account.")


def notion_token(choice: str = "") -> str:
    """The Notion integration token a choice means: the account's, or for this machine's own
    sign-in the NOTION_TOKEN variable."""
    acc = account("notion", choice)
    if acc is None:
        return os.environ.get(NOTION_TOKEN_ENV, "")
    return acc.get("token", "")


def notion_ready(choice: str = "") -> str:
    """"" if there's a Notion token to use, or what's missing."""
    if notion_token(choice):
        return ""
    who = label("notion", choice)
    return (f"Notion isn't set up for {who}: add an integration token in Accounts (the gear in the top bar), "
            "or pick another Notion account.")


def jira_env(choice: str = "") -> dict[str, str | None]:
    return {JIRA_TOKEN_VAR: jira_creds(choice).token or None}


def process_env(changes: dict[str, str | None], base: dict | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for k, v in changes.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def use(accounts: dict, jira: JiraIntakeConfig) -> None:
    """Start using these accounts: the default GitHub account goes into this process's
    environment, and factory.toml's Jira site and token variable are remembered as the machine's."""
    if not _toml_jira:
        _toml_jira.update(base_url=jira.base_url, email=jira.email, token_env=jira.token_env)
    if not _machine_env:  # only the first time: after that, the default account is in there
        for name in (*GITHUB_VARS, "GIT_CONFIG_COUNT"):
            _machine_env[name] = os.environ.get(name)
        n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
        for i in (n, n + 1):  # the two entries ours go in (normally unset)
            for kv in ("KEY", "VALUE"):
                _machine_env[f"GIT_CONFIG_{kv}_{i}"] = os.environ.get(f"GIT_CONFIG_{kv}_{i}")
    _current.clear()
    _current.update(accounts)
    for k, v in github_env("").items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
            if v == "" and os.name == "nt":
                _keep_empty(k)


def _keep_empty(name: str) -> None:
    """Windows' C runtime deletes a variable set to "" (os.environ keeps it, the real process
    environment doesn't), so a child that inherits the environment (subprocess without env=)
    saw GIT_CONFIG_COUNT but no GIT_CONFIG_VALUE_<n> and git died: "missing config value".
    Put the empty value into the process environment directly."""
    import ctypes
    ctypes.windll.kernel32.SetEnvironmentVariableW(name, "")


def summary(accounts: dict) -> dict:
    """What the game may see: everything but the tokens, and whether each is set."""
    machine = jira_creds(MACHINE)
    return {
        "github": [{"id": a["id"], "name": a["name"], "token_set": bool(a.get("token")),
                    "git_name": a.get("git_name", ""), "git_email": a.get("git_email", "")}
                   for a in accounts.get("github", [])],
        "jira": [{"id": a["id"], "name": a["name"], "token_set": bool(a.get("token")),
                  "base_url": a.get("base_url", ""), "email": a.get("email", "")}
                 for a in accounts.get("jira", [])],
        "notion": [{"id": a["id"], "name": a["name"], "token_set": bool(a.get("token"))}
                   for a in accounts.get("notion", [])],
        "defaults": dict(accounts.get("defaults", {})),
        "machine": {"jira_site": machine.base_url, "jira_email": machine.email,
                    "jira_ready": jira_ready(machine) == "",
                    "notion_ready": bool(os.environ.get(NOTION_TOKEN_ENV))},
        "file": str(ACCOUNTS_FILE),
    }


def _check_github(choice: str) -> dict:
    gh = shutil.which("gh")
    if not gh:
        return {"ok": False, "message": "gh is not installed or not on PATH."}
    try:
        r = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True, encoding="utf-8",
                           timeout=60, env=process_env(github_env(choice)))
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:400]}
    if r.returncode == 0 and r.stdout.strip():
        return {"ok": True, "login": r.stdout.strip(), "message": f"Signed in to GitHub as {r.stdout.strip()}."}
    return {"ok": False, "message": ((r.stderr or r.stdout).strip() or "gh failed")[:400]}


def _check_jira(choice: str) -> dict:
    creds = jira_creds(choice)
    missing = jira_ready(creds)
    if missing:
        return {"ok": False, "message": missing}
    auth = base64.b64encode(f"{creds.email}:{creds.token}".encode()).decode()
    req = urllib.request.Request(creds.base_url + "/rest/api/3/myself",
                                 headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            me = json.loads(resp.read() or b"{}")
    except Exception as exc:
        hint = " Check the email and API token." if "401" in str(exc) or "403" in str(exc) else ""
        return {"ok": False, "message": f"Could not sign in to {creds.base_url}: {exc}.{hint}"}
    name = me.get("displayName") or me.get("emailAddress") or creds.email
    return {"ok": True, "login": name, "message": f"Signed in to {creds.base_url} as {name}."}


def _check_notion(choice: str) -> dict:
    missing = notion_ready(choice)
    if missing:
        return {"ok": False, "message": missing}
    from .docs import NotionAdapter
    try:
        name = NotionAdapter(notion_token(choice)).whoami()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Could not sign in to Notion: {exc}"[:400]}
    return {"ok": True, "login": name, "message": f"Signed in to Notion as {name}."}


def check(accounts: dict) -> dict:
    """Who each account (and this machine's sign-in) is on its service (blocking: run in a thread)."""
    return {"github": {a["id"]: _check_github(a["id"]) for a in accounts.get("github", [])} |
                      {MACHINE: _check_github(MACHINE)},
            "jira": {a["id"]: _check_jira(a["id"]) for a in accounts.get("jira", [])} |
                    {MACHINE: _check_jira(MACHINE)},
            "notion": {a["id"]: _check_notion(a["id"]) for a in accounts.get("notion", [])} |
                      {MACHINE: _check_notion(MACHINE)}}
