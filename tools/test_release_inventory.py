#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_inventory import (ReleaseInventoryError, import_candidate,
                               load_inventory, published_bundle, sha256_file,
                               validate_inventory)
from verify_hosted_releases import verify_hosted


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_rom_zip(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": version,
        "members": [{"name": "game.03", "size": 4, "stock_crc32": "00000000",
                     "patched_crc32": "11111111", "action": "patch"}],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("apply.py", "pass\n")


class ReleaseInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "docs/downloads").mkdir(parents=True)
        old = self.root / "docs/downloads/demo-rc1-ips.zip"
        write_rom_zip(old, "rc1")
        write_json(self.root / "data/patches.json", {
            "patches": [{"slug": "demo", "version": "rc1", "artifact": {}}]
        })
        write_json(self.root / "data/releases.json", {
            "schema": 1,
            "releases": {
                "demo": {
                    "current": "rc1",
                    "versions": {
                        "rc1": {
                            "date": "2026-01-01",
                            "kind": "rom",
                            "files": {"ips": {"name": old.name,
                                              "size": old.stat().st_size,
                                              "sha256": sha256_file(old)}},
                            "qualification": {},
                        }
                    },
                }
            },
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def candidate(self, version: str = "rc2", slug: str = "demo") -> tuple[Path, Path]:
        candidate = self.root / f"candidate-{slug}-{version}"
        download = candidate / "downloads" / f"{slug}-{version}-ips.zip"
        write_rom_zip(download, version)
        release = {
            "schema": 1,
            "status": "candidate",
            "plan": {"kit": slug, "version": version, "candidate": f"{version}-1",
                     "date": "2026-02-02", "sources": {"capcom": "a" * 40}},
            "downloads": {download.name: sha256_file(download)},
        }
        release_path = candidate / "release.json"
        write_json(release_path, release)
        ready = {
            "schema": 1,
            "status": "ready_for_import",
            "release": release,
            "release_sha256": sha256_file(release_path),
            "qa": {"status": "passed", "release_sha256": sha256_file(release_path)},
            "reproduction": {"status": "clean_rebuild_identical",
                             "release_sha256": sha256_file(release_path),
                             "downloads": release["downloads"]},
        }
        ready_path = self.root / f"{slug}.ready.json"
        write_json(ready_path, ready)
        return ready_path, candidate

    def set_page_version(self, version: str) -> None:
        config = json.loads((self.root / "data/patches.json").read_text())
        config["patches"][0]["version"] = version
        write_json(self.root / "data/patches.json", config)

    def test_import_appends_and_activates_qualified_version(self) -> None:
        ready, candidate = self.candidate()
        self.set_page_version("rc2")
        import_candidate(self.root, ready, candidate)
        inventory = load_inventory(self.root / "data/releases.json")
        self.assertEqual("rc2", inventory["releases"]["demo"]["current"])
        self.assertEqual({"rc1", "rc2"}, set(inventory["releases"]["demo"]["versions"]))
        validate_inventory(self.root, inventory)
        bundle = published_bundle(
            self.root, json.loads((self.root / "data/patches.json").read_text())["patches"][0],
            inventory)
        self.assertEqual("demo-rc2-ips.zip", bundle["ips"]["zipname"])

    def test_existing_version_cannot_be_replaced(self) -> None:
        ready, candidate = self.candidate("rc1")
        with self.assertRaisesRegex(ReleaseInventoryError, "cannot be replaced"):
            import_candidate(self.root, ready, candidate)
        self.assertEqual("rc1", load_inventory(self.root / "data/releases.json")
                         ["releases"]["demo"]["current"])

    def test_new_kit_can_be_imported(self) -> None:
        config = json.loads((self.root / "data/patches.json").read_text())
        config["patches"].append({"slug": "newkit", "version": "1.0", "artifact": {}})
        write_json(self.root / "data/patches.json", config)
        ready, candidate = self.candidate("1.0", "newkit")
        import_candidate(self.root, ready, candidate)
        inventory = load_inventory(self.root / "data/releases.json")
        self.assertEqual("1.0", inventory["releases"]["newkit"]["current"])
        validate_inventory(self.root, inventory)

    def test_changed_candidate_is_rejected_without_copy(self) -> None:
        ready, candidate = self.candidate()
        self.set_page_version("rc2")
        with (candidate / "downloads/demo-rc2-ips.zip").open("ab") as output:
            output.write(b"changed")
        with self.assertRaisesRegex(ReleaseInventoryError, "hash mismatch"):
            import_candidate(self.root, ready, candidate)
        self.assertFalse((self.root / "docs/downloads/demo-rc2-ips.zip").exists())
        self.assertEqual("rc1", load_inventory(self.root / "data/releases.json")
                         ["releases"]["demo"]["current"])

    def test_changed_published_download_blocks_the_site_inventory(self) -> None:
        with (self.root / "docs/downloads/demo-rc1-ips.zip").open("ab") as output:
            output.write(b"changed")
        with self.assertRaisesRegex(ReleaseInventoryError, "size changed"):
            validate_inventory(self.root, load_inventory(self.root / "data/releases.json"))

    def test_hosted_verifier_checks_inventory_bytes(self) -> None:
        verified = verify_hosted(self.root, (self.root / "docs").as_uri())
        self.assertEqual(["demo rc1 ips: demo-rc1-ips.zip"], verified)


if __name__ == "__main__":
    unittest.main()
