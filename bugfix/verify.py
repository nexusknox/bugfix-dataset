"""Verifying records: do the new tests fail without the fix and pass with it?

The project's code only ever runs inside a Docker container (see runner.py),
with limited memory and CPU, so a repo can't touch your PC.
"""

import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Callable

from bugfix.models import Quality, Record, Verification
from bugfix.rules import quality_score, scrub_text, split_fix
from bugfix.targets import narrow

IMAGE = "bugfix-runner"
DOCKER_TIMEOUT = 45 * 60  # install + two test runs

# Python release dates. A fix is checked on the newest Python that had been
# out for 6+ months when it was merged: what the project was tested on then.
PYTHON_RELEASES = [("3.8", "2019-10-14"), ("3.9", "2020-10-05"), ("3.10", "2021-10-04"),
                   ("3.11", "2022-10-24"), ("3.12", "2023-10-02")]
SETTLE_MONTHS = 6


def python_for_date(merged_at: str | None) -> str:
    """'2023-04-12T…' -> '3.11'. Unknown dates get the newest version."""
    if not merged_at:
        return PYTHON_RELEASES[-1][0]
    year, month = int(merged_at[:4]), int(merged_at[5:7])
    chosen = PYTHON_RELEASES[0][0]
    for version, released in PYTHON_RELEASES:
        r_year, r_month = int(released[:4]), int(released[5:7])
        if (year - r_year) * 12 + (month - r_month) >= SETTLE_MONTHS:
            chosen = version
    return chosen


def image_for(version: str) -> str:
    return f"{IMAGE}:py{version}"


class DockerError(Exception):
    pass


# ---------------------------------------------------------------- decision

