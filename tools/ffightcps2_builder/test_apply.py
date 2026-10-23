#!/usr/bin/env python3
"""apply.py on synthetic data: no ROMs, discs or builder modules.

A copy of the kit gets a synthetic romset catalog and romsets, stub ffex_kit and
packs modules (the release stages the real ones), MRAs and definitions made for
the stub's members, and a placeholder core. The real shared rom_sources.py and
darksoft.py run unchanged.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = json.loads((HERE / "kit.json").read_text())
SHARED = ("apply.py", "rom_sources.py", "darksoft.py", "darksoft_profiles.json", "darksoft_kits.json", "kit.json")

STUB_KIT = '''
import json, os
from pathlib import Path
HERE = Path(__file__).resolve().parent
PROFILE = json.loads((HERE / "darksoft_profiles.json").read_text())["profiles"]["sfa3ud"]["members"]

def build(edition, romsets, discs, work):
    assert sorted(romsets) == ["ffight", "ffightj", "ffightu", "sfa3u"], sorted(romsets)
    assert all(romsets[s] == {s + ".bin": s.encode() * 8} for s in romsets)
    log = HERE / "calls.json"
    calls = json.loads(log.read_text()) if log.exists() else []
    log.write_text(json.dumps(calls + [{"edition": edition, "discs": {k: v and Path(v).name for k, v in discs.items()}}]))
    return members(edition, os.environ.get("FFEX2_TEST_SMALL"))

def members(edition, small):
    result = {}
    for i, (name, size) in enumerate(sorted(PROFILE.items())):
        result[name] = bytes([i + 1 + 32 * ("usa", "japan", "english").index(edition)]) * (16 if small else size)
    result["phoenix.key"] = b"\\xff" * 20
    return result
'''

STUB_PACKS = '''
from pathlib import Path

def build(disc_jp, disc_us, edition, out_path, *, work, keyed=None, expect_sha256=None):
    disc = disc_us if edition == "usa" else disc_jp
    assert disc is not None and keyed is not None
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(b"CP2A" + edition.encode())
    return Path(out_path)
'''


def stub_members(kit):
    """The stub's members for each (edition, small) pair."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ffex_kit_stub", kit / "ffex_kit.py")
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)
    return {(e, small): stub.members(e, small) for e in KIT["editions"] for small in (True, False)}


