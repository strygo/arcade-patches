#!/usr/bin/env python3
"""Import one qualified Capcom candidate into the append-only public inventory."""
from __future__ import annotations

import argparse
from pathlib import Path

from release_inventory import ReleaseInventoryError, import_candidate


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", type=Path, required=True,
                        help="Capcom *.ready.json produced by check-ready")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="immutable Capcom candidate directory containing release.json")
    args = parser.parse_args()
    try:
        imported = import_candidate(ROOT, args.ready.resolve(), args.candidate.resolve())
    except ReleaseInventoryError as exc:
        raise SystemExit(f"import rejected: {exc}") from exc
    names = ", ".join(record["name"] for record in imported["files"].values())
    print(f"imported: {names}")


if __name__ == "__main__":
    main()
