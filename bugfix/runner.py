"""Runs INSIDE the Docker container, for one record. Standard library only.

Reads /work/job.json and the two patches, then:
  1. fetches the repo at the commit before the fix
  2. installs it with its test dependencies, as they were when the fix was merged
  3. adds the new tests only, and runs them   -> they should FAIL
  4. adds the fix too, and runs them again    -> they should PASS
and writes /work/result.json. The host never runs the project's code itself:
that only ever happens in here, isolated from the PC.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LOG_TAIL = 150  # lines of output kept per test run


class StepFailed(Exception):
    pass


def run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout} s: {' '.join(cmd)}"


def tail(text: str, lines: int = LOG_TAIL) -> str:
    return "\n".join(text.splitlines()[-lines:])


def checkout(repo_url: str, commit: str, repo: Path) -> None:
    """Fetch just the one commit we need (fast, small download)."""
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "fetch", "-q", "--depth", "1", repo_url, commit],
                ["git", "checkout", "-q", "FETCH_HEAD"]):
        code, out = run(cmd, repo, 600)
        if code != 0:
            raise StepFailed(f"couldn't get the code at {commit[:10]}: {tail(out, 5)}")


def extras_available(repo: Path) -> set[str]:
    """Names of optional dependency groups the project declares."""
    names: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            names |= set(data.get("project", {}).get("optional-dependencies", {}))
            names |= {f"group:{g}" for g in data.get("dependency-groups", {})}
        except Exception:
            pass
    for name in ("setup.cfg", "setup.py"):
        path = repo / name
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for extra in ("testing", "tests", "test", "dev"):
                if f"{extra} =" in text or f'"{extra}"' in text or f"'{extra}'" in text:
                    names.add(extra)
    return names


def pip_command(date: str | None) -> list[str]:
    """uv installs packages fast; --exclude-newer ignores anything released
    after `date`, so a fix from March 2025 gets the libraries of March 2025.
    Without that, today's versions break old code and old tests."""
    cmd = ["uv", "pip", "install", "--system", "--quiet"]
    if date:
        cmd += ["--exclude-newer", date]
    return cmd


def install(repo: Path, date: str | None = None) -> str:
    """Install the project, its test dependencies and pytest, as of `date`."""
    pip = pip_command(date)
    log = []
    for req in ("requirements/tests.txt", "requirements/test.txt", "requirements-dev.txt",
                "requirements/dev.txt", "test-requirements.txt", "requirements-test.txt"):
        if (repo / req).exists():
            code, out = run(pip + ["-r", req], repo, 900)
            log.append(out)
            if code == 0:
                break

    available = extras_available(repo)
    for group in ("tests", "test", "testing", "dev"):
        if f"group:{group}" in available:
            code, out = run(pip + ["--group", group], repo, 900)
            log.append(out)
            break

    extra = next((e for e in ("testing", "tests", "test", "dev") if e in available), None)
    target = f".[{extra}]" if extra else "."
    code, out = run(pip + ["-e", target], repo, 900)
    log.append(out)
    if code != 0:
        raise StepFailed("couldn't install the project: " + tail(out, 8))
    if run([sys.executable, "-c", "import pytest"], repo, 60)[0] != 0:
        code, out = run(pip + ["pytest"], repo, 600)  # pytest from the same date
        log.append(out)
    return "\n".join(log)


def apply_patch(repo: Path, patch: Path, what: str) -> None:
    if not patch.read_text(encoding="utf-8").strip():
        return
    code, out = run(["git", "apply", "--whitespace=nowarn", str(patch)], repo, 120)
    if code != 0:
        raise StepFailed(f"the {what} doesn't apply to this commit: {tail(out, 5)}")


def parse_junit(xml_path: Path) -> dict[str, str]:
    """{test id: passed / failed / error / skipped} from pytest's JUnit XML."""
    if not xml_path.exists():
        return {}
    outcomes = {}
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        outcomes[test_id] = outcome
    return outcomes


def test_command(test_files: list[str], xml_path: Path) -> list[str]:
    """--continue-on-collection-errors: one test file that can't load must not
    stop every other test from running."""
    return [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-o", "addopts=",
            "--continue-on-collection-errors", f"--junitxml={xml_path}", *test_files]


def run_tests(repo: Path, test_files: list[str], xml_path: Path, timeout: int) -> tuple[dict, str]:
    existing = [f for f in test_files if (repo / f).exists()]
    if not existing:
        return {}, "no test files to run"
    xml_path.unlink(missing_ok=True)
    code, out = run(test_command(existing, xml_path), repo, timeout)
    return parse_junit(xml_path), tail(out)


def main(work: Path) -> dict:
    job = json.loads((work / "job.json").read_text(encoding="utf-8"))
    repo = work / "repo"
    result = {"status": "error", "note": "", "before": {}, "after": {}, "log_before": "", "log_after": "",
              "python_version": sys.version.split()[0]}
    try:
        checkout(job["repo_url"], job["base_commit"], repo)
        if job.get("install", True):
            install(repo, job.get("exclude_newer"))
        apply_patch(repo, work / "test.patch", "test patch")
        result["before"], result["log_before"] = run_tests(repo, job["test_files"], work / "before.xml", job.get("test_timeout", 600))
        apply_patch(repo, work / "code.patch", "fix")
        result["after"], result["log_after"] = run_tests(repo, job["test_files"], work / "after.xml", job.get("test_timeout", 600))
        result["status"] = "ran"
    except StepFailed as error:
        result["note"] = str(error)
    except Exception as error:  # never leave without writing a result
        result["note"] = f"unexpected problem: {error!r}"
    (work / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return result


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/work"))
