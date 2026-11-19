"""PR titles and bodies name the crate's ticket (Jira key or GitHub issue), never the crate number."""
from factory.engine import _pr_body, _pr_links, _pr_title


def test_plain_title_has_no_crate_number():
    assert _pr_title({"id": 7, "text": "Fix login"}, "Fix login") == "Fix login"


def test_jira_key_leads_the_title_and_is_in_the_body():
    item = {"id": 7, "text": "Fix login", "source": "jira", "source_ref": "ABC-123",
            "source_url": "https://x.atlassian.net/browse/ABC-123"}
    assert _pr_title(item, "Fix login") == "ABC-123: Fix login"
    assert "ABC-123" in _pr_body(item)
    assert "#7" not in _pr_body(item)


def test_github_issue_leads_the_title_and_closes_it():
    item = {"id": 7, "text": "Fix login", "source": "github", "source_ref": "12",
            "source_url": "https://github.com/o/r/issues/12"}
    assert _pr_title(item, "Fix login") == "#12: Fix login"
    assert _pr_links(item) == "Closes o/r#12"


def test_document_writer_title_gets_the_ticket_once():
    item = {"id": 7, "source": "jira", "source_ref": "ABC-123", "pr_title": "Make login retry"}
    assert _pr_title(item, "x") == "ABC-123: Make login retry"
    item["pr_title"] = "ABC-123 Make login retry"
    assert _pr_title(item, "x") == "ABC-123 Make login retry"
    assert _pr_title({"source": "jira", "source_ref": "ABC-12", "pr_title": "ABC-123 x"}, "x") == "ABC-12: ABC-123 x"
