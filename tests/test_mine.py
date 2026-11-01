from bugfix import store
from bugfix.github import GitHubClient
from bugfix.mine import mine_repo, repo_info
from tests.fake_github import FakeSession

REPO = "acme/widgets"
GOOD_DIFF = (
    "diff --git a/widgets/core.py b/widgets/core.py\n--- a/widgets/core.py\n+++ b/widgets/core.py\n"
    "@@ -1,2 +1,2 @@\n def f(x):\n-    return x\n+    return x or 0\n"
    "diff --git a/tests/test_core.py b/tests/test_core.py\n--- a/tests/test_core.py\n+++ b/tests/test_core.py\n"
    "@@ -1,1 +1,3 @@\n import widgets\n+def test_none():\n+    assert widgets.f(None) == 0\n"
)
NO_TESTS_DIFF = GOOD_DIFF.split("diff --git a/tests")[0]
SECRET_DIFF = GOOD_DIFF.replace("return x or 0", "return 'ghp_" + "a" * 36 + "'")


def pr(number, body="", linked=(), base="b" * 40, head="h" * 40):
    """One merged PR, as GitHub's GraphQL API returns it."""
    return {"number": number, "title": f"PR {number}", "body": body, "url": f"https://github.com/{REPO}/pull/{number}",
            "mergedAt": "2026-10-01T10:00:00Z", "mergeCommit": {"oid": f"m{number}"},
            "baseRefOid": base, "headRefOid": head,
            "closingIssuesReferences": {"nodes": [{"number": n, "repository": {"nameWithOwner": repo}}
                                                  for n, repo in linked]}}


def graphql_pages(*pages):
    """GraphQL pages of PRs; every page but the last says there's more."""
    return [{"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": i < len(pages) - 1, "endCursor": f"c{i}"}, "nodes": nodes}}}
        for i, nodes in enumerate(pages)]


def repo_routes(license_id="MIT", language="Python", root=("tests", "widgets", "README.md")):
    return {
        f"/repos/{REPO}": {"license": {"spdx_id": license_id}, "default_branch": "main", "stargazers_count": 5000,
                           "language": language, "archived": False, "fork": False, "private": False},
        f"/repos/{REPO}/contents": [{"name": n} for n in root],
    }


def make_client(routes):
    return GitHubClient("t", cache_dir=None, session=FakeSession(routes))


# ------------------------------------------------------------- repo checks

def test_good_repo_passes():
    assert repo_info(make_client(repo_routes()), REPO).problem is None


def test_copyleft_repo_is_skipped():
    assert "licence GPL-3.0" in repo_info(make_client(repo_routes(license_id="GPL-3.0")), REPO).problem


def test_repo_without_tests_is_skipped():
    assert "no tests" in repo_info(make_client(repo_routes(root=("src", "README.md"))), REPO).problem


def test_non_python_repo_is_skipped():
    assert "not Python" in repo_info(make_client(repo_routes(language="Go")), REPO).problem


# ------------------------------------------------------------------ mining

