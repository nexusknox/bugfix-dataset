"""Reading and writing records as JSON Lines (one record per line)."""

import json
from pathlib import Path

from bugfix.models import Record


def load(path: Path) -> list[Record]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [Record.from_dict(json.loads(line)) for line in f if line.strip()]


def save(path: Path, records: list[Record]) -> None:
    """Write atomically: a crash mid-write never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with open(temporary, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    temporary.replace(path)


def merge(existing: list[Record], new: list[Record]) -> tuple[list[Record], int]:
    """Add new records, skipping ones already there (same id or same fix).
    Returns (all records, how many were added)."""
    ids = {r.id for r in existing}
    prints = {r.fingerprint for r in existing}
    merged = list(existing)
    added = 0
    for record in new:
        if record.id in ids or record.fingerprint in prints:
            continue
        merged.append(record)
        ids.add(record.id)
        prints.add(record.fingerprint)
        added += 1
    return merged, added