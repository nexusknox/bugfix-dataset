"""The rules that decide what goes into the dataset.

All pure functions: text and numbers in, decisions out. No network, no
Docker. That makes them fast to test, and easy to point to when someone asks
"why was this record included?"
"""

import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Licences: only ones that allow reuse, including commercial use
# ---------------------------------------------------------------------------

PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "Zlib"}


def license_allowed(spdx_id: str | None) -> bool:
    """GPL/LGPL/AGPL, 'NOASSERTION' (unclear) and no licence at all are excluded."""
    return spdx_id in PERMISSIVE_LICENSES


# ---------------------------------------------------------------------------
# Linking a pull request to the issue it fixes
# ---------------------------------------------------------------------------

CLOSING_WORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"


def closing_issue_numbers(text: str, repo: str) -> list[int]:
    """Issue numbers in THIS repo that a PR description says it closes.

    Matches GitHub's own keywords: 'Fixes #12', 'closes owner/repo#12',
    'Resolves https://github.com/owner/repo/issues/12'. A mention of
    another repo's issue doesn't count.
    """
    if not text:
        return []
    repo_pattern = re.escape(repo)
    patterns = [
        rf"\b{CLOSING_WORDS}:?\s+#(\d+)\b",
        rf"\b{CLOSING_WORDS}:?\s+{repo_pattern}#(\d+)\b",
        rf"\b{CLOSING_WORDS}:?\s+https://github\.com/{repo_pattern}/issues/(\d+)\b",
    ]
    found: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            number = int(match.group(1))
            if number not in found:
                found.append(number)
    return found


# ---------------------------------------------------------------------------
# Splitting a fix into "code" and "tests"
# ---------------------------------------------------------------------------

@dataclass
class FileDiff:
    path: str
    text: str
    added: int
    removed: int


def split_diff(diff: str) -> list[FileDiff]:
    """Cut a unified diff (as `git diff` prints it) into one piece per file."""
    pieces = re.split(r"(?m)^(?=diff --git )", diff)
    files = []
    for piece in pieces:
        if not piece.startswith("diff --git "):
            continue
        header = piece.splitlines()[0]
        match = re.match(r"diff --git a/(.+?) b/(.+)$", header)
        if not match:
            continue
        path = match.group(2)
        added = removed = 0
        for line in piece.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        files.append(FileDiff(path, piece, added, removed))
    return files


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    parts = path.lower().split("/")
    return (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
        or any(part in ("test", "tests", "testing") for part in parts[:-1])
    )


PROJECT_INFO_NAMES = ("authors", "contributors", "changelog", "changes", "history", "license", "licence",
                      "notice", "codeowners", "manifest.in", "readme")
OTHER_LANGUAGES = (".c", ".h", ".cc", ".cpp", ".hpp", ".pyx", ".pxd", ".js", ".jsx", ".ts", ".tsx",
                   ".rs", ".go", ".java", ".rb", ".cs", ".swift", ".kt")


