#!/usr/bin/env python3
"""Producer-only IPS packager regression."""
from __future__ import annotations

import json
import hashlib
import sys
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_packager as packager


class ReleasePackagerTests(unittest.TestCase):
    def test_full_stock_device_member_is_optional_and_hash_checked(self):
        firmware = b'fixture device firmware'
        game = {'game.03': b'stock', 'dl-1425.bin': firmware}
        config = {'artifact': {'auxiliary_stock_members': [{
            'name': 'dl-1425.bin', 'size': len(firmware),
            'sha256': hashlib.sha256(firmware).hexdigest()}]}}
        self.assertEqual({'game.03': b'stock'}, packager.source_members(config, game))
        self.assertEqual({'game.03': b'stock'}, packager.source_members(config, {'game.03': b'stock'}))
        self.assertIn('dl-1425.bin', game)
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            packager.source_members(config, dict(game, **{'dl-1425.bin': b'wrong'}))
        config['artifact']['auxiliary_stock_members'][0]['name'] = 'game.03'
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            packager.source_members(config, game)

    def test_auxiliary_firmware_does_not_change_default_game_inventory(self):
        # Execute the shipped CLI with a synthetic, positively identified
        # device. Exercise both canonical archive layouts and explicit opt-in.
        firmware = b'fixture device firmware'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stock, patched = root / 'stock.zip', root / 'patched.zip'
            for path, program in [(stock, b'stock'), (patched, b'fixed')]:
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr('game.03', program)
                    z.writestr('game.04', b'unchanged')
                    if path == stock:
                        z.writestr('dl-1425.bin', firmware)
            old = packager.DOCS, packager.SITE
            self.addCleanup(lambda: (setattr(packager, 'DOCS', old[0]), setattr(packager, 'SITE', old[1])))
            packager.DOCS = root / 'published'
            packager.SITE = {'title': 'Fixture'}
            patch = {'slug': 'device', 'title': 'Device', 'subtitle': 'Fixture',
                     'version': 'rc1', 'date': '2026-10-04', 'game': 'Demo',
                     'hardware': 'Fixture', 'set': 'demo', 'description': [], 'changes': [],
                     'artifact': {'stock_zip': str(stock), 'patched_zip': str(patched),
                                  'auxiliary_stock_members': [{
                                      'name': 'dl-1425.bin', 'size': len(firmware),
                                      'sha256': hashlib.sha256(firmware).hexdigest()}]},
                     'hbmame': {'setname': 'demorest', 'complete': True,
                                'renames': {'game.03': 'game.03', 'game.04': 'game.04'}}}
            packager.build_rom_downloads(patch)
            kit = root / 'kit'
            with zipfile.ZipFile(root / 'published/downloads/device-rc1-ips.zip') as z:
                self.assertIs(json.loads(z.read('manifest.json'))['mame_include_devices'], False)
                z.extractall(kit)
            spec = {'name': 'dl-1425.bin', 'size': len(firmware),
                    'sha256': hashlib.sha256(firmware).hexdigest(), 'role': 'device'}
            for embedded in (True, False):
                source = root / ('complete.zip' if embedded else 'game-only.zip')
                with zipfile.ZipFile(source, 'w') as z:
                    z.writestr('game.03', b'stock')
                    z.writestr('game.04', b'unchanged')
                    if embedded:
                        z.writestr('dl-1425.bin', firmware)
                argv = [str(kit / 'apply.py'), str(source), '--out-dir', str(root / str(embedded))]
                code = ('import runpy,sys;sys.path.insert(0,' + repr(str(kit)) + ');'
                        'import rom_sources;rom_sources.QSOUND=' + repr(spec) + ';'
                        'sys.argv=' + repr(argv) + ';runpy.run_path(sys.argv[0],run_name="__main__")')
                subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
                for platform, name in [('mame', 'demo'), ('hbmame', 'demorest')]:
                    with zipfile.ZipFile(root / str(embedded) / platform / (name + '.zip')) as z:
                        self.assertEqual({'game.03': b'fixed', 'game.04': b'unchanged'},
                                         {n: z.read(n) for n in z.namelist()})
                if embedded:
                    argv += ['--include-devices']
                    code = ('import runpy,sys;sys.path.insert(0,' + repr(str(kit)) + ');'
                            'import rom_sources;rom_sources.QSOUND=' + repr(spec) + ';'
                            'sys.argv=' + repr(argv) + ';runpy.run_path(sys.argv[0],run_name="__main__")')
                    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
                    with zipfile.ZipFile(root / str(embedded) / 'mame/demo.zip') as z:
                        self.assertEqual(firmware, z.read('dl-1425.bin'))

    def test_complete_hbmame_set_leaves_embedded_firmware_to_qsound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stock, patched = root / 'stock.zip', root / 'patched.zip'
            for path, program in [(stock, b'stock'), (patched, b'fixed')]:
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr('game.03', program)
                    z.writestr('game.04', b'unchanged')
                    z.writestr('dl-1425.bin', b'firmware')
            old = packager.DOCS, packager.SITE
            self.addCleanup(lambda: (setattr(packager, 'DOCS', old[0]), setattr(packager, 'SITE', old[1])))
            packager.DOCS = root / 'published'
            packager.SITE = {'title': 'Fixture'}
            patch = {'slug': 'embedded', 'title': 'Embedded', 'subtitle': 'Fixture',
                     'version': 'rc1', 'date': '2026-10-06', 'game': 'Demo',
                     'hardware': 'Fixture', 'set': 'demo', 'description': [], 'changes': [],
                     'artifact': {'stock_zip': str(stock), 'patched_zip': str(patched)},
                     'hbmame': {'setname': 'demorest', 'complete': True,
                                'renames': {'game.03': 'game.03', 'game.04': 'game.04'}}}
            packager.build_rom_downloads(patch)
            with zipfile.ZipFile(root / 'published/downloads/embedded-rc1-ips.zip') as z:
                z.extractall(root / 'kit')
            subprocess.run([sys.executable, str(root / 'kit/apply.py'), str(stock),
                            '--platform', 'hbmame', '--out-dir', str(root / 'out')],
                           check=True, capture_output=True)
            with zipfile.ZipFile(root / 'out/hbmame/demorest.zip') as z:
                self.assertEqual({'game.03': b'fixed', 'game.04': b'unchanged'},
                                 {n: z.read(n) for n in z.namelist()})

    def test_full_companion_reconstructs_unchanged_roms_in_hbmame_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stock, patched = root / 'stock.zip', root / 'patched.zip'
            for path, program in [(stock, b'stock'), (patched, b'fixed')]:
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr('game.03', program)
                    z.writestr('game.04', b'unchanged')
            old = packager.DOCS, packager.SITE
            self.addCleanup(lambda: (setattr(packager, 'DOCS', old[0]), setattr(packager, 'SITE', old[1])))
            packager.DOCS = root / 'published'
            packager.SITE = {'title': 'Fixture'}
            patch = {'slug': 'full', 'title': 'Full', 'subtitle': 'Fixture',
                     'version': 'rc1', 'date': '2026-10-04', 'game': 'Demo',
                     'hardware': 'Fixture', 'set': 'demo', 'description': [], 'changes': [],
                     'artifact': {'stock_zip': str(stock), 'patched_zip': str(patched)},
                     'hbmame': {'setname': 'demorest', 'complete': True,
                                'renames': {'game.03': 'game.03', 'game.04': 'game.04'}}}
            packager.build_rom_downloads(patch)
            with zipfile.ZipFile(root / 'published/downloads/full-rc1-ips.zip') as z:
                z.extractall(root / 'kit')
            subprocess.run([sys.executable, str(root / 'kit/apply.py'), str(stock),
                            '--platform', 'hbmame', '--out-dir', str(root / 'out')],
                           check=True, capture_output=True)
            with zipfile.ZipFile(root / 'out/hbmame/demorest.zip') as z:
                self.assertEqual({'game.03': b'fixed', 'game.04': b'unchanged'},
                                 {n: z.read(n) for n in z.namelist()})

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
