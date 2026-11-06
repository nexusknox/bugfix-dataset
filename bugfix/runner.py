"""Runs INSIDE the Docker container, for one record. Standard library only.

Reads /work/job.json and the two patches, then:
  1. fetches the repo at the commit before the fix
  2. installs it with its test dependencies, as they were when the fix was merged
  3. adds the new tests only, and runs them   -> they should FAIL
  4. adds the fix too, and runs them again    -> they should PASS
and writes /work/result.json. The host never runs the project's code itself:
that only ever happens in here, isolated from the PC.
Must run on Python 3.8+: older records are checked on older Pythons.
"""

from __future__ import annotations  # lets "str | None" hints work on Python 3.8

import json
import re
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
        # errors="replace": some test suites print raw bytes that aren't valid text
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
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


def toml_section_keys(text: str, section: str) -> set[str]:
    """Keys in one [section] of a TOML file. A tiny parser, because tomllib
    only exists on Python 3.11+ and older records run on older Pythons."""
    keys: set[str] = set()
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == f"[{section}]"
            continue
        match = re.match(r'^"?([A-Za-z0-9_.-]+)"?\s*=', stripped) if inside else None
        if match:
            keys.add(match.group(1))
    return keys


def extras_available(repo: Path) -> set[str]:
    """Names of optional dependency groups the project declares."""
    names: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        names |= toml_section_keys(text, "project.optional-dependencies")
        names |= {f"group:{g}" for g in toml_section_keys(text, "dependency-groups")}
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


NETWORK_ERRORS = ("Request failed", "Connection", "timed out", "Temporary failure", "502", "503")


def run_install(cmd: list[str], repo: Path, timeout: int = 900) -> tuple[int, str]:
    """Run an install, retrying once if the failure looks like a network hiccup."""
    code, out = run(cmd, repo, timeout)
    if code != 0 and any(word in out for word in NETWORK_ERRORS):
        code, out = run(cmd, repo, timeout)
    return code, out


MODERN_SETUPTOOLS = "setuptools>=64,<71"


def needs_modern_setuptools(out: str) -> bool:
    """Editable installs of setup.py-only projects need setuptools 64+ (2022).
    Installing "as of" an older date gives an older setuptools, which can't."""
    return "build_editable" in out


def install_editable(pip: list[str], target: str, repo: Path) -> tuple[int, str]:
    """pip install -e, falling back to today's setuptools for old setup.py projects.

    The fallback only changes the build tool: the project's own dependencies
    still come from the merge date."""
    code, out = run_install(pip + ["-e", target], repo)
    if code != 0 and needs_modern_setuptools(out):
        # 64+ can do editable installs; below 71 it uses its own built-in copy of
        # "packaging", not the project's dated one (which may be too old for it)
        _, tools = run_install(pip_command(None) + [MODERN_SETUPTOOLS, "wheel"], repo)
        code, out = run_install(pip + ["--no-build-isolation", "-e", target], repo)
        out = tools + "\n(retried with current setuptools, no build isolation)\n" + out
    return code, out


def subprojects(repo: Path, test_files: list[str]) -> list[Path]:
    """Folders between a test file and the repo root that are their own
    installable project (e.g. Flask's examples/javascript)."""
    found: list[Path] = []
    for test_file in test_files:
        folder = (repo / test_file).parent
        while folder != repo and repo in folder.parents:
            if ((folder / "pyproject.toml").exists() or (folder / "setup.py").exists()) and folder not in found:
                found.append(folder)
            folder = folder.parent
    return found


def install(repo: Path, date: str | None = None, test_files: list[str] | None = None) -> str:
    """Install the project, its test dependencies and pytest, as of `date`."""
    pip = pip_command(date)
    log = []
    for req in ("requirements/tests.txt", "requirements/test.txt", "requirements-dev.txt",
                "requirements/dev.txt", "test-requirements.txt", "requirements-test.txt"):
        if (repo / req).exists():
            code, out = run_install(pip + ["-r", req], repo)
            log.append(out)
            if code == 0:
                break

    available = extras_available(repo)
    for group in ("tests", "test", "testing", "dev"):
        if f"group:{group}" in available:
            code, out = run_install(pip + ["--group", group], repo)
            log.append(out)
            break

    extra = next((e for e in ("testing", "tests", "test", "dev") if e in available), None)
    target = f".[{extra}]" if extra else "."
    code, out = install_editable(pip, target, repo)
    log.append(out)
    if code != 0:
        raise StepFailed("couldn't install the project: " + tail(out, 8))
    for folder in subprojects(repo, test_files or []):
        code, out = install_editable(pip, str(folder), repo)
        log.append(out)
        if code != 0:
            raise StepFailed(f"couldn't install {folder.relative_to(repo)}: " + tail(out, 8))
    if run([sys.executable, "-c", "import pytest"], repo, 60)[0] != 0:
        code, out = run_install(pip + ["pytest"], repo, 600)  # pytest from the same date
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


def test_command(test_files: list[str], xml_path: Path, keep_project_options: bool = True) -> list[str]:
    """--continue-on-collection-errors: one test file that can't load must not
    stop every other test from running.

    The project's own pytest options (addopts) are kept: some test suites need
    them (pytest's own suite loads its "pytester" plugin that way)."""
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA",
           "--continue-on-collection-errors", f"--junitxml={xml_path}"]
    if not keep_project_options:
        cmd += ["-o", "addopts="]
    return cmd + list(test_files)


def needs_plain_options(code: int, output: str) -> bool:
    """Exit code 4 is pytest's "usage error". If it's about an option from the
    project's config that we can't honour (a plugin that isn't installed),
    run again without the project's options."""
    return code == 4 and ("unrecognized arguments" in output or "no such option" in output)


def run_tests(repo: Path, test_files: list[str], xml_path: Path, timeout: int) -> tuple[dict, str]:
    existing = [f for f in test_files if (repo / f).exists()]
    if not existing:
        return {}, "no test files to run"
    xml_path.unlink(missing_ok=True)
    code, out = run(test_command(existing, xml_path), repo, timeout)
    if needs_plain_options(code, out):
        code, out = run(test_command(existing, xml_path, keep_project_options=False), repo, timeout)
    return parse_junit(xml_path), tail(out)


def main(work: Path) -> dict:
    job = json.loads((work / "job.json").read_text(encoding="utf-8"))
    repo = work / "repo"
    result = {"status": "error", "note": "", "before": {}, "after": {}, "log_before": "", "log_after": "",
              "python_version": sys.version.split()[0]}
    try:
        checkout(job["repo_url"], job["base_commit"], repo)
        if job.get("install", True):
            install(repo, job.get("exclude_newer"), job["test_files"])
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
