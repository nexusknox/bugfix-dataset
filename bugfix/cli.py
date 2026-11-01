"""Command line for the pipeline.

    python -m bugfix.cli discover --min-stars 2000 --limit 10
    python -m bugfix.cli mine psf/requests --max-prs 300
    python -m bugfix.cli stats
"""

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from bugfix import store
from bugfix.github import GitHubClient, GitHubError
from bugfix.mine import discover, mine_repo

CANDIDATES = Path("data/candidates.jsonl")


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

    p = sub.add_parser("stats", help="summarise the collected records")
    p.set_defaults(run=cmd_stats)

    args = parser.parse_args(argv)
    logging.basicConfig(filename="bugfix.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        args.run(args)
    except GitHubError as error:
        print(f"Stopped: {error}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped. Everything collected so far is saved; cached API calls won't be repeated.")
        sys.exit(1)


if __name__ == "__main__":
    main()