def full_routes():
    routes = repo_routes()
    routes["/graphql"] = graphql_pages(
        [pr(2, body="Just a refactor"),
         pr(3, body="Fixes #11"),                                     # no tests in the diff
         pr(4, body="Fixes #12\n\nThanks to jane@company.co.ke")],   # good: linked in the text
        [pr(5, body="Closes #12 (backport)"),                        # same fix again
         pr(6, body="Fixes #13"),                                     # secret in patch
         pr(7, body="Fixes #14"),                                     # "issue" 14 is really a PR
         pr(8, body="Small fix", linked=[(15, REPO)]),                # good: linked only in the sidebar
         pr(9, body="Port of upstream fix", linked=[(3, "other/repo")])],  # links another repo's issue
    )
    routes[f"/repos/{REPO}/pulls/3#diff"] = NO_TESTS_DIFF
    routes[f"/repos/{REPO}/pulls/4#diff"] = GOOD_DIFF
    routes[f"/repos/{REPO}/pulls/5#diff"] = GOOD_DIFF.replace("@@ -1,2 +1,2 @@", "@@ -40,2 +40,2 @@")
    routes[f"/repos/{REPO}/pulls/6#diff"] = SECRET_DIFF
    routes[f"/repos/{REPO}/pulls/7#diff"] = GOOD_DIFF.replace("x or 0", "x or 1")
    routes[f"/repos/{REPO}/pulls/8#diff"] = GOOD_DIFF.replace("x or 0", "x if x is not None else 0")
    routes[f"/repos/{REPO}/issues/15"] = {"number": 15, "title": "Edge case", "html_url": "https://github.com/acme/widgets/issues/15",
                                          "body": "Short description of the edge case here.", "labels": []}
    routes[f"/repos/{REPO}/issues/12"] = {"number": 12, "title": "f(None) crashes", "html_url": "https://github.com/acme/widgets/issues/12",
                                          "body": "Calling f(None) raises. Reported by bob@corp.example.co.uk " + "details " * 30,
                                          "labels": [{"name": "bug"}], "created_at": "2026-09-01T00:00:00Z"}
    routes[f"/repos/{REPO}/issues/14"] = {"number": 14, "title": "x", "html_url": "u", "pull_request": {}}
    routes[f"/repos/{REPO}/compare/{'b' * 40}...{'h' * 40}"] = {"merge_base_commit": {"sha": "mergebase123"}}
    routes[f"/repos/{REPO}/commits"] = [{"sha": "c1", "commit": {"message": "Tidy core\n\nlong text",
                                                                  "author": {"date": "2026-08-01T00:00:00Z"}}}]
    return routes


def test_mining_keeps_only_real_verifiable_fixes():
    result = mine_repo(make_client(full_routes()), REPO)

    assert result.prs_checked == 8  # across two GraphQL pages
    assert [r.id for r in result.records] == ["acme__widgets-4", "acme__widgets-8"]
    assert result.rejected == {
        "no linked issue": 2, "no test changes, so it can't be verified": 1,
        "duplicate of another fix": 1, "patch contains a secret": 1, "linked issue is missing or is itself a PR": 1,
    }


def test_sidebar_links_count_but_other_repos_issues_do_not():
    records = {r.id: r for r in mine_repo(make_client(full_routes()), REPO).records}
    assert records["acme__widgets-8"].issue.number == 15
    assert "acme__widgets-9" not in records


def test_max_prs_stops_early_without_fetching_more_pages():
    session = FakeSession(full_routes())
    result = mine_repo(GitHubClient("t", cache_dir=None, session=session), REPO, max_prs=2)
    assert result.prs_checked == 2
    assert sum(1 for path, _ in session.calls if path == "/graphql") == 1


def test_record_contents():
    record = mine_repo(make_client(full_routes()), REPO).records[0]

    assert record.license == "MIT"
    assert record.base_commit == "mergebase123"       # the merge base, not the base branch tip
    assert record.fix_commit == "m4"
    assert record.code_files == ["widgets/core.py"] and record.test_files == ["tests/test_core.py"]
    assert "return x or 0" in record.code_patch and "test_none" in record.test_patch
    assert record.issue.labels == ["bug"]
    assert record.history[0].message == "Tidy core"
    assert record.verification.status == "not_run"
    assert record.quality.score > 0 and record.quality.reasons


def test_personal_emails_are_removed_from_text():
    record = mine_repo(make_client(full_routes()), REPO).records[0]
    assert "bob@corp" not in record.issue.body and "[email]" in record.issue.body
    assert "jane@company" not in record.pr.body


def test_skipped_repo_is_reported_not_mined():
    routes = repo_routes(license_id="GPL-3.0")
    session = FakeSession(routes)
    result = mine_repo(GitHubClient("t", cache_dir=None, session=session), REPO)
    assert result.records == [] and "repo skipped" in next(iter(result.rejected))
    assert not any("/pulls" in path for path, _ in session.calls)


# ------------------------------------------------------------------- store

def test_store_round_trip_and_merge_without_duplicates(tmp_path):
    records = mine_repo(make_client(full_routes()), REPO).records
    path = tmp_path / "data" / "candidates.jsonl"
    store.save(path, records)
    loaded = store.load(path)
    assert loaded == records

    merged, added = store.merge(loaded, records)
    assert added == 0 and len(merged) == len(records)
    assert not (tmp_path / "data" / "candidates.jsonl.part").exists()