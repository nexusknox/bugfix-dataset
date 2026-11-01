import json
import os
import stat
import subprocess

import pytest
from pathlib import Path

from bugfix import runner
from bugfix.models import Issue, PullRequest, Record
from bugfix.verify import classify, decide, prepare_job, verify_record


# ------------------------------------------------------------- deciding

def test_classify_finds_fail_to_pass_and_broken_tests():
    before = {"t::new": "failed", "t::old": "passed", "t::other": "passed"}
    after = {"t::new": "passed", "t::old": "passed", "t::other": "failed"}
    assert classify(before, after) == (["t::new"], ["t::old"], ["t::other"])


def test_tests_that_could_not_even_import_before_count_as_failing():
    before = {"tests.test_core": "error"}  # collection error: the new function didn't exist
    after = {"tests.test_core::test_new": "passed"}
    assert classify(before, after)[0] == ["tests.test_core::test_new"]


def ran(before, after):
    return {"status": "ran", "before": before, "after": after, "python_version": "3.12.1",
            "log_before": "jane@corp.co.ke failed", "log_after": ""}


def test_verified_when_new_tests_flip_and_nothing_breaks():
    v = decide(ran({"a": "failed", "b": "passed"}, {"a": "passed", "b": "passed"}))
    assert v.status == "verified" and v.fail_to_pass == ["a"] and v.pass_to_pass == ["b"]


def test_logs_are_scrubbed():
    v = decide(ran({"a": "failed"}, {"a": "passed"}))
    assert "jane@corp" not in v.log_before


@pytest.mark.parametrize("before, after, note", [
    ({"a": "passed"}, {"a": "passed"}, "already pass without the fix"),
    ({"a": "failed"}, {"a": "failed"}, "don't pass with the fix either"),
    ({"a": "failed", "b": "passed"}, {"a": "passed", "b": "failed"}, "breaks 1 test"),
    ({}, {}, "no tests ran"),
])
def test_failed_verifications_say_why(before, after, note):
    v = decide(ran(before, after))
    assert v.status == "failed" and note in v.note


def test_setup_problems_are_errors_not_failures():
    v = decide({"status": "error", "note": "the fix doesn't apply to this commit"})
    assert v.status == "error" and "doesn't apply" in v.note


# --------------------------------------------------------------- records

def make_record(**changes) -> Record:
    data = dict(id="acme__widgets-4", repo="acme/widgets", repo_url="https://github.com/acme/widgets", license="MIT",
                issue=Issue(12, "Bug", "x" * 300, "u"), pr=PullRequest(4, "Fix", "Fixes #12", "u"),
                base_commit="abc", fix_commit="def",
                code_patch="diff --git a/w.py b/w.py\n--- a/w.py\n+++ b/w.py\n@@ -1 +1 @@\n-a\n+b\n",
                test_patch="diff --git a/tests/test_w.py b/tests/test_w.py\n--- a/tests/test_w.py\n+++ b/tests/test_w.py\n@@ -1 +1 @@\n-a\n+b\n",
                code_files=["w.py"], test_files=["tests/test_w.py"])
    data.update(changes)
    return Record(**data)


def test_verified_record_gets_the_verification_bonus(tmp_path):
    record = verify_record(make_record(), tmp_path, runner=lambda r, w: ran({"a": "failed"}, {"a": "passed"}))
    assert record.verification.status == "verified"
    assert record.quality.score == 100 and record.quality.reasons[0].startswith("+50")


def test_unverified_record_does_not(tmp_path):
    record = verify_record(make_record(), tmp_path, runner=lambda r, w: ran({"a": "passed"}, {"a": "passed"}))
    assert record.quality.score == 50


def test_job_installs_dependencies_as_of_the_merge_date(tmp_path):
    record = make_record(pr=PullRequest(4, "Fix", "Fixes #12", "u", merged_at="2025-03-01T10:00:00Z"))
    prepare_job(record, tmp_path / "job")
    assert json.loads((tmp_path / "job" / "job.json").read_text())["exclude_newer"] == "2025-03-01T10:00:00Z"
    assert runner.pip_command("2025-03-01T10:00:00Z")[-2:] == ["--exclude-newer", "2025-03-01T10:00:00Z"]
    assert "--exclude-newer" not in runner.pip_command(None)


