"""Command line for the pipeline.

    python -m bugfix.cli discover --min-stars 2000 --limit 10
    python -m bugfix.cli mine psf/requests --max-prs 300
    python -m bugfix.cli build-image
    python -m bugfix.cli verify --limit 5
    python -m bugfix.cli export
    python -m bugfix.cli stats
    streamlit run viewer/app.py
"""

import argparse
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from bugfix import store
from bugfix.export import export
from bugfix.github import GitHubClient, GitHubError
from bugfix.mine import discover, mine_repo
from bugfix.verify import PYTHON_RELEASES, DockerError, build_image, docker_ready, image_for, verify_record

CANDIDATES = Path("data/candidates.jsonl")
WORK = Path("work")


def make_client() -> GitHubClient:
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("No GITHUB_TOKEN found: limited to 60 requests an hour. Add one to .env (see .env.example).")
    return GitHubClient(token)


def cmd_discover(args):
    client = make_client()
    print(f"Searching for Python repos with {args.min_stars}+ stars, permissive licence and tests…")
    for info in discover(client, args.min_stars, args.limit):
        print(f"  {info.name:<45} {info.stars:>7} stars  {info.license}")
    print(f"({client.requests_made} API requests)")


def cmd_mine(args):
    client = make_client()
    total_added = 0
    for repo in args.repos:
        print(f"Mining {repo} (up to {args.max_prs} pull requests)…")
        result = mine_repo(client, repo, args.max_prs)
        records, added = store.merge(store.load(CANDIDATES), result.records)
        store.save(CANDIDATES, records)
        total_added += added
        print(f"  checked {result.prs_checked} PRs, {len(result.records)} candidates, {added} new")
        for reason, count in result.rejected.most_common(6):
            print(f"    skipped {count:>4}: {reason}")
    print(f"Done: {total_added} new candidate(s) in {CANDIDATES} ({client.requests_made} API requests)")


def cmd_build_image(args):
    problem = docker_ready()
    if problem:
        print(problem)
        sys.exit(1)
    versions = [args.python] if args.python else [v for v, _ in PYTHON_RELEASES]
    for version in versions:
        print(f"Building {image_for(version)} (first time takes a few minutes)…")
        if not build_image(version):
            print("Build failed: see the messages above.")
            sys.exit(1)
    print("Image(s) ready.")


def cmd_verify(args):
    problem = docker_ready()
    if problem:
        print(problem)
        sys.exit(1)
    records = store.load(CANDIDATES)
    wanted = {"not_run"} | ({"failed", "error", "timeout"} if args.retry else set())
    todo = [r for r in records if r.verification.status in wanted and (not args.repo or r.repo == args.repo)]
    todo = todo[: args.limit]
    if not todo:
        print("Nothing to verify. (Use --retry to try failed ones again.)")
        return
    print(f"Verifying {len(todo)} record(s). Each one installs a project in Docker: allow a few minutes each.")
    for number, record in enumerate(todo, start=1):
        started = time.time()
        print(f"[{number}/{len(todo)}] {record.id} … ", end="", flush=True)
        verify_record(record, WORK)
        v = record.verification
        took = f"{time.time() - started:.0f}s"
        if v.status == "verified":
            print(f"verified ({len(v.fail_to_pass)} test(s) fail before, pass after) [{took}]")
        else:
            print(f"{v.status}: {v.note[:120]} [{took}]")
        store.save(CANDIDATES, records)  # after every record, so stopping loses nothing


def cmd_export(args):
    records = store.load(CANDIDATES)
    if not records:
        print("No records yet. Run: python -m bugfix.cli mine owner/repo")
        return
    out = Path(args.out)
    numbers = export(records, out, verified_only=not args.include_unverified, min_score=args.min_score)
    print(f"Exported {numbers['released']} record(s) to {out}")
    print("  dataset.jsonl, test_logs.jsonl, swebench.jsonl, stats.json, DATASET_CARD.md, sample.jsonl")


def cmd_stats(args):
    records = store.load(CANDIDATES)
    if not records:
        print("No records yet. Run: python -m bugfix.cli mine owner/repo")
        return
    print(f"{len(records)} record(s) in {CANDIDATES}")
    for repo, count in Counter(r.repo for r in records).most_common():
        print(f"  {repo:<40} {count}")
    statuses = Counter(r.verification.status for r in records)
    print("Verification: " + ", ".join(f"{s} {n}" for s, n in statuses.most_common()))
    print(f"Average quality score: {sum(r.quality.score for r in records) / len(records):.0f}/100")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bugfix", description="Build a verified bug-fix dataset from GitHub.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="find Python repos that can be mined")
    p.add_argument("--min-stars", type=int, default=1000)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(run=cmd_discover)

    p = sub.add_parser("mine", help="collect candidate bug fixes from repos")
    p.add_argument("repos", nargs="+", help="e.g. psf/requests")
    p.add_argument("--max-prs", type=int, default=300)
    p.set_defaults(run=cmd_mine)

    p = sub.add_parser("build-image", help="build the Docker images records are verified in")
    p.add_argument("--python", help="just one version, e.g. 3.11 (default: all)")
    p.set_defaults(run=cmd_build_image)

    p = sub.add_parser("verify", help="prove each fix: tests fail before it, pass after it")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--repo", help="only this repo, e.g. pallets/flask")
    p.add_argument("--retry", action="store_true", help="also retry failed and errored records")
    p.set_defaults(run=cmd_verify)

    p = sub.add_parser("export", help="write a release: dataset, logs, SWE-bench format, dataset card")
    p.add_argument("--out", default="data/release")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--include-unverified", action="store_true")
    p.set_defaults(run=cmd_export)

    p = sub.add_parser("stats", help="summarise the collected records")
    p.set_defaults(run=cmd_stats)

    args = parser.parse_args(argv)
    logging.basicConfig(filename="bugfix.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        args.run(args)
    except (GitHubError, DockerError) as error:
        print(f"Stopped: {error}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped. Everything collected so far is saved; cached API calls won't be repeated.")
        sys.exit(1)


if __name__ == "__main__":
    main()
