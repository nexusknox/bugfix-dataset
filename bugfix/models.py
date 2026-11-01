"""The shape of one dataset record.

One record = one real bug: the defect ticket (issue), the pull request that
fixed it, the fix split into a code patch and a test patch, a slice of
history, and the evidence from running the tests.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str] = field(default_factory=list)
    created_at: str = ""


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    url: str
    merged_at: str = ""


@dataclass
class Commit:
    sha: str
    message: str
    date: str = ""


@dataclass
class Verification:
    status: str = "not_run"            # not_run, verified, failed, error
    fail_before: list[str] = field(default_factory=list)   # tests failing without the fix
    pass_after: list[str] = field(default_factory=list)    # the same tests passing with it
    log_before: str = ""
    log_after: str = ""
    python_version: str = ""
    note: str = ""


@dataclass
class Quality:
    score: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Record:
    id: str                       # e.g. "psf__requests-6028"
    repo: str                     # "owner/name"
    repo_url: str
    license: str                  # SPDX id, e.g. "MIT"
    issue: Issue
    pr: PullRequest
    base_commit: str              # the code before the fix
    fix_commit: str               # the merged fix
    code_patch: str               # changes to source files only
    test_patch: str               # changes to test files only
    code_files: list[str]
    test_files: list[str]
    history: list[Commit] = field(default_factory=list)  # recent commits to the fixed files
    verification: Verification = field(default_factory=Verification)
    quality: Quality = field(default_factory=Quality)
    fingerprint: str = ""         # for finding duplicates
    collected_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        data = dict(data)
        data["issue"] = Issue(**data["issue"])
        data["pr"] = PullRequest(**data["pr"])
        data["history"] = [Commit(**c) for c in data.get("history", [])]
        data["verification"] = Verification(**data.get("verification", {}))
        data["quality"] = Quality(**data.get("quality", {}))
        return cls(**data)


def record_id(repo: str, pr_number: int) -> str:
    """'psf/requests', 6028 -> 'psf__requests-6028' (same style as SWE-bench)."""
    owner, name = repo.split("/", 1)
    return f"{owner}__{name}-{pr_number}"