def test_one_broken_test_file_does_not_stop_the_others():
    assert "--continue-on-collection-errors" in runner.test_command(["tests/a.py"], Path("x.xml"))


def test_job_files_keep_unix_line_endings(tmp_path):
    prepare_job(make_record(), tmp_path / "job")
    assert b"\r\n" not in (tmp_path / "job" / "code.patch").read_bytes()
    job = json.loads((tmp_path / "job" / "job.json").read_text())
    assert job["repo_url"].endswith("widgets.git") and job["test_files"] == ["tests/test_w.py"]


def test_old_records_with_the_previous_field_names_still_load():
    data = make_record().to_dict()
    data["verification"] = {"status": "not_run", "fail_before": [], "pass_after": []}
    assert Record.from_dict(data).verification.fail_to_pass == []


# -------------------------------------------- the container script, for real

def git(cwd, *args) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    done = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, env=env,
                          check=True, capture_output=True, text=True)
    return done.stdout


@pytest.fixture
def buggy_repo(tmp_path):
    """A tiny real git repo: a bug, then a commit that fixes it and adds a test."""
    repo = tmp_path / "upstream"
    (repo / "tests").mkdir(parents=True)
    (repo / "conftest.py").write_text("")
    (repo / "widgets.py").write_text("def f(x):\n    return x\n")
    (repo / "tests" / "test_widgets.py").write_text("import widgets\n\ndef test_number():\n    assert widgets.f(2) == 2\n")
    git(repo, "init", "-q")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")  # GitHub allows fetching any commit
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "widgets.py").write_text("def f(x):\n    return x or 0\n")
    with open(repo / "tests" / "test_widgets.py", "a") as f:
        f.write("\ndef test_none():\n    assert widgets.f(None) == 0\n")
    return repo, base, git(repo, "diff", "--", "widgets.py"), git(repo, "diff", "--", "tests/test_widgets.py")


def write_job(work, repo, base, code_patch, test_patch):
    work.mkdir()
    (work / "job.json").write_text(json.dumps({"repo_url": str(repo), "base_commit": base,
                                               "test_files": ["tests/test_widgets.py"], "install": False}))
    for name, patch in (("code.patch", code_patch), ("test.patch", test_patch)):
        with open(work / name, "w", encoding="utf-8", newline="\n") as f:
            f.write(patch)


def test_runner_proves_a_real_fix(buggy_repo, tmp_path):
    repo, base, code_patch, test_patch = buggy_repo
    write_job(tmp_path / "work", repo, base, code_patch, test_patch)

    result = runner.main(tmp_path / "work")

    assert result["status"] == "ran", result["note"]
    v = decide(result)
    assert v.status == "verified"
    assert v.fail_to_pass == ["tests.test_widgets::test_none"]
    assert v.pass_to_pass == ["tests.test_widgets::test_number"]
    assert json.loads((tmp_path / "work" / "result.json").read_text())["status"] == "ran"


def test_runner_catches_a_fix_that_does_not_fix_anything(buggy_repo, tmp_path):
    repo, base, _, test_patch = buggy_repo
    write_job(tmp_path / "work", repo, base, "", test_patch)  # tests added, but no fix
    assert decide(runner.main(tmp_path / "work")).status == "failed"


def test_runner_reports_a_patch_that_does_not_apply(buggy_repo, tmp_path):
    repo, base, code_patch, test_patch = buggy_repo
    write_job(tmp_path / "work", repo, base, code_patch.replace("return x\n", "return y\n"), test_patch)
    result = runner.main(tmp_path / "work")
    assert result["status"] == "error" and "doesn't apply" in result["note"]


def test_runner_reports_a_missing_commit(buggy_repo, tmp_path):
    repo, _, code_patch, test_patch = buggy_repo
    write_job(tmp_path / "work", repo, "0" * 40, code_patch, test_patch)
    result = runner.main(tmp_path / "work")
    assert result["status"] == "error" and "couldn't get the code" in result["note"]


def test_extras_are_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project.optional-dependencies]\ntesting = ["x"]\n'
                                             '[dependency-groups]\ntests = ["y"]\n')
    assert runner.extras_available(tmp_path) == {"testing", "group:tests"}


