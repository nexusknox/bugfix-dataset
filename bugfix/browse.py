"""Filtering and summarising records for the viewer. No Streamlit here,
so it can be tested on its own."""

from collections import Counter

from bugfix.models import Record


def filter_records(records: list[Record], repos: list[str] | None = None, statuses: list[str] | None = None,
                   min_score: int = 0, search: str = "") -> list[Record]:
    words = search.lower().split()

    def matches(r: Record) -> bool:
        text = f"{r.id} {r.issue.title} {r.issue.body} {r.pr.title}".lower()
        return all(w in text for w in words)

    return [
        r for r in records
        if (not repos or r.repo in repos)
        and (not statuses or r.verification.status in statuses)
        and r.quality.score >= min_score
        and matches(r)
    ]


def summary(records: list[Record]) -> dict:
    statuses = Counter(r.verification.status for r in records)
    tried = sum(n for s, n in statuses.items() if s != "not_run")
    return {
        "records": len(records),
        "verified": statuses.get("verified", 0),
        "verified_rate": round(100 * statuses.get("verified", 0) / tried) if tried else 0,
        "repos": len({r.repo for r in records}),
        "average_quality": round(sum(r.quality.score for r in records) / len(records)) if records else 0,
    }


def row(r: Record) -> dict:
    return {"id": r.id, "repo": r.repo, "status": r.verification.status, "score": r.quality.score,
            "fail→pass": len(r.verification.fail_to_pass), "issue": r.issue.title[:80]}
