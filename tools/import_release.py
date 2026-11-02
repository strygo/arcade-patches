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
    args = parser.parse_args()
    try:
        if args.recover:
            if args.ready or args.candidate:
                parser.error("--recover cannot be combined with other operations")
            from kit_revisions import recover
            from collection_catalog import recover as recover_collection, JOURNAL
            pending = (ROOT / JOURNAL).exists()
            if (ROOT / 'data/kit-revision-transaction.json').exists():
                recover(ROOT)
            elif not pending:
                raise ValueError('no interrupted publication to recover')
            recover_collection(ROOT)
            print("Recovered previous publication")
            return
        if not args.ready or not args.candidate:
            parser.error("import requires --ready and --candidate")
        from collection_catalog import prepare, begin, recover as finish_collection
        pending = prepare(ROOT, args.ready.resolve(), args.candidate.resolve())
        begin(ROOT, pending)
        imported = import_candidate(ROOT, args.ready.resolve(), args.candidate.resolve())
        if pending:finish_collection(ROOT)
    except (ReleaseInventoryError, ValueError, OSError) as exc:
        raise SystemExit(f"import rejected: {exc}. If a transaction remains, run --recover before retrying") from exc
    names = ", ".join(record["name"] for record in imported["files"].values())
    print(f"imported: {names}")


if __name__ == "__main__":
    main()