def test_runner_still_verifies_when_another_test_file_cannot_load(buggy_repo, tmp_path):
    repo, base, code_patch, test_patch = buggy_repo
    broken = ("diff --git a/tests/test_broken.py b/tests/test_broken.py\nnew file mode 100644\n"
              "--- /dev/null\n+++ b/tests/test_broken.py\n@@ -0,0 +1 @@\n+from _pytest.nowhere import gone\n")
    write_job(tmp_path / "work", repo, base, code_patch, test_patch + broken)
    job = json.loads((tmp_path / "work" / "job.json").read_text())
    job["test_files"].append("tests/test_broken.py")
    (tmp_path / "work" / "job.json").write_text(json.dumps(job))

    v = decide(runner.main(tmp_path / "work"))

    assert v.status == "verified"
    assert v.fail_to_pass == ["tests.test_widgets::test_none"]


def test_no_tests_note_shows_the_real_error():
    result = ran({}, {})
    result["log_after"] = "collecting...\nE   ModuleNotFoundError: No module named 'blinker'\n1 error"
    v = decide(result)
    assert "No module named 'blinker'" in v.note


def test_leftover_work_folder_with_read_only_files_is_replaced(tmp_path):
    work = tmp_path / "rec"
    (work / "repo" / ".git" / "objects").mkdir(parents=True)
    locked = work / "repo" / ".git" / "objects" / "pack"
    locked.write_text("x")
    os.chmod(locked, stat.S_IREAD)  # like git's object files on Windows
    prepare_job(make_record(), work)
    assert (work / "job.json").exists() and not (work / "repo").exists()


def test_unexpected_problem_marks_one_record_as_error(tmp_path):
    def crashing_runner(record, work):
        raise RuntimeError("disk full")
    record = verify_record(make_record(), tmp_path, runner=crashing_runner)
    assert record.verification.status == "error" and "disk full" in record.verification.note


# ------------------------------------------------------- the right Python

@pytest.mark.parametrize("merged_at, version", [
    ("2025-06-01T00:00:00Z", "3.12"),
    ("2024-03-01T00:00:00Z", "3.11"),   # 3.12 was only 5 months old
    ("2023-03-01T00:00:00Z", "3.10"),   # 3.11 only 5 months old
    ("2023-04-12T00:00:00Z", "3.11"),   # flask-5063's era: 3.11, not 3.12
    ("2018-01-01T00:00:00Z", "3.8"),    # older than every image: oldest we have
    (None, "3.12"),
    ("", "3.12"),
])
def test_python_version_is_chosen_by_merge_date(merged_at, version):
    from bugfix.verify import python_for_date
    assert python_for_date(merged_at) == version


def test_images_are_tagged_by_python_version():
    from bugfix.verify import image_for
    assert image_for("3.11") == "bugfix-runner:py3.11"


# ----------------------------------------------- installing older projects

def test_pyproject_extras_found_without_tomllib(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\n'
        'testing = ["pytest"]\n"docs" = ["sphinx"]\n\n[dependency-groups]\ntests = ["y"]\n')
    assert runner.extras_available(tmp_path) == {"testing", "docs", "group:tests"}


def test_example_subprojects_are_found(tmp_path):
    example = tmp_path / "examples" / "javascript"
    (example / "tests").mkdir(parents=True)
    (example / "pyproject.toml").write_text("[project]\nname='js_example'\n")
    found = runner.subprojects(tmp_path, ["examples/javascript/tests/test_js.py", "tests/test_app.py"])
    assert found == [example]


def test_install_retries_once_on_a_network_error(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, cwd, timeout):
        calls.append(cmd)
        return (1, "Request failed after 3 retries") if len(calls) == 1 else (0, "ok")

    monkeypatch.setattr(runner, "run", fake_run)
    assert runner.run_install(["uv", "pip", "install", "x"], tmp_path) == (0, "ok")
    assert len(calls) == 2


def test_install_does_not_retry_real_errors(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "run", lambda cmd, cwd, timeout: calls.append(cmd) or (1, "No solution found"))
    runner.run_install(["uv"], tmp_path)
    assert len(calls) == 1
