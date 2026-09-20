#!/usr/bin/env python3
"""Verify that hosted current downloads exactly match the tracked inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from release_inventory import ReleaseInventoryError, load_inventory, validate_inventory


ROOT = Path(__file__).resolve().parent.parent


def hosted_digest(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "arcade-patches-release-verifier/1"})
    h = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                size += len(block)
                h.update(block)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseInventoryError(f"cannot fetch {url}: {exc}") from exc
    return size, h.hexdigest()


def verify_hosted(root: Path, base_url: str) -> list[str]:
    inventory = load_inventory(root / "data" / "releases.json")
    validate_inventory(root, inventory)
    base = base_url.rstrip("/") + "/downloads/"
    verified = []
    for slug, release in sorted(inventory["releases"].items()):
        version = release["current"]
        files = release["versions"][version]["files"]
        for role, record in sorted(files.items()):
            url = urllib.parse.urljoin(base, urllib.parse.quote(record["name"]))
            size, digest = hosted_digest(url)
            if size != record["size"]:
                raise ReleaseInventoryError(
                    f"hosted size mismatch for {record['name']}: {size} != {record['size']}")
            if digest != record["sha256"]:
                raise ReleaseInventoryError(
                    f"hosted hash mismatch for {record['name']}: {digest} != {record['sha256']}")
            verified.append(f"{slug} {version} {role}: {record['name']}")
    return verified


def main() -> None:
    config = json.loads((ROOT / "data" / "patches.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=config["site"]["site_url"],
                        help="published site root (default: site.site_url)")
    args = parser.parse_args()
    try:
        verified = verify_hosted(ROOT, args.base_url)
    except ReleaseInventoryError as exc:
        raise SystemExit(f"hosted verification failed: {exc}") from exc
    for item in verified:
        print(f"verified: {item}")
    print(f"hosted verification passed: {len(verified)} downloads")


if __name__ == "__main__":
    main()
