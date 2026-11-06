"""Turning verified records into a release someone else can use.

A release folder holds:
  dataset.jsonl     one verified record per line (without the long logs)
  test_logs.jsonl   the before/after pytest output for each record
  swebench.jsonl    the same records in SWE-bench's field names
  stats.json        counts, for dashboards and the card
  DATASET_CARD.md   what's in it, how it was made, licences, limits
  sample.jsonl      the first few records, small enough to commit to git
"""

SAMPLE_SIZE = 3

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bugfix.models import Record


def select(records: list[Record], verified_only: bool = True, min_score: int = 0) -> list[Record]:
    chosen = [r for r in records
              if (not verified_only or r.verification.status == "verified") and r.quality.score >= min_score]
    return sorted(chosen, key=lambda r: (r.repo.lower(), r.pr.number))


def without_logs(record: Record) -> dict:
    data = record.to_dict()
    data["verification"].pop("log_before", None)
    data["verification"].pop("log_after", None)
    return data


def swebench_row(record: Record) -> dict:
    """SWE-bench's field names, so existing evaluation tools can read it."""
    return {
        "instance_id": record.id,
        "repo": record.repo,
        "base_commit": record.base_commit,
        "patch": record.code_patch,
        "test_patch": record.test_patch,
        "problem_statement": f"{record.issue.title}\n\n{record.issue.body}".strip(),
        "hints_text": "",
        "created_at": record.issue.created_at,
        "version": record.verification.python_version,
        "FAIL_TO_PASS": json.dumps(record.verification.fail_to_pass),
        # also_fixed tests pass with the fix too, so evaluators should still check them
        "PASS_TO_PASS": json.dumps(sorted(record.verification.pass_to_pass + record.verification.also_fixed)),
        "license": record.license,
    }


def stats(all_records: list[Record], released: list[Record]) -> dict:
    return {
        "candidates": len(all_records),
        "released": len(released),
        "verification": dict(Counter(r.verification.status for r in all_records).most_common()),
        "by_repo": dict(Counter(r.repo for r in released).most_common()),
        "by_license": dict(Counter(r.license for r in released).most_common()),
        "by_python": dict(Counter(r.verification.python_version.rsplit(".", 1)[0] for r in released).most_common()),
        "average_quality": round(sum(r.quality.score for r in released) / len(released), 1) if released else 0,
        "fail_to_pass_tests": sum(len(r.verification.fail_to_pass) for r in released),
        "also_fixed_tests": sum(len(r.verification.also_fixed) for r in released),
        "records_keeping_all_flipped": sum(1 for r in released if r.verification.note),
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def export(all_records: list[Record], out: Path, verified_only: bool = True, min_score: int = 0) -> dict:
    released = select(all_records, verified_only, min_score)
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "dataset.jsonl", [without_logs(r) for r in released])
    write_jsonl(out / "test_logs.jsonl", [{"id": r.id, "log_before": r.verification.log_before,
                                           "log_after": r.verification.log_after} for r in released])
    write_jsonl(out / "swebench.jsonl", [swebench_row(r) for r in released])
    write_jsonl(out / "sample.jsonl", [without_logs(r) for r in released[:SAMPLE_SIZE]])
    numbers = stats(all_records, released)
    (out / "stats.json").write_text(json.dumps(numbers, indent=2), encoding="utf-8")
    (out / "DATASET_CARD.md").write_text(dataset_card(released, numbers, verified_only, min_score), encoding="utf-8")
    return numbers


def table(counts: dict, first: str, second: str = "Records") -> str:
    rows = [f"| {first} | {second} |", "|---|---:|"]
    rows += [f"| {key} | {value} |" for key, value in counts.items()]
    return "\n".join(rows)


