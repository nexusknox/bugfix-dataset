import json

from bugfix import browse
from bugfix.export import export, select, swebench_row
from bugfix.models import Issue, PullRequest, Quality, Record, Verification


def record(number, repo="acme/widgets", status="verified", score=90, license="MIT"):
    return Record(
        id=f"{repo.replace('/', '__')}-{number}", repo=repo, repo_url=f"https://github.com/{repo}", license=license,
        issue=Issue(number, f"Crash number {number}", "It crashes when x is None", "u", created_at="2025-01-01T00:00:00Z"),
        pr=PullRequest(number + 100, "Fix", "Fixes", "u", merged_at="2025-02-01T00:00:00Z"),
        base_commit="abc", fix_commit="def", code_patch="diff --git a/a.py b/a.py\n", test_patch="diff --git a/tests/t.py b/tests/t.py\n",
        code_files=["a.py"], test_files=["tests/t.py"],
        verification=Verification(status=status, fail_to_pass=["tests.t::test_new"], pass_to_pass=["tests.t::test_old"],
                                  log_before="1 failed", log_after="2 passed", python_version="3.12.16"),
        quality=Quality(score, ["+50 verified"]),
    )


RECORDS = [record(2), record(1), record(3, status="failed", score=40),
           record(4, repo="pallets/flask", license="BSD-3-Clause", score=60)]


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_only_verified_records_are_released_by_default_in_a_stable_order():
    assert [r.id for r in select(RECORDS)] == ["acme__widgets-1", "acme__widgets-2", "pallets__flask-4"]


def test_minimum_score_and_unverified_option():
    assert [r.id for r in select(RECORDS, min_score=70)] == ["acme__widgets-1", "acme__widgets-2"]
    assert len(select(RECORDS, verified_only=False)) == 4


def test_export_writes_every_file(tmp_path):
    numbers = export(RECORDS, tmp_path / "release")
    files = sorted(p.name for p in (tmp_path / "release").iterdir())
    assert files == ["DATASET_CARD.md", "dataset.jsonl", "sample.jsonl", "stats.json", "swebench.jsonl", "test_logs.jsonl"]
    assert len(read_jsonl(tmp_path / "release" / "sample.jsonl")) == 3
    assert numbers["released"] == 3 and numbers["candidates"] == 4
    assert numbers["by_license"] == {"MIT": 2, "BSD-3-Clause": 1}
    assert numbers["by_python"] == {"3.12": 3}
    assert numbers["fail_to_pass_tests"] == 3


def test_logs_live_in_their_own_file(tmp_path):
    export(RECORDS, tmp_path)
    data = read_jsonl(tmp_path / "dataset.jsonl")
    assert "log_before" not in data[0]["verification"]
    logs = read_jsonl(tmp_path / "test_logs.jsonl")
    assert logs[0] == {"id": "acme__widgets-1", "log_before": "1 failed", "log_after": "2 passed"}


def test_swebench_format():
    row = swebench_row(record(7))
    assert row["instance_id"] == "acme__widgets-7"
    assert row["problem_statement"].startswith("Crash number 7\n\n")
    assert json.loads(row["FAIL_TO_PASS"]) == ["tests.t::test_new"]
    assert row["patch"].startswith("diff --git a/a.py")


def test_dataset_card_has_numbers_and_attribution(tmp_path):
    export(RECORDS, tmp_path)
    card = (tmp_path / "DATASET_CARD.md").read_text(encoding="utf-8")
    assert "**3 records** released" in card
    assert "| [acme/widgets](https://github.com/acme/widgets) | MIT |" in card
    assert "| [pallets/flask](https://github.com/pallets/flask) | BSD-3-Clause |" in card
    assert "## Limitations" in card


def test_empty_release_does_not_crash(tmp_path):
    numbers = export([record(1, status="failed")], tmp_path)
    assert numbers["released"] == 0 and numbers["average_quality"] == 0


# ------------------------------------------------------------- the viewer

def test_viewer_filters():
    assert len(browse.filter_records(RECORDS, statuses=["verified"])) == 3
    assert [r.id for r in browse.filter_records(RECORDS, repos=["pallets/flask"])] == ["pallets__flask-4"]
    assert len(browse.filter_records(RECORDS, min_score=80)) == 2
    assert [r.id for r in browse.filter_records(RECORDS, search="NUMBER 3")] == ["acme__widgets-3"]
    assert len(browse.filter_records(RECORDS, search="crash none")) == 4  # every word must match


def test_viewer_summary_counts_the_rate_over_tried_records_only():
    numbers = browse.summary(RECORDS + [record(9, status="not_run", score=0)])
    assert numbers["verified"] == 3 and numbers["verified_rate"] == 75 and numbers["repos"] == 2
