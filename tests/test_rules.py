from bugfix.models import Issue, PullRequest, Record, record_id
from bugfix.rules import (
    closing_issue_numbers, find_secrets, fingerprint, is_test_file, license_allowed,
    quality_score, rejection_reasons, scrub_text, split_diff, split_fix,
)

REPO = "acme/widgets"


def file_diff(path: str, added: list[str], removed: list[str] = ()) -> str:
    lines = [f"diff --git a/{path} b/{path}", "index 111..222 100644", f"--- a/{path}", f"+++ b/{path}",
             f"@@ -1,{len(removed) + 1} +1,{len(added) + 1} @@", " context"]
    lines += [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


FIX = (
    file_diff("widgets/core.py", ["    return x or 0"], ["    return x"])
    + file_diff("tests/test_core.py", ["def test_none():", "    assert f(None) == 0"])
    + file_diff("CHANGELOG.md", ["- Fixed None handling"])
)


# ------------------------------------------------------------------ licences

def test_permissive_licences_allowed():
    assert license_allowed("MIT") and license_allowed("Apache-2.0") and license_allowed("BSD-3-Clause")


def test_copyleft_unclear_and_missing_licences_excluded():
    for spdx in ("GPL-3.0", "AGPL-3.0", "LGPL-2.1", "NOASSERTION", None, ""):
        assert not license_allowed(spdx)


# ------------------------------------------------------------- issue links

def test_closing_keywords_find_issues():
    text = "Fixes #12. Also closes acme/widgets#40 and Resolves https://github.com/acme/widgets/issues/7"
    assert closing_issue_numbers(text, REPO) == [12, 40, 7]


def test_keywords_are_case_insensitive_and_allow_a_colon():
    assert closing_issue_numbers("FIXED: #5", REPO) == [5]


def test_plain_mentions_do_not_count():
    assert closing_issue_numbers("Related to #12, see #13", REPO) == []


def test_other_repos_issues_do_not_count():
    assert closing_issue_numbers("Fixes other/repo#12 and https://github.com/other/repo/issues/3", REPO) == []


def test_no_duplicates_and_empty_text():
    assert closing_issue_numbers("fixes #3, fixes #3", REPO) == [3]
    assert closing_issue_numbers("", REPO) == []


# ------------------------------------------------------------ splitting fixes

def test_split_diff_counts_lines_per_file():
    files = split_diff(FIX)
    assert [f.path for f in files] == ["widgets/core.py", "tests/test_core.py", "CHANGELOG.md"]
    assert (files[0].added, files[0].removed) == (1, 1)


def test_test_files_are_recognised():
    for path in ("tests/test_core.py", "pkg/tests/helpers.py", "test_x.py", "pkg/x_test.py", "conftest.py"):
        assert is_test_file(path), path
    for path in ("widgets/core.py", "widgets/testing_utils.py", "contest.py"):
        assert not is_test_file(path), path


def test_fix_is_split_into_code_and_tests_ignoring_docs():
    fix = split_fix(FIX)
    assert fix.code_files == ["widgets/core.py"]
    assert fix.test_files == ["tests/test_core.py"]
    assert fix.other_files == []
    assert "return x or 0" in fix.code_patch and "test_none" not in fix.code_patch
    assert "test_none" in fix.test_patch
    assert fix.changed_lines == 2


def test_patches_keep_their_git_headers_so_they_still_apply():
    fix = split_fix(FIX)
    assert fix.code_patch.startswith("diff --git a/widgets/core.py")
    assert fix.test_patch.startswith("diff --git a/tests/test_core.py")


# ---------------------------------------------------------------- eligibility

def test_good_fix_is_eligible():
    assert rejection_reasons(split_fix(FIX), [12]) == []


def test_every_reason_is_reported():
    fix = split_fix(file_diff("src/native.c", ["int x;"]))
    reasons = rejection_reasons(fix, [])
    assert "no linked issue" in reasons
    assert "no Python source changes" in reasons
    assert any("can't be verified" in r for r in reasons)
    assert any("another language" in r for r in reasons)


def test_huge_fix_is_rejected():
    fix = split_fix(file_diff("a.py", [f"x{i} = {i}" for i in range(400)]) + file_diff("tests/test_a.py", ["pass"]))
    assert any("too large" in r for r in rejection_reasons(fix, [1]))


# ------------------------------------------------------- privacy and secrets

def test_secrets_are_detected():
    text = "token = 'ghp_" + "a" * 36 + "'\nkey = AKIAABCDEFGHIJKLMNOP"
    assert set(find_secrets(text)) == {"GitHub token", "AWS access key"}
    assert find_secrets("nothing to see") == []


def test_prose_is_scrubbed_but_safe_emails_kept():
    text = "Reported by jane.doe@company.co.ke, see bot@users.noreply.github.com and a@example.com"
    out = scrub_text(text)
    assert "jane.doe" not in out and "[email]" in out
    assert "bot@users.noreply.github.com" in out and "a@example.com" in out


def test_secrets_in_prose_are_removed():
    assert "ghp_" not in scrub_text("my token ghp_" + "b" * 36)


# --------------------------------------------------- duplicates and quality

def test_cherry_picked_fix_has_the_same_fingerprint():
    original = file_diff("a.py", ["return x or 0"], ["return x"])
    backport = original.replace("@@ -1,2 +1,2 @@", "@@ -88,2 +88,2 @@").replace("return x or 0", "return  x  or 0")
    assert fingerprint(original) == fingerprint(backport)


def test_different_fixes_have_different_fingerprints():
    assert fingerprint(file_diff("a.py", ["return 1"])) != fingerprint(file_diff("a.py", ["return 2"]))


def test_quality_score_explains_itself():
    score, reasons = quality_score("x" * 300, split_fix(FIX), verified=True)
    assert score == 100
    assert len(reasons) == 4 and reasons[0].startswith("+50 verified")


def test_unverified_vague_fix_scores_low():
    score, _ = quality_score("", split_fix(FIX), verified=False)
    assert score == 30  # small fix + one file only


# -------------------------------------------------------------------- schema

def test_record_round_trips_through_a_dict():
    record = Record(
        id=record_id("acme/widgets", 7), repo="acme/widgets", repo_url="https://github.com/acme/widgets",
        license="MIT", issue=Issue(1, "Bug", "Crash", "u"), pr=PullRequest(7, "Fix", "Fixes #1", "u"),
        base_commit="abc", fix_commit="def", code_patch="", test_patch="", code_files=[], test_files=[],
    )
    assert record.id == "acme__widgets-7"
    assert Record.from_dict(record.to_dict()) == record


def test_project_info_files_are_ignored_not_rejected():
    fix = split_fix(FIX + file_diff("AUTHORS", ["Jane Doe"]) + file_diff("requirements/tests.in", ["pytest"])
                    + file_diff("changelog/1234.bugfix.rst", ["Fixed it."]) + file_diff("LICENSE", ["x"]))
    assert fix.other_files == []
    assert fix.code_files == ["widgets/core.py"]
    assert rejection_reasons(fix, [1]) == []


def test_templates_and_data_files_stay_in_the_code_patch():
    fix = split_fix(file_diff("flask/templates/error.html", ["<p>{{ e }}</p>"]) + FIX)
    assert fix.code_files == ["widgets/core.py", "flask/templates/error.html"]  # Python first
    assert "error.html" in fix.code_patch
    assert fix.changed_lines == 2  # only Python lines count towards "too large"


def test_other_languages_are_rejected():
    fix = split_fix(FIX + file_diff("widgets/_speedups.pyx", ["cdef int x"]))
    assert fix.other_files == ["widgets/_speedups.pyx"]
    assert any("another language" in r for r in rejection_reasons(fix, [1]))


def test_template_only_change_has_no_python_source():
    fix = split_fix(file_diff("app/templates/a.html", ["x"]) + file_diff("tests/test_a.py", ["pass"]))
    assert "no Python source changes" in rejection_reasons(fix, [1])