def is_ignorable(path: str) -> bool:
    """Docs and project info a fix may touch that don't affect the tests:
    docs, changelogs, AUTHORS, licences, CI config, requirements files."""
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    return (
        lower.endswith((".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".in", ".lock"))
        or lower.startswith(("docs/", "doc/", ".github/", "changelog/", "changes/", "news/", "requirements/"))
        or name.split(".")[0] in PROJECT_INFO_NAMES
    )


def is_other_language(path: str) -> bool:
    """Source in a language pytest can't verify on its own (needs compiling, etc.)."""
    return path.lower().endswith(OTHER_LANGUAGES)


@dataclass
class SplitFix:
    code_patch: str
    test_patch: str
    code_files: list[str]      # Python files first, then data files and templates the fix needs
    test_files: list[str]
    other_files: list[str]     # other-language source we can't verify (C, JS…)
    changed_lines: int         # Python code lines only


def split_fix(diff: str) -> SplitFix:
    code, tests, other = [], [], []
    for f in split_diff(diff):
        if is_test_file(f.path):
            tests.append(f)
        elif is_ignorable(f.path):
            continue
        elif is_other_language(f.path):
            other.append(f)
        else:
            code.append(f)  # .py, plus templates/data files the fix may need
    code.sort(key=lambda f: not f.path.endswith(".py"))
    return SplitFix(
        code_patch="".join(f.text for f in code),
        test_patch="".join(f.text for f in tests),
        code_files=[f.path for f in code],
        test_files=[f.path for f in tests],
        other_files=[f.path for f in other],
        changed_lines=sum(f.added + f.removed for f in code if f.path.endswith(".py")),
    )


# ---------------------------------------------------------------------------
# Which fixes are worth keeping
# ---------------------------------------------------------------------------

MAX_CODE_LINES = 300   # bigger than this is usually a refactor, not a bug fix
MAX_CODE_FILES = 5


def rejection_reasons(fix: SplitFix, linked_issues: list[int]) -> list[str]:
    """Empty list = eligible. Otherwise, every reason it was rejected."""
    reasons = []
    if not linked_issues:
        reasons.append("no linked issue")
    if not any(path.endswith(".py") for path in fix.code_files):
        reasons.append("no Python source changes")
    if not fix.test_files:
        reasons.append("no test changes, so it can't be verified")
    if fix.other_files:
        reasons.append("changes another language's source: " + ", ".join(fix.other_files[:3]))
    if fix.changed_lines > MAX_CODE_LINES:
        reasons.append(f"too large ({fix.changed_lines} code lines changed)")
    if len(fix.code_files) > MAX_CODE_FILES:
        reasons.append(f"touches too many files ({len(fix.code_files)})")
    return reasons


# ---------------------------------------------------------------------------
# Privacy and secrets
# ---------------------------------------------------------------------------

SECRET_PATTERNS = {
    "GitHub token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "Slack token": r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b",
    "Google API key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")


def find_secrets(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS.items() if re.search(pattern, text or "")]


def scrub_text(text: str) -> str:
    """For prose only (issue and PR text, logs): hide emails and secrets.

    Patches are never edited this way: changing a patch would stop it
    applying, and the record could no longer be verified. A record whose
    patches contain secrets is dropped instead.
    """
    if not text:
        return text

    def hide_email(match: re.Match) -> str:
        return match.group(0) if match.group(0).lower().endswith(SAFE_EMAIL_DOMAINS) else "[email]"

    text = EMAIL.sub(hide_email, text)
    for name, pattern in SECRET_PATTERNS.items():
        text = re.sub(pattern, f"[{name} removed]", text)
    return text


# ---------------------------------------------------------------------------
# Duplicates and quality
# ---------------------------------------------------------------------------

def fingerprint(code_patch: str) -> str:
    """Same fix = same fingerprint, even if it was cherry-picked to another
    branch with different line numbers or whitespace."""
    changed = [
        re.sub(r"\s+", " ", line[1:]).strip()
        for line in code_patch.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return hashlib.sha256("\n".join(c for c in changed if c).encode()).hexdigest()[:16]


def quality_score(issue_body: str, fix: SplitFix, verified: bool) -> tuple[int, list[str]]:
    """0-100, with the reasons, so every score can be explained."""
    score, reasons = 0, []
    if verified:
        score += 50
        reasons.append("+50 verified: tests fail before the fix and pass after")
    body = len((issue_body or "").strip())
    if body >= 200:
        score += 20
        reasons.append("+20 detailed issue description")
    elif body >= 50:
        score += 10
        reasons.append("+10 short issue description")
    else:
        reasons.append("+0 issue has little or no description")
    if fix.changed_lines <= 50:
        score += 15
        reasons.append("+15 small, focused fix")
    elif fix.changed_lines <= 150:
        score += 8
        reasons.append("+8 medium-sized fix")
    if len(fix.code_files) == 1:
        score += 15
        reasons.append("+15 fix is in one file")
    elif len(fix.code_files) <= 3:
        score += 8
        reasons.append("+8 fix spans a few files")
    return score, reasons