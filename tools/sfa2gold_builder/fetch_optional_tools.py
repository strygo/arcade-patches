"""Authenticated optional media/disc helpers; never compile on recipient hosts."""
import os
import json
from pathlib import Path
import shutil

import fetch_switch_tools as common

HERE = Path(__file__).resolve().parent
COMPONENTS = {
    'x68k_sps_dec': 'extractors', 'm2seq22mid': 'converters',
    'sc55': 'sc55', 'mt32': 'munt', 'adlmidi': 'adlmidi',
    'chdman': 'chdman', 'sevenzip': 'sevenzip',
}
EXPECTED = {component: {tool for tool, owner in COMPONENTS.items() if owner == component}
            for component in set(COMPONENTS.values())}


def _resolve(tool, *, manifest=None, host=None):
    """Resolve only the requested component, with the shared cache/offline controls.

    A kit supplies its own trusted lock. Offline bundle metadata cannot change
    the lock or promote an unqualified producer. Explicit environment overrides
    remain the user's responsibility and never populate the verified cache.
    """
    common.require(tool in COMPONENTS, 'Unknown optional helper: '+tool)
    key = 'CPSPLUS_'+tool.upper()
    override = os.environ.get(key)
    if override:
        path = Path(override).expanduser()
        found = str(path.absolute()) if path.is_file() else shutil.which(override)
        common.require(found, key+' does not name a file: '+override)
        return Path(found)
    default = HERE/'optional_helpers.json'
    if not default.is_file():
        default = HERE.parent/'manifests/optional_helpers_v1.json'
    lock = json.loads(Path(manifest or default).read_text())
    common.require(lock.get('schema') == 1 and lock.get('components'), 'Invalid optional helper manifest')
    if manifest is None:
        common.require(lock.get('status') == 'qualified', 'Optional helpers have not completed qualification')
    component = COMPONENTS[tool]
    common.require(component in lock['components'], 'No qualified optional component: '+component)
    host = host or common.host_id()
    platforms = lock['components'][component]['platforms']
    common.require(host in platforms, 'No qualified '+component+' helper for '+host)
    record = platforms[host]
    common.require(set(record['executables']) == EXPECTED[component],
                   'Invalid optional helper record: '+component)
    return common.obtain(record, host, label=component)[tool]


def resolve(tool, *, manifest=None, host=None):
    try:
        return _resolve(tool, manifest=manifest, host=host)
    except common.SetupError as exc:
        raise ValueError(str(exc)) from exc


def disc_tool(tool, *, fetch=True):
    """Retain installed disc utilities, fetching a pinned fallback only on use.

    Discovery-only callers use fetch=False. MIDI/library helpers always use
    resolve() directly and cannot silently pick a different installed build.
    """
    if tool not in ('chdman', 'sevenzip'):
        raise ValueError('Unknown disc helper: '+tool)
    if os.environ.get('CPSPLUS_'+tool.upper()):
        return str(resolve(tool))
    names = ('chdman',) if tool == 'chdman' else ('7zz', '7z', '7za', '7zr')
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if tool == 'sevenzip' and os.name == 'nt':
        for key in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
            root = os.environ.get(key)
            if root and (Path(root)/'7-Zip/7z.exe').is_file():
                return str(Path(root)/'7-Zip/7z.exe')
        try:
            import winreg
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\7-Zip',
                                        0, winreg.KEY_READ | view) as key:
                        for field in ('Path64', 'Path'):
                            try:
                                path = Path(winreg.QueryValueEx(key, field)[0])/'7z.exe'
                                if path.is_file():return str(path)
                            except OSError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
    return str(resolve(tool)) if fetch else None