def classify(before: dict[str, str], after: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(fail_to_pass, pass_to_pass, broken), from per-test outcomes.

    fail_to_pass: didn't pass without the fix (failed, error, or didn't exist
                  yet because the file couldn't even import), passes with it.
    broken:       passed without the fix, fails with it.
    """
    fail_to_pass = sorted(t for t, o in after.items() if o == "passed" and before.get(t) != "passed")
    pass_to_pass = sorted(t for t, o in after.items() if o == "passed" and before.get(t) == "passed")
    broken = sorted(t for t, o in before.items() if o == "passed" and after.get(t) in ("failed", "error"))
    return fail_to_pass, pass_to_pass, broken


def last_error_line(log: str) -> str:
    """The most useful line of a pytest log: the last error, else the last line."""
    lines = [line.strip() for line in log.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith(("E ", "ERROR", "ImportError", "ModuleNotFoundError")) or "Error:" in line:
            return line[:200]
    return lines[-1][:200] if lines else "pytest printed nothing"


def decide(result: dict, test_patch: str = "") -> Verification:
    """Turn the container's result into a Verification.

    Of the tests that flipped, fail_to_pass keeps the ones the test patch adds
    or edits; the rest go to also_fixed (see targets.py).
    """
    v = Verification(python_version=result.get("python_version", ""),
                     log_before=scrub_text(result.get("log_before", "")),
                     log_after=scrub_text(result.get("log_after", "")))
    if result.get("status") != "ran":
        v.status, v.note = "error", result.get("note") or "the run didn't finish"
        return v
    flipped, v.pass_to_pass, broken = classify(result["before"], result["after"])
    v.fail_to_pass, v.also_fixed, narrow_note = narrow(flipped, test_patch)
    if broken:
        v.status, v.note = "failed", f"the fix breaks {len(broken)} test(s): {', '.join(broken[:3])}"
    elif not v.fail_to_pass:
        if not result["after"]:
            v.status, v.note = "failed", "no tests ran: " + last_error_line(result.get("log_after", ""))
        elif all(o == "passed" for o in result["before"].values()) and result["before"]:
            v.status, v.note = "failed", "the new tests already pass without the fix"
        else:
            v.status, v.note = "failed", "the new tests don't pass with the fix either"
    else:
        v.status, v.note = "verified", narrow_note
    return v


def refine(record: Record) -> bool:
    """Re-narrow a verified record's tests without re-running Docker.

    Every flipped test is already stored in fail_to_pass + also_fixed, so the
    narrowed list can be worked out again from the test patch. True if it changed.
    """
    v = record.verification
    if v.status != "verified":
        return False
    flipped = sorted(set(v.fail_to_pass) | set(v.also_fixed))
    fail_to_pass, also_fixed, note = narrow(flipped, record.test_patch)
    changed = (fail_to_pass, also_fixed, note) != (v.fail_to_pass, v.also_fixed, v.note)
    v.fail_to_pass, v.also_fixed, v.note = fail_to_pass, also_fixed, note
    return changed


# ------------------------------------------------------------------ docker

def remove_tree(path: Path) -> None:
    """Delete a folder, including git's read-only files.

    On Windows, git marks its object files read-only, so a plain
    shutil.rmtree fails on them. Make each one writable and try again.
    """
    def make_writable_and_retry(function, failed_path, _error):
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable_and_retry)


def prepare_job(record: Record, work: Path, install: bool = True) -> None:
    """Write everything the container needs into its work folder."""
    remove_tree(work)
    work.mkdir(parents=True)
    job = {"repo_url": record.repo_url + ".git", "base_commit": record.base_commit,
           "test_files": record.test_files, "install": install,
           # dependencies as they were when the fix was merged
           "exclude_newer": record.pr.merged_at or None}
    (work / "job.json").write_text(json.dumps(job), encoding="utf-8")
    # newline="\n": on Windows, writing "\r\n" would stop the patches applying
    for name, patch in (("code.patch", record.code_patch), ("test.patch", record.test_patch)):
        with open(work / name, "w", encoding="utf-8", newline="\n") as f:
            f.write(patch)


def image_exists(image: str) -> bool:
    done = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return done.returncode == 0


def build_image(version: str, quiet: bool = False) -> bool:
    cmd = ["docker", "build", "-t", image_for(version), "--build-arg", f"PYTHON_VERSION={version}",
           "-f", "docker/Dockerfile", "."]
    done = subprocess.run(cmd, capture_output=quiet)
    return done.returncode == 0


def docker_runner(record: Record, work: Path, timeout: int = DOCKER_TIMEOUT) -> dict:
    """Run one record in a fresh container, on the Python of its time."""
    version = python_for_date(record.pr.merged_at)
    image = image_for(version)
    if not image_exists(image):
        print(f"(building the Python {version} image, one time only…) ", end="", flush=True)
        if not build_image(version, quiet=True):
            return {"status": "error", "note": f"couldn't build the Python {version} image"}
    prepare_job(record, work)
    name = f"bugfix-{record.id}".replace("/", "-")[:60]
    cmd = ["docker", "run", "--rm", "--name", name, "--memory", "4g", "--cpus", "2",
           "-v", f"{work.resolve()}:/work", image]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DockerError("Docker isn't installed, or isn't on PATH.")
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return {"status": "timeout", "note": f"took longer than {timeout // 60} minutes"}
    result_file = work / "result.json"
    if not result_file.exists():
        return {"status": "error", "note": "the container stopped without a result (is Docker running?)"}
    return json.loads(result_file.read_text(encoding="utf-8"))


def docker_ready() -> str | None:
    """None if Docker is running, else a friendly message."""
    try:
        done = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "Docker isn't installed, or isn't on PATH."
    except subprocess.TimeoutExpired:
        return "Docker didn't answer. Is Docker Desktop still starting?"
    return None if done.returncode == 0 else "Docker isn't running. Open Docker Desktop and wait for 'Engine running'."


# ------------------------------------------------------------------ record

def verify_record(record: Record, work_root: Path, runner: Callable[[Record, Path], dict] = docker_runner) -> Record:
    """Verify one record, and update its verification and quality score."""
    try:
        result = runner(record, work_root / record.id)
    except DockerError:
        raise  # Docker itself is missing: stop the whole batch
    except Exception as error:  # one bad record must not stop a batch
        logging.exception("Verifying %s failed", record.id)
        result = {"status": "error", "note": f"unexpected problem: {error}"}
    record.verification = decide(result, record.test_patch)
    fix = split_fix(record.code_patch + record.test_patch)
    score, reasons = quality_score(record.issue.body, fix, verified=record.verification.status == "verified")
    record.quality = Quality(score, reasons)
    try:
        remove_tree(work_root / record.id)  # repos are big: tidy up
    except OSError:
        logging.warning("Couldn't delete %s; it will be replaced next time", work_root / record.id)
    return record
