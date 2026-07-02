"""The scheduler's GitHub surface, against a faked requests transport."""

import pytest
from control_plane.github_api import (
    COPILOT_REVIEWER,
    GitHubApi,
    GitHubApiError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeTransport:
    """Records calls; replies from a {(method, path): response} table."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, *, headers=None, params=None, json=None, timeout=None):
        path = url.split("api.github.com", 1)[1]
        self.calls.append({"method": method, "path": path, "params": params, "json": json, "headers": headers})
        return self.routes.get((method, path), FakeResponse(200, payload=None))


def _api(routes):
    transport = FakeTransport(routes)
    return GitHubApi("owner/product", "ghs_test", request=transport), transport


def test_list_ready_issues_filters_prs_and_parses_labels():
    api, transport = _api(
        {
            ("GET", "/repos/owner/product/issues"): FakeResponse(
                200,
                payload=[
                    {"number": 7, "title": "fix it", "body": "text", "labels": [{"name": "ready"}, {"name": "human"}]},
                    {"number": 8, "title": "a pr", "labels": [], "pull_request": {"url": "x"}},
                    {"number": 9, "title": "auto one", "body": None, "labels": [{"name": "ready"}]},
                ],
            )
        }
    )
    issues = api.list_ready_issues()
    assert [issue.number for issue in issues] == [7, 9]
    assert issues[0].provenance == "human"
    assert issues[1].provenance == "auto"  # unlabelled = tainted
    assert issues[1].body == ""
    assert transport.calls[0]["params"]["labels"] == "ready"


def test_label_ops_and_tolerated_404():
    api, transport = _api(
        {("DELETE", "/repos/owner/product/issues/7/labels/ready"): FakeResponse(404, text="not there")}
    )
    api.remove_label(7, "ready")  # 404 = already gone — fine
    api.add_labels(7, ["in-progress"])
    assert transport.calls[1]["json"] == {"labels": ["in-progress"]}


def test_errors_raise_with_context():
    api, _ = _api({("POST", "/repos/owner/product/issues/7/comments"): FakeResponse(403, text="forbidden")})
    with pytest.raises(GitHubApiError, match="commenting on #7.*403"):
        api.comment(7, "hello")


def test_open_pr_matched_by_branch_prefix():
    api, _ = _api(
        {
            ("GET", "/repos/owner/product/pulls"): FakeResponse(
                200,
                payload=[
                    {"number": 2, "state": "open", "head": {"ref": "dev/1-coinflip", "sha": "abc"}},
                    {"number": 3, "state": "open", "head": {"ref": "dev/12-other", "sha": "def"}},
                ],
            )
        }
    )
    pr = api.open_pr_with_branch_prefix("dev/1-")
    assert pr is not None and pr.number == 2  # not the dev/12- one
    assert api.open_pr_with_branch_prefix("dev/99-") is None


def test_reviews_and_re_request():
    api, transport = _api(
        {
            ("GET", "/repos/owner/product/pulls/2/reviews"): FakeResponse(
                200,
                payload=[
                    {"id": 10, "user": {"login": "MakhimKitov"}, "state": "CHANGES_REQUESTED", "body": "clown it"},
                    {"id": 11, "user": {"login": COPILOT_REVIEWER}, "state": "COMMENTED", "body": None},
                ],
            )
        }
    )
    reviews = api.list_reviews(2)
    assert reviews[0].author == "MakhimKitov" and reviews[0].state == "CHANGES_REQUESTED"
    assert reviews[1].body == ""
    api.request_reviewers(2, [COPILOT_REVIEWER, "MakhimKitov"])
    assert transport.calls[-1]["json"] == {"reviewers": [COPILOT_REVIEWER, "MakhimKitov"]}


def test_token_supplier_is_consulted_per_call():
    tokens = iter(["ghs_first", "ghs_second"])
    transport = FakeTransport({})
    api = GitHubApi("owner/product", lambda: next(tokens), request=transport)
    api.comment(1, "one")
    api.comment(2, "two")
    auths = [call["headers"]["Authorization"] for call in transport.calls]
    assert auths == ["Bearer ghs_first", "Bearer ghs_second"]
