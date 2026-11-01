"""Finding real bug fixes on GitHub and turning them into records.

Cheap checks run first, so most pull requests cost no extra API calls:
  1. merged?               (already in the PR list)
  2. closes an issue?      (PR text, no call)
  3. code + tests changed? (1 call: the diff)
  4. the issue, the true starting commit and recent history (3-4 calls)
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bugfix.github import GitHubClient
from bugfix.models import Commit, Issue, PullRequest, Quality, Record, record_id
from bugfix.rules import (
    closing_issue_numbers, find_secrets, fingerprint, license_allowed, quality_score,
    rejection_reasons, scrub_text, split_fix,
)

TEST_MARKERS = {"tests", "test", "pytest.ini", "conftest.py", "tox.ini"}


@dataclass
class RepoInfo:
    name: str
    license: str | None
    default_branch: str
    stars: int
    has_tests: bool
    problem: str | None = None  # why it can't be mined, if it can't


@dataclass
class MineResult:
    records: list[Record] = field(default_factory=list)
    rejected: Counter = field(default_factory=Counter)  # reason -> count
    prs_checked: int = 0


# ---------------------------------------------------------------- repos

def repo_info(client: GitHubClient, repo: str) -> RepoInfo:
    data = client.get_json(f"/repos/{repo}")
    if data is None:
        return RepoInfo(repo, None, "", 0, False, "repository not found")
    spdx = (data.get("license") or {}).get("spdx_id")
    root = client.get_json(f"/repos/{repo}/contents") or []
    names = {item["name"].lower() for item in root}
    info = RepoInfo(repo, spdx, data.get("default_branch", "main"), data.get("stargazers_count", 0),
                    bool(names & TEST_MARKERS))
    if data.get("archived") or data.get("fork") or data.get("private"):
        info.problem = "archived, fork or private"
    elif not license_allowed(spdx):
        info.problem = f"licence {spdx or 'missing'} doesn't allow reuse"
    elif (data.get("language") or "") != "Python":
        info.problem = f"main language is {data.get('language')}, not Python"
    elif not info.has_tests:
        info.problem = "no tests folder or pytest config at the top level"
    return info


def discover(client: GitHubClient, min_stars: int = 1000, limit: int = 20) -> list[RepoInfo]:
    """Popular Python repos that pass every repo-level check."""
    found = []
    query = f"language:Python stars:>={min_stars} archived:false fork:false"
    for item in client.paginate("/search/repositories", {"q": query, "sort": "stars"}, limit=limit * 5):
        info = repo_info(client, item["full_name"])
        if info.problem is None:
            found.append(info)
            if len(found) >= limit:
                break
    return found


# ---------------------------------------------------------------- mining

def mine_repo(client: GitHubClient, repo: str, max_prs: int = 300) -> MineResult:
    """Check up to `max_prs` recently closed pull requests in one repo."""
    result = MineResult()
    info = repo_info(client, repo)
    if info.problem:
        result.rejected[f"repo skipped: {info.problem}"] += 1
        return result

    seen_fingerprints: set[str] = set()
    pulls = client.paginate(f"/repos/{repo}/pulls",
                            {"state": "closed", "sort": "updated", "direction": "desc"}, limit=max_prs)
    for pr in pulls:
        result.prs_checked += 1
        reason = None
        if not pr.get("merged_at"):
            reason = "not merged"
        else:
            issues = closing_issue_numbers(f"{pr.get('title', '')}\n{pr.get('body') or ''}", repo)
            if not issues:
                reason = "no linked issue"
            else:
                record, reason = build_record(client, repo, info, pr, issues)
                if record is not None:
                    if record.fingerprint in seen_fingerprints:
                        reason = "duplicate of another fix"
                    else:
                        seen_fingerprints.add(record.fingerprint)
                        result.records.append(record)
        if reason:
            result.rejected[reason] += 1
    return result


def build_record(client, repo: str, info: RepoInfo, pr: dict, issues: list[int]):
    """(Record, None) if the PR is a good candidate, else (None, reason)."""
    diff = client.get_diff(f"/repos/{repo}/pulls/{pr['number']}")
    if not diff:
        return None, "diff unavailable"
    fix = split_fix(diff)
    reasons = rejection_reasons(fix, issues)
    if reasons:
        return None, reasons[0]
    if find_secrets(fix.code_patch + fix.test_patch):
        return None, "patch contains a secret"

    issue = client.get_json(f"/repos/{repo}/issues/{issues[0]}")
    if issue is None or "pull_request" in issue:
        return None, "linked issue is missing or is itself a PR"

    # The PR diff is relative to where its branch split from the base branch
    # (the merge base), not the base branch's latest commit. Use that, or the
    # patches might not apply.
    compare = client.get_json(f"/repos/{repo}/compare/{pr['base']['sha']}...{pr['head']['sha']}")
    if not compare or not compare.get("merge_base_commit"):
        return None, "couldn't find the starting commit"
    base_commit = compare["merge_base_commit"]["sha"]

    history = []
    for c in client.get_json(f"/repos/{repo}/commits", {"sha": base_commit, "path": fix.code_files[0],
                                                         "per_page": 5}) or []:
        history.append(Commit(c["sha"], scrub_text(c["commit"]["message"].split("\n")[0]),
                              c["commit"]["author"]["date"]))

    score, why = quality_score(issue.get("body") or "", fix, verified=False)
    record = Record(
        id=record_id(repo, pr["number"]),
        repo=repo,
        repo_url=f"https://github.com/{repo}",
        license=info.license,
        issue=Issue(issue["number"], scrub_text(issue["title"]), scrub_text(issue.get("body") or ""),
                    issue["html_url"], [label["name"] for label in issue.get("labels", [])],
                    issue.get("created_at", "")),
        pr=PullRequest(pr["number"], scrub_text(pr["title"]), scrub_text(pr.get("body") or ""),
                       pr["html_url"], pr["merged_at"]),
        base_commit=base_commit,
        fix_commit=pr.get("merge_commit_sha") or "",
        code_patch=fix.code_patch,
        test_patch=fix.test_patch,
        code_files=fix.code_files,
        test_files=fix.test_files,
        history=history,
        quality=Quality(score, why),
        fingerprint=fingerprint(fix.code_patch),
        collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return record, None