#!/usr/bin/env python3
"""Import the ArcadePatches launcher and a prepared catalog; never rebuild games."""
import argparse
from pathlib import Path
from tool_inventory import import_tool

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--qualification', type=Path, required=True)
    args = parser.parse_args()
    record = import_tool(Path(__file__).resolve().parents[1], args.archive, args.receipt, args.catalog, args.qualification)
    print(f"Imported ArcadePatches {record['version']}, catalog generation {record['generation']}: {len(record['files'])} files")
