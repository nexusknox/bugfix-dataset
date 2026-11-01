import time

import pytest

from bugfix.github import GitHubClient, GitHubError
from tests.fake_github import FakeResponse, FakeSession


def client(routes, tmp_path=None, sleeps=None):
    return GitHubClient("token", cache_dir=tmp_path, session=FakeSession(routes),
                        sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_results_are_cached_on_disk(tmp_path):
    c = client({"/repos/a/b": {"name": "b"}}, tmp_path)
    assert c.get_json("/repos/a/b") == {"name": "b"}
    assert c.get_json("/repos/a/b") == {"name": "b"}
    assert c.requests_made == 1


def test_cache_survives_a_new_run(tmp_path):
    client({"/repos/a/b": {"name": "b"}}, tmp_path).get_json("/repos/a/b")
    second = client({}, tmp_path)  # GitHub would now say 404
    assert second.get_json("/repos/a/b") == {"name": "b"}
    assert second.requests_made == 0


def test_not_found_is_none():
    assert client({}).get_json("/repos/a/missing") is None


def test_bad_token_gives_a_friendly_error():
    with pytest.raises(GitHubError, match="token"):
        client({"/user": FakeResponse(401, {})}).get_json("/user")


def test_rate_limit_waits_then_retries():
    reset = str(int(time.time()) + 30)
    routes = {"/repos/a/b": [FakeResponse(403, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}),
                             FakeResponse(200, {"ok": True})]}
    sleeps = []
    assert client(routes, sleeps=sleeps).get_json("/repos/a/b") == {"ok": True}
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 31


def test_long_rate_limit_stops_with_a_message():
    reset = str(int(time.time()) + 3600)
    routes = {"/repos/a/b": FakeResponse(403, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})}
    with pytest.raises(GitHubError, match="rate limit"):
        client(routes).get_json("/repos/a/b")


def test_server_hiccups_are_retried():
    routes = {"/repos/a/b": [FakeResponse(502, {}), FakeResponse(200, {"ok": 1})]}
    assert client(routes).get_json("/repos/a/b") == {"ok": 1}


def test_paging_follows_full_pages_and_respects_the_limit():
    routes = {("/items", 1): [{"n": i} for i in range(100)], ("/items", 2): [{"n": i} for i in range(100, 130)]}
    c = client(routes)
    assert len(list(c.paginate("/items"))) == 130
    assert len(list(c.paginate("/items", limit=50))) == 50


def test_search_results_are_unwrapped():
    c = client({"/search/repositories": {"items": [{"full_name": "a/b"}]}})
    assert list(c.paginate("/search/repositories")) == [{"full_name": "a/b"}]


def test_diffs_are_fetched_as_text(tmp_path):
    c = client({"/repos/a/b/pulls/1#diff": "diff --git a/x b/x\n"}, tmp_path)
    assert c.get_diff("/repos/a/b/pulls/1").startswith("diff --git")


def test_graphql_is_cached_and_needs_a_token(tmp_path):
    page = {"repository": {"x": 1}}
    c = client({"/graphql": [page]}, tmp_path)
    assert c.graphql("query", {"a": 1}) == page
    assert c.graphql("query", {"a": 1}) == page  # second time from the cache
    assert c.requests_made == 1

    no_token = GitHubClient(None, cache_dir=None, session=FakeSession({}))
    with pytest.raises(GitHubError, match="token"):
        no_token.graphql("query", {})