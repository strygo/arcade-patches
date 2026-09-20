"""Final Fight's declared source identities, using the shared kit resolver."""
from __future__ import annotations
import json
import sys
from pathlib import Path

TRACK = Path(__file__).resolve().parents[1]
try:
    import rom_sources
except ModuleNotFoundError:
    sys.path.insert(0, str(TRACK.parents[1] / 'release'))
    import rom_sources

_catalog_path = TRACK / 'rom_inputs.json'
if not _catalog_path.is_file():
    _catalog_path = TRACK.parents[1] / 'release/rom_inputs.json'
CATALOG = json.loads(_catalog_path.read_text())['sets']
_readers = {}
find_7z = rom_sources.find_7z


def read(romset: Path, stems, members) -> dict[str, bytes]:
    stems = tuple(stems)
    specs = []
    for name in members:
        spec = next((CATALOG[s][name] for s in stems if name in CATALOG.get(s, {})), None)
        if spec is None:
            raise rom_sources.RomError(f'Final Fight input identity is not declared: {stems}/{name}')
        specs.append(spec)
    paths = rom_sources.search_paths(romset, kit=TRACK)
    key = tuple(map(str, paths))
    if key not in _readers:
        _readers[key] = rom_sources.Resolver(paths, hints=('ffightu', 'ffightj', 'ffight'),
                                            exclude=[TRACK / 'work', TRACK / 'out'])
    return _readers[key].resolve(specs)


def extract(romset: Path, stems, members, dest: Path) -> None:
    dest = Path(dest)
    resolved = read(romset, stems, members)
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in resolved.items():
        (dest / name).write_bytes(data)
