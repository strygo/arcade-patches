#!/usr/bin/env python3
"""Producer-only IPS packager regression."""
from __future__ import annotations

import json
import sys
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_packager as packager


class ReleasePackagerTests(unittest.TestCase):
    def test_builds_round_trip_checked_ips_without_the_site_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stock = root / "stock.zip"
            patched = root / "patched.zip"
            with zipfile.ZipFile(stock, "w") as archive:
                archive.writestr("game.03", b"stock")
            with zipfile.ZipFile(patched, "w") as archive:
                archive.writestr("game.03", b"fixed")
            old = packager.DOCS, packager.SITE
            self.addCleanup(lambda: (
                setattr(packager, "DOCS", old[0]),
                setattr(packager, "SITE", old[1])))
            packager.DOCS = root / "published"
            packager.SITE = {"title": "Fixture"}
            patch = {
                "slug": "demo", "title": "Demo", "subtitle": "Fixture",
                "version": "rc1", "date": "2026-09-19", "game": "Demo Game",
                "hardware": "Fixture", "set": "demo", "description": ["Fixture"],
                "changes": [],
                "artifact": {"stock_zip": str(stock), "patched_zip": str(patched)},
            }
            result = packager.build_rom_downloads(patch)
            self.assertEqual("demo-rc1-ips.zip", result["ips"]["zipname"])
            with zipfile.ZipFile(root / "published/downloads/demo-rc1-ips.zip") as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertIn("ips/game.03.ips", archive.namelist())
                self.assertIn("rom_sources.py", archive.namelist())
                archive.extractall(root / "kit")
            self.assertEqual("patch", manifest["members"][0]["action"])
            self.assertEqual(64, len(manifest["members"][0]["stock_sha256"]))
            subprocess.run([sys.executable, str(root / "kit/apply.py"), str(stock),
                            "--out-dir", str(root / "out")], check=True, capture_output=True)
            with zipfile.ZipFile(root / "out/mame/demo.zip") as archive:
                self.assertEqual(b"fixed", archive.read("game.03"))


if __name__ == "__main__":
    unittest.main()
