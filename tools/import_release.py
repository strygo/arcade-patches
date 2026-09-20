#!/usr/bin/env python3
"""Import one qualified Capcom candidate into the append-only public inventory."""
from __future__ import annotations

import argparse
from pathlib import Path

from release_inventory import ReleaseInventoryError, import_candidate


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", type=Path,
                        help="Capcom *.ready.json produced by check-ready")
    parser.add_argument("--candidate", type=Path,
                        help="immutable Capcom candidate directory containing release.json")
    parser.add_argument("--recover", action="store_true", help="restore an interrupted kit revision transaction")
    parser.add_argument("--rollback", nargs=3, metavar=("KIT", "VERSION", "REVISION"))
    args = parser.parse_args()
    try:
        if args.recover:
            if args.rollback or args.ready or args.candidate:
                parser.error("--recover cannot be combined with other operations")
            from kit_revisions import recover
            recover(ROOT)
            print("Recovered previous publication")
            return
        if args.rollback:
            if args.ready or args.candidate:
                parser.error("--rollback cannot be combined with import")
            from kit_revisions import rollback
            rollback(ROOT, args.rollback[0], args.rollback[1], int(args.rollback[2]))
            print("Restored selected kit revision")
            return
        if not args.ready or not args.candidate:
            parser.error("import requires --ready and --candidate")
        imported = import_candidate(ROOT, args.ready.resolve(), args.candidate.resolve())
    except (ReleaseInventoryError, ValueError, OSError) as exc:
        raise SystemExit(f"import rejected: {exc}") from exc
    names = ", ".join(record["name"] for record in imported["files"].values())
    print(f"imported: {names}")


if __name__ == "__main__":
    main()
