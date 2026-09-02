#!/usr/bin/env python3
"""List and extract Human68k FAT12 files from X68000 DIM/XDF images.

X68000 2HD images use a compact, big-endian BPB followed by an otherwise
ordinary FAT12 filesystem.  XDF stores the filesystem at byte zero; DIM adds
a 256-byte ``DIFC HEADER``.  This tool deliberately supports the filesystem
subset used by the original game disks rather than depending on a host mount.

Examples::

    python3 x68k_disk.py list disk.dim
    python3 x68k_disk.py extract disk.xdf -o work/disk
    python3 x68k_disk.py --self-test
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import struct
import sys
from pathlib import Path, PurePosixPath


class DiskError(ValueError):
    """The image is not a supported or internally consistent Human68k disk."""


@dataclasses.dataclass(frozen=True)
class Geometry:
    image_offset: int
    bytes_per_sector: int
    sectors_per_cluster: int
    fat_count: int
    reserved_sectors: int
    root_entries: int
    total_sectors: int
    media: int
    sectors_per_fat: int

    @property
    def fat_offset(self) -> int:
        return self.image_offset + self.reserved_sectors * self.bytes_per_sector

    @property
    def root_offset(self) -> int:
        return self.fat_offset + (
            self.fat_count * self.sectors_per_fat * self.bytes_per_sector
        )

    @property
    def root_size(self) -> int:
        return self.root_entries * 32

    @property
    def data_offset(self) -> int:
        root_sectors = math.ceil(self.root_size / self.bytes_per_sector)
        return self.root_offset + root_sectors * self.bytes_per_sector

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster


@dataclasses.dataclass(frozen=True)
class Entry:
    path: PurePosixPath
    attr: int
    first_cluster: int
    size: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & 0x10)


def _be16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _geometry_at(data: bytes, image_offset: int) -> Geometry:
    # Human68k's BPB begins at boot-sector byte 0x12.  Unlike DOS FAT BPBs,
    # its 16-bit fields are big-endian (the host CPU is a Motorola 68000) and
    # sectors-per-FAT occupies one byte.
    bpb = image_offset + 0x12
    if len(data) < bpb + 12:
        raise DiskError("image is too short for a Human68k BPB")
    geometry = Geometry(
        image_offset=image_offset,
        bytes_per_sector=_be16(data, bpb),
        sectors_per_cluster=data[bpb + 2],
        fat_count=data[bpb + 3],
        reserved_sectors=_be16(data, bpb + 4),
        root_entries=_be16(data, bpb + 6),
        total_sectors=_be16(data, bpb + 8),
        media=data[bpb + 10],
        sectors_per_fat=data[bpb + 11],
    )
    if geometry.bytes_per_sector not in (256, 512, 1024, 2048, 4096):
        raise DiskError(f"invalid sector size {geometry.bytes_per_sector}")
    if not 1 <= geometry.sectors_per_cluster <= 64:
        raise DiskError("invalid sectors-per-cluster")
    if not 1 <= geometry.fat_count <= 4:
        raise DiskError("invalid FAT count")
    if not geometry.reserved_sectors or not geometry.root_entries:
        raise DiskError("missing reserved sectors or root directory")
    if not geometry.total_sectors or not geometry.sectors_per_fat:
        raise DiskError("missing disk or FAT size")
    filesystem_end = (
        geometry.image_offset
        + geometry.total_sectors * geometry.bytes_per_sector
    )
    if filesystem_end > len(data):
        raise DiskError(
            f"BPB claims {filesystem_end} bytes but image has {len(data)}"
        )
    if geometry.data_offset >= filesystem_end:
        raise DiskError("filesystem metadata consumes the whole image")
    return geometry


def detect_geometry(data: bytes) -> Geometry:
    """Detect raw XDF or 256-byte-header DIM geometry."""
    errors: list[str] = []
    for image_offset in (0, 0x100):
        try:
            geometry = _geometry_at(data, image_offset)
            fat = data[geometry.fat_offset : geometry.fat_offset + 3]
            if len(fat) != 3 or fat[0] != geometry.media or fat[1:] != b"\xff\xff":
                raise DiskError("FAT media signature does not match the BPB")
            return geometry
        except DiskError as exc:
            errors.append(f"offset 0x{image_offset:x}: {exc}")
    # Some commercial IPLs (notably Street Fighter II's ``X68IPL30``) use
    # their own boot parameter block and therefore do not retain Human.sys's
    # BPB at 0x12.  The rest of the disk is still the canonical 77-cylinder
    # 2HD FAT12 layout.  Accept that exact geometry only when both the image
    # length and FAT signature independently agree; this is intentionally not
    # a guess for arbitrary damaged images.
    canonical_size = 1232 * 1024
    for image_offset in (0, 0x100):
        if len(data) != image_offset + canonical_size:
            continue
        geometry = Geometry(
            image_offset=image_offset,
            bytes_per_sector=1024,
            sectors_per_cluster=1,
            fat_count=2,
            reserved_sectors=1,
            root_entries=192,
            total_sectors=1232,
            media=0xFE,
            sectors_per_fat=2,
        )
        if data[geometry.fat_offset : geometry.fat_offset + 3] == b"\xfe\xff\xff":
            return geometry
    raise DiskError("not a supported XDF/DIM image (" + "; ".join(errors) + ")")


def _decode_name(raw: bytes) -> str:
    # Human68k filenames are Shift-JIS.  Replacement is preferable to losing
    # an otherwise extractable file because of one damaged directory byte.
    return raw.rstrip(b" ").decode("shift_jis", errors="replace")


def _fat12_next(fat: bytes, cluster: int) -> int:
    offset = cluster + cluster // 2
    if offset + 1 >= len(fat):
        raise DiskError(f"cluster {cluster} indexes beyond the FAT")
    pair = fat[offset] | (fat[offset + 1] << 8)
    return (pair >> 4) & 0xFFF if cluster & 1 else pair & 0xFFF


class Human68kDisk:
    def __init__(self, data: bytes, source: str = "<memory>") -> None:
        self.data = data
        self.source = source
        self.geometry = detect_geometry(data)
        fat_size = self.geometry.sectors_per_fat * self.geometry.bytes_per_sector
        self.fat = data[self.geometry.fat_offset : self.geometry.fat_offset + fat_size]

    @classmethod
    def open(cls, path: Path) -> "Human68kDisk":
        return cls(path.read_bytes(), str(path))

    def _cluster(self, cluster: int) -> bytes:
        if cluster < 2:
            raise DiskError(f"invalid data cluster {cluster}")
        offset = self.geometry.data_offset + (cluster - 2) * self.geometry.cluster_size
        end = offset + self.geometry.cluster_size
        filesystem_end = (
            self.geometry.image_offset
            + self.geometry.total_sectors * self.geometry.bytes_per_sector
        )
        if end > filesystem_end:
            raise DiskError(f"cluster {cluster} lies beyond the filesystem")
        return self.data[offset:end]

    def _chain(self, first_cluster: int) -> bytes:
        out = bytearray()
        seen: set[int] = set()
        cluster = first_cluster
        while 2 <= cluster < 0xFF8:
            if cluster in seen:
                raise DiskError(f"FAT cycle at cluster {cluster} in {self.source}")
            seen.add(cluster)
            out.extend(self._cluster(cluster))
            cluster = _fat12_next(self.fat, cluster)
            if cluster == 0xFF7:
                raise DiskError(f"bad cluster in chain from {first_cluster}")
            if cluster < 2:
                raise DiskError(
                    f"free/reserved cluster {cluster} in chain from {first_cluster}"
                )
        return bytes(out)

    @staticmethod
    def _directory_records(blob: bytes, parent: PurePosixPath) -> list[Entry]:
        entries: list[Entry] = []
        for pos in range(0, len(blob) - 31, 32):
            raw = blob[pos : pos + 32]
            if raw[0] == 0x00:
                break
            if raw[0] == 0xE5:
                continue
            if raw[0] == 0x05:
                # FAT escapes a real leading 0xE5 byte as 0x05 so it cannot
                # be confused with the deleted-entry marker.
                raw = b"\xe5" + raw[1:]
            attr = raw[11]
            if attr == 0x0F or attr & 0x08:  # LFN or volume label
                continue
            stem = _decode_name(raw[:8])
            ext = _decode_name(raw[8:11])
            name = stem + (("." + ext) if ext else "")
            if name in (".", ".."):
                continue
            if not name or "/" in name or "\\" in name:
                raise DiskError(f"unsafe directory name {name!r}")
            entries.append(Entry(
                path=parent / name,
                attr=attr,
                first_cluster=struct.unpack_from("<H", raw, 26)[0],
                size=struct.unpack_from("<I", raw, 28)[0],
            ))
        return entries

    def entries(self) -> list[Entry]:
        root = self.data[
            self.geometry.root_offset : self.geometry.root_offset + self.geometry.root_size
        ]
        output: list[Entry] = []

        def walk(blob: bytes, parent: PurePosixPath, ancestors: set[int]) -> None:
            for entry in self._directory_records(blob, parent):
                output.append(entry)
                if entry.is_dir:
                    if entry.first_cluster in ancestors:
                        raise DiskError(f"directory cycle at {entry.path}")
                    walk(
                        self._chain(entry.first_cluster),
                        entry.path,
                        ancestors | {entry.first_cluster},
                    )

        walk(root, PurePosixPath(), set())
        return output

    def read(self, entry: Entry) -> bytes:
        if entry.is_dir:
            raise IsADirectoryError(str(entry.path))
        if entry.size == 0:
            return b""
        if entry.first_cluster < 2:
            raise DiskError(f"non-empty file {entry.path} has no data cluster")
        content = self._chain(entry.first_cluster)
        if len(content) < entry.size:
            raise DiskError(
                f"short cluster chain for {entry.path}: {len(content)} < {entry.size}"
            )
        return content[: entry.size]

    def extract(self, destination: Path) -> list[dict[str, object]]:
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        catalog: list[dict[str, object]] = []
        for entry in self.entries():
            target = destination.joinpath(*entry.path.parts)
            resolved = target.resolve()
            if resolved != root and root not in resolved.parents:
                raise DiskError(f"unsafe extraction target {target}")
            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self.read(entry)
            target.write_bytes(content)
            catalog.append({
                "path": entry.path.as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "attributes": f"0x{entry.attr:02x}",
                "first_cluster": entry.first_cluster,
            })
        return catalog


def _synthetic_image(dim: bool = False) -> bytes:
    offset = 0x100 if dim else 0
    bps = 1024
    image = bytearray(offset + 16 * bps)
    if dim:
        image[0xAB:0xB6] = b"DIFC HEADER"
    bpb = offset + 0x12
    struct.pack_into(">H", image, bpb, bps)
    image[bpb + 2] = 1
    image[bpb + 3] = 2
    struct.pack_into(">H", image, bpb + 4, 1)
    struct.pack_into(">H", image, bpb + 6, 32)
    struct.pack_into(">H", image, bpb + 8, 16)
    image[bpb + 10] = 0xFE
    image[bpb + 11] = 1
    for fat_sector in (1, 2):
        fat = offset + fat_sector * bps
        image[fat : fat + 5] = b"\xfe\xff\xff\xff\x0f"  # cluster 2 -> EOC
    root = offset + 3 * bps
    image[root : root + 11] = b"TEST    TXT"
    image[root + 11] = 0x20
    struct.pack_into("<H", image, root + 26, 2)
    struct.pack_into("<I", image, root + 28, 5)
    image[offset + 4 * bps : offset + 4 * bps + 5] = b"hello"
    return bytes(image)


def self_test() -> None:
    for dim in (False, True):
        disk = Human68kDisk(_synthetic_image(dim), "synthetic")
        entries = disk.entries()
        assert len(entries) == 1
        assert entries[0].path.as_posix() == "TEST.TXT"
        assert disk.read(entries[0]) == b"hello"
        assert disk.geometry.image_offset == (0x100 if dim else 0)
    # Odd/even FAT12 extraction and an EOC marker.
    fat = bytes.fromhex("feffff034000ff0f")
    assert _fat12_next(fat, 2) == 3
    assert _fat12_next(fat, 3) == 4
    assert _fat12_next(fat, 4) == 0xFFF
    print("x68k_disk self-test: OK")


def _list_image(path: Path, as_json: bool) -> None:
    disk = Human68kDisk.open(path)
    records = [
        {
            "path": entry.path.as_posix(),
            "type": "directory" if entry.is_dir else "file",
            "size": entry.size,
            "attributes": f"0x{entry.attr:02x}",
            "first_cluster": entry.first_cluster,
        }
        for entry in disk.entries()
    ]
    if as_json:
        print(json.dumps({"image": str(path), "files": records}, indent=2))
        return
    print(f"{path}:")
    for record in records:
        suffix = "/" if record["type"] == "directory" else ""
        print(f"  {record['size']:9d}  {record['path']}{suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="list files in one or more images")
    list_parser.add_argument("images", nargs="+", type=Path)
    list_parser.add_argument("--json", action="store_true")
    extract_parser = sub.add_parser("extract", help="extract one image")
    extract_parser.add_argument("image", type=Path)
    extract_parser.add_argument("-o", "--output", required=True, type=Path)
    extract_parser.add_argument("--catalog", type=Path)
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.command == "list":
        for image in args.images:
            _list_image(image, args.json)
        return 0
    if args.command == "extract":
        disk = Human68kDisk.open(args.image)
        catalog = disk.extract(args.output)
        payload = {
            "image": str(args.image),
            "geometry": dataclasses.asdict(disk.geometry),
            "files": catalog,
        }
        if args.catalog:
            args.catalog.parent.mkdir(parents=True, exist_ok=True)
            args.catalog.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"extracted {len(catalog)} files from {args.image} to {args.output}")
        return 0
    parser.error("choose list or extract, or use --self-test")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DiskError, OSError) as exc:
        print(f"x68k_disk: error: {exc}", file=sys.stderr)
        raise SystemExit(1)