def dataset_card(released: list[Record], numbers: dict, verified_only: bool, min_score: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repos = sorted({(r.repo, r.repo_url, r.license) for r in released})
    attribution = "\n".join(f"| [{name}]({url}) | {license} |" for name, url, license in repos)
    selection = "verified records only" if verified_only else "all candidates (verified or not)"
    return f"""---
license: other
task_categories: [text-generation]
tags: [code, bug-fix, software-engineering, swe-bench]
pretty_name: Verified Python bug fixes
---

# Verified Python bug fixes

Real bugs from open-source Python projects: the issue that reported each one,
the pull request that fixed it, the fix split into a code patch and a test
patch, and **evidence from running the tests**: the new tests fail without
the fix and pass with it.

Generated {today} by [bugfix-dataset](https://github.com/nexusknox/bugfix-dataset).

## At a glance

- **{numbers['released']} records** released ({selection}, quality score ≥ {min_score})
- **{numbers['fail_to_pass_tests']} target tests**: tests the fix's pull request added or
  edited, that fail before the fix and pass after it
- {numbers['also_fixed_tests']} more tests also start passing with the fix (usually because
  their file couldn't load before it); they're listed separately as `also_fixed`
- Average quality score: **{numbers['average_quality']}/100**
- Built from {numbers['candidates']} candidates; verification results:
  {", ".join(f"{k} {v}" for k, v in numbers['verification'].items())}

{table(numbers['by_repo'], 'Repository')}

## How it was made

1. **Mined** merged pull requests from permissively licensed Python repositories
   (MIT, Apache-2.0, BSD, ISC and similar) through the GitHub API. A PR is kept
   only if GitHub links it to an issue it closes, it changes Python source and
   tests, it changes no other language's source, and it's a focused fix
   (at most 300 changed lines and 5 files).
2. **Split** each fix into a code patch and a test patch.
3. **Verified** each record in a fresh Docker container: the repository at the
   commit before the fix, with dependencies installed **as they were on the
   day the fix was merged** (`uv --exclude-newer`), on the Python version
   current at that time. The test patch is applied alone (the new tests must
   fail), then the fix (they must pass, and no previously passing test may break).
4. **Cleaned**: emails and secrets removed from issue text, PR text and logs;
   records whose patches contain secrets dropped; duplicate fixes (e.g.
   backports) removed by fingerprint.

## Files

| File | Contents |
|---|---|
| `dataset.jsonl` | One record per line: issue, PR, commits, `code_patch`, `test_patch`, `fail_to_pass`, `also_fixed`, `pass_to_pass`, quality score with reasons |
| `test_logs.jsonl` | pytest output before and after the fix, per record |
| `swebench.jsonl` | The same records with SWE-bench field names (`instance_id`, `patch`, `test_patch`, `problem_statement`, `FAIL_TO_PASS`, …); `also_fixed` tests are added to `PASS_TO_PASS` so evaluators still check them |
| `stats.json` | The numbers on this card |

## Quality score

Each record has a 0-100 score with its reasons: +50 verified, up to +20 for a
detailed issue description, up to +15 for a small fix, up to +15 for a fix in
one file.

## Limitations

- Python projects tested with pytest only.
- Test IDs use pytest's JUnit naming (`tests.test_app::test_name`), not file
  paths (`tests/test_app.py::test_name`).
- `fail_to_pass` holds the tests the pull request added or edited, found by
  reading the test patch. If a patch only changes fixtures or helpers, no
  target can be found and every flipped test is kept, with a note
  ({numbers['records_keeping_all_flipped']} record(s) here).
- Very old commits (before ~2019) often can't be installed any more and are
  left out, which skews the data towards recent code.
- Fixes for bugs that only appear on a newer Python than the one chosen for
  their date can show as "tests already pass" and are left out.
- Issue text is written by people and may be informal, incomplete or wrong.
- Verification shows the new tests pass; it does not prove the fix is the
  best possible one.

## Licences and attribution

Every record keeps the licence of the repository it came from, in its
`license` field. Code in `code_patch` and `test_patch` remains under that
licence and copyright; reuse must follow it (for example, MIT and BSD require
keeping the copyright notice, and Apache-2.0 also requires noting changes).

| Repository | Licence |
|---|---|
{attribution}

## Personal information

Email addresses and credentials are removed from issue text, PR text, commit
messages and logs. GitHub usernames may remain in issue and PR text.
"""