def mra(meta, members):
    parts = "\n".join(f'    <part name="{n}" crc="{zlib.crc32(d):08x}" />' for n, d in sorted(members.items()))
    return (f'<misterromdescription>\n  <name>{meta["mra"]}</name>\n  <setname>{meta["set"]}</setname>\n'
            f'  <rom index="0" zip="/hbmame/{meta["set"]}.zip" address="0x30000000">\n{parts}\n'
            f'    <part name="dl-1425.bin" crc="d6cf5ef5" length="0x2000" />\n'
            f'    <part name="{meta["pack"]}.cpk" zip="/CPS+/{meta["pack"]}.zip" />\n  </rom>\n</misterromdescription>\n')


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ffex2-kit-test-")
        root = Path(self.tmp.name)
        self.kit, self.roms = root / "kit", root / "roms"
        self.kit.mkdir()
        for name in SHARED:
            shutil.copyfile(HERE / name, self.kit / name)
        (self.kit / "ffex_kit.py").write_text(STUB_KIT)
        (self.kit / "packs.py").write_text(STUB_PACKS)
        (self.kit / "core").mkdir()
        (self.kit / "core" / KIT["core"]).write_bytes(b"core")
        (self.kit / "keyed").mkdir()
        sets = {}
        self.roms.mkdir()
        for s in KIT["romsets"]:
            data = s.encode() * 8
            sets[s] = {f"{s}.bin": {"name": f"{s}.bin", "size": len(data), "crc32": f"{zlib.crc32(data):08x}",
                                    "sha256": hashlib.sha256(data).hexdigest(), "role": "game"}}
            with zipfile.ZipFile(self.roms / f"{s}.zip", "w") as z:
                z.writestr(f"{s}.bin", data)
        (self.kit / "rom_inputs.json").write_text(json.dumps({"schema": 1, "sets": sets}))
        (self.kit / "mras").mkdir()
        (self.kit / "definitions").mkdir()
        self.small = stub_members(self.kit)
        for meta in KIT["editions"].values():
            (self.kit / "definitions" / f"{meta['set']}.cpp.inc").write_text(f"// {meta['set']}\n")
        self.discs = root / "jp.img", root / "us.bin"
        for disc in self.discs:
            disc.write_bytes(b"disc")

    def tearDown(self):
        self.tmp.cleanup()

    def write_mras(self, small):
        for edition, meta in KIT["editions"].items():
            (self.kit / "mras" / f"{meta['mra']}.mra").write_text(mra(meta, self.small[(edition, small)]))

    def run_apply(self, *args, small=True):
        env = dict(os.environ, PYTHONPATH="", CAPCOM_ARCADE_ROM_PATH="", FFEX2_TEST_SMALL="1" if small else "")
        return subprocess.run([sys.executable, str(self.kit / "apply.py"), "--rompath", str(self.roms), *args],
                              cwd=self.kit.parent, env=env, capture_output=True, text=True)

    def test_check_needs_only_the_romsets(self):
        result = self.run_apply("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arcade inputs verified", result.stdout)

    def test_only_the_editions_whose_disc_is_given(self):
        self.write_mras(True)
        out = self.kit.parent / "out"
        result = self.run_apply("--disc-jp", str(self.discs[0]), "--platform", "hbmame", "--out-dir", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in (out / "hbmame").glob("*.zip")), ["ffex2j.zip", "ffex2r.zip"])
        self.assertEqual(sorted(p.name for p in (out / "hbmame/definitions").iterdir()),
                         ["ffex2j.cpp.inc", "ffex2r.cpp.inc"])
        calls = json.loads((self.kit / "calls.json").read_text())
        self.assertEqual([c["edition"] for c in calls], ["english", "japan"])
        self.assertEqual(calls[0]["discs"], {"jp": "jp.img", "us": None})
        with zipfile.ZipFile(out / "hbmame/ffex2r.zip") as z:
            self.assertEqual({n: z.read(n) for n in z.namelist()}, self.small[("english", True)])
        self.assertFalse((out / "mister").exists() or (out / "darksoft").exists())

    def test_mister_layout(self):
        self.write_mras(True)
        out = self.kit.parent / "out"
        result = self.run_apply("--disc-jp", str(self.discs[0]), "--disc-us", str(self.discs[1]),
                                "--platform", "mister", "--out-dir", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        root = out / "mister/_Arcade/_Arcade Patches/_Enhanced Versions"
        self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob("*.mra")),
                         ["Final Fight EX CPS2 (English Restoration).mra",
                          "_Japan/Final Fight EX CPS2 (Japan).mra", "_USA/Final Fight EX CPS2 (USA).mra"])
        text = (root / "_USA/Final Fight EX CPS2 (USA).mra").read_text()
        self.assertIn('zip="/hbmame/ffex2u.zip|qsound.zip|qsound_hle.zip"', text)
        self.assertIn('zip="/CPS+/ffightcps2_ex_arrange.zip"', text)
        self.assertEqual((out / "mister/_Arcade/cores" / KIT["core"]).read_bytes(), b"core")
        for edition, meta in KIT["editions"].items():
            with zipfile.ZipFile(out / f"mister/games/hbmame/{meta['set']}.zip") as z:
                self.assertEqual(z.read("sz3.01"), self.small[(edition, True)]["sz3.01"])
            with zipfile.ZipFile(out / f"mister/games/CPS+/{meta['pack']}.zip") as z:
                self.assertEqual(z.namelist(), [f"{meta['pack']}.cpk"])
                self.assertEqual(z.read(f"{meta['pack']}.cpk"), b"CP2A" + edition.encode())
        self.assertFalse((out / "hbmame").exists())

    def test_darksoft_phoenix_folder(self):
        out = self.kit.parent / "out"
        result = self.run_apply("--disc-us", str(self.discs[1]), "--platform", "darksoft", "--out-dir", str(out),
                                small=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        game = out / "darksoft/games/final-fight-ex-cps2_usa"
        self.assertEqual(sorted(p.name for p in game.iterdir()),
                         sorted(["NAME", "key"] + [f"flash.{i:02d}" for i in range(1, 13)]))
        self.assertEqual((game / "key").read_bytes(), b"\xff" * 20)
        menus = json.loads((HERE / "darksoft_kits.json").read_text())["dead_key"][KIT["darksoft"]]["menus"]
        self.assertEqual((game / "NAME").read_bytes(), menus["usa"].encode())
        members = self.small[("usa", False)]
        self.assertEqual((game / "flash.03").read_bytes(), members["sz3.11m"])
        receipt = json.loads((out / "darksoft/receipts/final-fight-ex-cps2_usa.json").read_text())
        self.assertEqual(receipt["dead_key_kit"], KIT["darksoft"])
        self.assertFalse(receipt["hardware_review"])

    def test_mismatched_mra_fails_before_publishing(self):
        self.write_mras(False)   # CRCs of other members than the build's
        out = self.kit.parent / "out"
        result = self.run_apply("--disc-us", str(self.discs[1]), "--out-dir", str(out))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the built", result.stderr)
        self.assertFalse(out.exists())

    def test_unstaged_mra_or_definitions_fail_before_building(self):
        out = self.kit.parent / "out"
        result = self.run_apply("--disc-us", str(self.discs[1]), "--platform", "mister", "--out-dir", str(out))
        self.assertIn("mras/Final Fight EX CPS2 (USA).mra is missing", result.stderr)
        self.write_mras(True)
        (self.kit / "definitions/ffex2u.cpp.inc").unlink()
        result = self.run_apply("--disc-us", str(self.discs[1]), "--platform", "hbmame", "--out-dir", str(out))
        self.assertIn("definitions/ffex2u.cpp.inc is missing", result.stderr)
        self.assertFalse((self.kit / "calls.json").exists() or out.exists())

    def test_missing_disc_module_and_inputs_fail_clearly(self):
        out = str(self.kit.parent / "out")
        result = self.run_apply("--out-dir", out)
        self.assertIn("give --disc-us", result.stderr)
        result = self.run_apply("--disc-us", str(self.kit.parent / "missing.bin"), "--out-dir", out)
        self.assertIn("is not a file", result.stderr)
        (self.kit / "ffex_kit.py").unlink()
        result = self.run_apply("--disc-us", str(self.discs[1]), "--out-dir", out)
        self.assertIn("ffex_kit.py is missing", result.stderr)
        (self.roms / "sfa3u.zip").unlink()
        result = self.run_apply("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sfa3u.bin", result.stderr)


if __name__ == "__main__":
    unittest.main()
