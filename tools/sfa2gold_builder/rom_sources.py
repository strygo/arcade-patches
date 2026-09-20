"""Shared standalone ROM discovery for IPS, Gold and Final Fight EX kits.

This is the authoritative source. Export with sync_kit_tools.py; bundled copies
must be byte-identical. Only Python's standard library is required for ZIPs.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from pathlib import Path

QSOUND = {"name": "dl-1425.bin", "size": 24576, "crc32": "d6cf5ef5",
          "sha1": "555f50fe5cdf127619da7d854c03f4a244a0c501", "role": "device"}


class RomError(ValueError):
    pass


def fingerprint(name, data, role="game"):
    return {"name": name, "size": len(data), "crc32": f"{zlib.crc32(data) & 0xffffffff:08x}",
            "sha256": hashlib.sha256(data).hexdigest(), "role": role}


def verify(data, spec):
    return (len(data) == spec["size"]
            and (not spec.get("crc32") or f"{zlib.crc32(data) & 0xffffffff:08x}" == spec["crc32"].lower())
            and all(hashlib.new(kind, data).hexdigest() == spec[kind].lower()
                    for kind in ("sha256", "sha1") if spec.get(kind)))


def find_7z():
    for name in ("7zz", "7z", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            return found
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(key):
            path = Path(os.environ[key]) / "7-Zip/7z.exe"
            if path.is_file():
                return str(path)
    return None


def search_paths(target=None, rompaths=(), kit=None):
    paths = ([Path(target)] if target else []) + [Path(p) for p in rompaths]
    if target and Path(target).is_file():
        paths.append(Path(target).parent)
    paths.extend(Path(p) for p in os.environ.get("CAPCOM_ARCADE_ROM_PATH", "").split(os.pathsep) if p)
    for root in (Path.cwd(), Path(kit) if kit else Path.cwd()):
        paths.extend((root / "roms", root / "roms/mame0260"))
    return list(dict.fromkeys(p.expanduser().resolve() for p in paths))


class Resolver:
    """Resolve by identity, never by first matching basename. No persistent cache."""

    def __init__(self, paths, hints=(), exclude=(), progress=None):
        self.paths = list(dict.fromkeys(Path(p).expanduser().resolve() for p in paths))
        self.hints = tuple(h.lower() for h in hints)
        self.exclude = [Path(p).resolve() for p in exclude]
        self.progress = progress or (lambda message: None)
        self.provenance = {}
        self.diagnostics = []
        self._cache = {}
        self._indexes = {}

    def _allowed(self, path):
        real = path.resolve()
        return not any(real == p or p in real.parents for p in self.exclude)

    def _candidates(self):
        seen = set()
        # Try explicit archives and hinted sibling archives before walking roots.
        for path in self.paths:
            if path.is_file() and self._allowed(path) and path not in seen:
                seen.add(path)
                yield path
        for root in self.paths:
            if root.is_dir():
                for hint in self.hints:
                    for ext in (".zip", ".7z"):
                        path = root / (hint + ext)
                        if path.is_file() and self._allowed(path) and path.resolve() not in seen:
                            seen.add(path.resolve())
                            yield path
        for root in self.paths:
            if not root.is_dir():
                continue
            self.progress(f"Searching ROM files in {root}")
            for directory, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not Path(directory, d).is_symlink()
                                 and self._allowed(Path(directory, d)))
                for name in sorted(names):
                    path = Path(directory, name)
                    real = path.resolve()
                    if path.is_symlink() and root != real and root not in real.parents:
                        continue
                    if real not in seen and self._allowed(path):
                        seen.add(real)
                        yield path

    def _index(self, path):
        if path in self._indexes:
            return self._indexes[path]
        entries = []
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                entries = [(i, i.filename, i.file_size, f"{i.CRC:08x}")
                           for i in archive.infolist() if not i.is_dir()]
        elif path.suffix.lower() == ".7z":
            exe = find_7z()
            if not exe:
                raise RomError(f"{path}: reading 7z requires 7-Zip (or provide ZIP/loose files)")
            result = subprocess.run([exe, "l", "-slt", "-ba", "--", str(path)],
                                    capture_output=True, text=True, errors="replace", timeout=120)
            if result.returncode:
                raise RomError(f"Cannot list {path}: {result.stderr.strip()}")
            for block in result.stdout.replace("\r\n", "\n").split("\n\n"):
                fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
                if "Path" in fields and fields.get("Folder") != "+" and "Size" in fields:
                    entries.append((fields["Path"], fields["Path"], int(fields["Size"]),
                                    fields.get("CRC", "").lower()))
        else:
            entries = [(None, path.name, path.stat().st_size, "")]
        self._indexes[path] = entries
        return entries

    def _read(self, path, entry):
        member, _, size, _ = entry
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                return archive.read(member)
        if path.suffix.lower() == ".7z":
            # -spd disables wildcard matching; stdout avoids archive path extraction.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run([find_7z(), "e", "-so", "-spd", "--", str(path), member],
                                        stdout=output, stderr=subprocess.PIPE, timeout=180)
                if result.returncode:
                    raise RomError(f"Cannot read {path}:{member}: {result.stderr.decode(errors='replace')}")
                if output.tell() != size:
                    raise RomError(f"Unexpected extracted size: {path}:{member}")
                output.seek(0)
                return output.read()
        return path.read_bytes()

    def resolve(self, requirements, optional=False, embedded_only=False):
        specs = {}
        for spec in requirements:
            name = spec["name"]
            if Path(name).name != name or "\\" in name or name in (".", ".."):
                raise RomError(f"Invalid canonical ROM name: {name}")
            if not isinstance(spec.get("size"), int) or spec["size"] < 0 or not (spec.get("sha256") or spec.get("sha1")):
                raise RomError(f"ROM identity requires size and a strong hash: {name}")
            if name in specs and specs[name] != spec:
                raise RomError(f"Conflicting required identities: {name}")
            specs[name] = spec
        embedded = set(Path(v["path"]) for v in self.provenance.values())
        # An extracted set is equivalent to its archive: include loose siblings.
        for source in tuple(embedded):
            if source.suffix.lower() not in (".zip", ".7z"):
                embedded.update(p for p in source.parent.iterdir()
                                if p.is_file() and not p.is_symlink()
                                and p.suffix.lower() not in (".zip", ".7z") and self._allowed(p))
        found = {}
        for name, spec in specs.items():
            key = (spec["size"], spec.get("sha256"), spec.get("sha1"))
            if key in self._cache:
                data, source = self._cache[key]
                if (not embedded_only or Path(source["path"]) in embedded) and verify(data, spec):
                    found[name] = data
                    self.provenance[name] = source
        candidates = self._candidates()
        if embedded_only:
            candidates = iter(sorted(embedded))
        for path in candidates:
            if len(found) == len(specs):
                break
            try:
                entries = self._index(path)
                for entry in entries:
                    wanted = [s for n, s in specs.items() if n not in found and s["size"] == entry[2]
                              and (not entry[3] or not s.get("crc32") or s["crc32"].lower() == entry[3])]
                    if not wanted:
                        continue
                    try:
                        data = self._read(path, entry)
                    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                        self.diagnostics.append(str(exc))
                        continue
                    for spec in wanted:
                        if verify(data, spec):
                            name = spec["name"]
                            source = {"path": str(path), "member": entry[1],
                                      "sha256": hashlib.sha256(data).hexdigest()}
                            found[name] = data
                            self.provenance[name] = source
                            self._cache[(spec["size"], spec.get("sha256"), spec.get("sha1"))] = (data, source)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                self.diagnostics.append(str(exc))
        missing = [s for n, s in specs.items() if n not in found]
        if missing and not optional:
            lines = ["Required stock ROM content is missing:"]
            lines.extend(f"  {s['name']}: {s['size']} bytes, "
                         f"{('CRC ' + s['crc32']) if s.get('crc32') else ('SHA-256 ' + s.get('sha256', ''))}"
                         for s in missing)
            lines.append("Searched: " + ", ".join(map(str, self.paths)))
            lines.append("Provide the required revision in any ZIP, 7z or extracted folder; filenames need not match.")
            lines.extend(dict.fromkeys(self.diagnostics[-8:]))
            raise RomError("\n".join(lines))
        return found

    def devices(self, include=False):
        result = self.resolve([QSOUND], optional=not include, embedded_only=not include)
        if not result:
            named = any(Path(entry[1]).name.lower() == QSOUND["name"]
                        for entries in self._indexes.values() for entry in entries)
            if named:
                self.progress("Ignoring unrecognized dl-1425.bin: its contents do not match QSound firmware.")
            self.progress("QSound firmware not included; provide it separately in your emulator ROM path.")
        return result


def write_zip(path, members):
    """Canonical game output; complete temporary archive replaces destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".rom-", suffix=".zip", dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(members.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_zip(root / "parent.zip", {"child/a": b"valid", "a": b"wrong"})
        spec = fingerprint("a", b"valid")
        assert Resolver([root]).resolve([spec]) == {"a": b"valid"}
    print("ROM resolver self-test passed")


def publish_tree(destination, outputs):
    """Stage all artifacts, then replace with rollback on ordinary I/O failures."""
    destination = Path(destination)
    if destination.is_symlink():
        raise RomError("Output directory must not be a symlink")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".kit-export-", dir=destination.parent) as tmp:
        stage, backup = Path(tmp) / "stage", Path(tmp) / "backup"
        for name, value in outputs.items():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or "\\" in name:
                raise RomError(f"Unsafe output path: {name}")
            target = destination / rel
            if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                raise RomError(f"Output symlinks are not supported: {target}")
            path = stage / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, dict):
                write_zip(path, value)
            else:
                path.write_bytes(value)
        installed, saved = [], []
        try:
            for name in outputs:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    old = backup / name
                    old.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, old)
                    saved.append(name)
                os.replace(stage / name, target)
                installed.append(name)
        except BaseException:
            for name in reversed(installed):
                (destination / name).unlink()
            for name in reversed(saved):
                os.replace(backup / name, destination / name)
            raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.parse_args()
    self_test()
