#!/usr/bin/env python3
"""Keep names and review citations out of every published download.

A download may credit its author ("Patch by:", "Backport by:", "Project by:")
and nothing else personal: no names, no personal paths, and no comments that
cite who reported, asked for or approved something.  Downloads published
before this gate carry some such comments; they are recorded in
data/content_audit_baseline.json and tolerated.  Any other hit rejects an
import.

  python3 tools/content_audit.py            audit every current download
  python3 tools/content_audit.py --baseline rewrite the baseline from the
                                            published downloads (do this only
                                            to record already-public text)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "data" / "content_audit_baseline.json"
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".tsv", ".toml", ".lua", ".mra", ".cfg", ".ini", ".xml"}
# The author credit, e.g. "Patch by: Steve Gordon (https://x.com/strygo)".
CREDIT = re.compile(r"\b(Patch|Backport|Project) by:?\s")
PATTERNS = re.compile(
    r"\bSteve\b|\bGordon\b|srg@|sgordon|/Users/|/home/[a-z]"
    r"|\b(?:user|owner|reviewer|tester|review)[- ](?:reported|asked|requested|approved|curated|chose|confirmed)\b"
    r"|\b(?:reported|asked for|requested|flagged|caught) (?:in|by|during) (?:review|testing|listening)\b"
    r"|\b(?:the )?user asked\b|\bthe owner's\b|\bear[- ]approved\b|\bsigned[- ]off\b|\blistening sign-off\b"
    r"|\bper (?:review|the review)\b|\bas (?:requested|asked)\b",
    re.IGNORECASE)


def member_key(name: str) -> str:
    """Member path without a versioned top folder (cpsplus-kit-1.3b/...)."""
    return re.sub(r"^[^/]*-\d[^/]*/", "", name)


def hits(path: Path) -> list[tuple[str, str]]:
    found = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith("/") or Path(name).suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = archive.read(name).decode("utf-8", errors="replace")
            for line in text.splitlines():
                if PATTERNS.search(line) and not CREDIT.search(line):
                    found.append((member_key(name), line.strip()))
    return found


def load_baseline() -> set[tuple[str, str]]:
    if not BASELINE.exists():
        return set()
    return {(row["member"], row["line"]) for row in json.loads(BASELINE.read_text())["tolerated"]}


def check(path: Path) -> None:
    """Raise ValueError when a download adds a name or review citation."""
    tolerated = load_baseline()
    new = [(m, l) for m, l in hits(path) if (m, l) not in tolerated]
    if new:
        shown = "; ".join(f"{m}: {l[:100]}" for m, l in new[:5])
        raise ValueError(f"{path.name} carries names or review citations "
                         f"({len(new)} line(s)): {shown}")


def published() -> list[Path]:
    return sorted(p for p in (ROOT / "docs" / "downloads").rglob("*.zip"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args()
    if args.baseline:
        rows = sorted({hit for path in published() for hit in hits(path)})
        BASELINE.write_text(json.dumps({
            "note": "Text already public before the content audit; tolerated, never extended by hand.",
            "tolerated": [{"member": m, "line": l} for m, l in rows]}, indent=1, ensure_ascii=False) + "\n")
        print(f"baseline: {len(rows)} tolerated lines")
        return 0
    failures = 0
    for path in published():
        try:
            check(path)
        except ValueError as exc:
            failures += 1
            print(exc)
    print("content audit:", "clean" if not failures else f"{failures} download(s) fail")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
