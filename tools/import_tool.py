#!/usr/bin/env python3
"""Import the ArcadePatches launcher, alone or with a prepared catalog; never rebuild games."""
import argparse
from pathlib import Path
from tool_inventory import import_launcher, import_tool

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, help='prepared catalog directory (initial or full catalog release)')
    parser.add_argument('--qualification', type=Path, help='catalog qualification for --catalog')
    parser.add_argument('--launcher-only', action='store_true',
                        help='publish a newer launcher over the current catalog and announce it to older launchers')
    parser.add_argument('--date', help='tool page release date for --launcher-only (YYYY-MM-DD)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.launcher_only:
        if args.catalog or args.qualification or not args.date:
            parser.error('--launcher-only takes --date and no catalog')
        record = import_launcher(root, args.archive, args.receipt, args.date)
    else:
        if not args.catalog or not args.qualification:
            parser.error('a catalog import needs --catalog and --qualification')
        record = import_tool(root, args.archive, args.receipt, args.catalog, args.qualification)
    print(f"Imported ArcadePatches {record['version']}, catalog generation {record['generation']}: {len(record['files'])} files")
