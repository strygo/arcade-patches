#!/usr/bin/env python3
"""Rebuild Upper from original media and export its supported platform layouts."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import ExitStack
sys.path.insert(0,str(Path(__file__).resolve().parent))
from tool_controls import executable_path
sys.dont_write_bytecode = True


def require(value, message):
    if not value:
        raise ValueError(message)


def pin(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'bytes':path.stat().st_size,'sha256':digest}


def local(root, name):
    require(isinstance(name,str) and name and '\\' not in name, 'Portable release path required')
    p = PurePosixPath(name)
    require(not p.is_absolute() and all(s not in ('','.','..') for s in name.split('/')), 'Release path escape')
    path = root
    for part in p.parts:
        path /= part
        require(not path.is_symlink(), 'Release symlink forbidden')
    return path


NATIVE = {'manifest.json','collection.json'}


def require_inventory(expected, actual, label):
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    if not missing and not extra:
        return
    lines = [label + ' inventory differs.']
    for heading, names in (('Missing files', missing), ('Extra files', extra)):
        if names:
            lines.append(heading + ':')
            lines.extend('  ' + name for name in names[:20])
            if len(names) > 20:
                lines.append(f'  ... and {len(names) - 20} more')
    lines.append('Restore missing or modified kit files from the original ZIP. Your inputs, tools and output folders may stay alongside them.')
    raise ValueError('\n'.join(lines))


def native_pair(root, expected):
    """The builder-app pair is generated after release-kit.json, so it pins that file, not the reverse."""
    present = {name for name in NATIVE if (root/name).exists() or (root/name).is_symlink()}
    if not present:
        return
    require_inventory(NATIVE, present, 'Builder-app definition pair')
    files = json.loads(local(root,'manifest.json').read_text()).get('files')
    require(local(root,'collection.json').is_file(), 'Missing kit file: collection.json')
    require(isinstance(files,dict), 'Invalid builder-app manifest file inventory')
    require_inventory(files, set(expected)|{'release-kit.json'}, 'Builder-app manifest')
    for name, expected in files.items():
        path = local(root,name)
        require(path.is_file(), 'Missing kit file: '+name)
        require(pin(path)['sha256']==expected, 'Modified kit file: '+name+'. Restore it from the original ZIP.')


def validate(root):
    manifest = json.loads(local(root,'release-kit.json').read_text())
    require(isinstance(manifest,dict) and set(manifest)=={'schema','files','names','core','release_final'} and manifest['schema']==1, 'Release kit schema')
    require(isinstance(manifest['files'],dict), 'Invalid release kit file inventory')
    actual={name for name in manifest['files'] if local(root,name).is_file()}
    require_inventory(manifest['files'], actual, 'Release kit')
    native_pair(root, manifest['files'])
    for name, expected in manifest['files'].items():
        require(pin(local(root,name))==expected, 'Modified kit file: '+name+'. Restore it from the original ZIP.')
    require(all(isinstance(manifest[k],str) and manifest[k] in actual for k in ('names','core')), 'Pinned platform assets required')
    require(isinstance(manifest['release_final'],dict) and set(manifest['release_final'])=={'entrypoint','recipe'} and
            all(isinstance(name,str) and name in actual for name in manifest['release_final'].values()), 'Pinned final release assets required')
    return manifest


def resolve_chdman(path):
    names = ('chdman.exe','chdman') if os.name == 'nt' else ('chdman','chdman.exe')
    return executable_path(path,names,label='chdman')


def prepare_paths(args, root, manifest):
    """Check all destinations before creating a workspace or launching tools."""
    args.chdman = resolve_chdman(args.chdman)
    sources = [args.chdman]
    for key in ('ps2_us','ps2_jp','naomi_chd','naomi_pic'):
        path = getattr(args,key).resolve()
        require(path.is_file(), 'Missing input --'+key.replace('_','-')+': '+str(path))
        setattr(args,key,path)
        sources.append(path)
    for path in args.rompath:
        require(path.exists(), 'ROM path not found: '+str(path))
        sources.append(path.resolve())
    protected = sources + [local(root,name).resolve() for name in set(manifest['files'])|NATIVE|{'release-kit.json'}]
    destinations = []
    for key in ('work','out_dir'):
        original = getattr(args,key)
        path = original.resolve()
        require(not original.is_symlink() and not path.exists(), '--'+key.replace('_','-')+' already exists: '+str(original)+'. Choose a new directory.')
        require(path.parent.is_dir(), 'Parent directory does not exist: '+str(path.parent))
        require(not any(p == path or p.is_relative_to(path) for p in protected), 'Output would contain a kit file or source input: '+str(path))
        setattr(args,key,path)
        destinations.append(path)
    work, out = destinations
    require(not work.is_relative_to(out) and not out.is_relative_to(work), 'Work and output directories must be separate and must not contain each other.')


def check_python():
    require((3,12) <= sys.version_info[:2] <= (3,13),
            'The manual builder needs Python 3.12 or 3.13 for its pinned NumPy/SciPy dependencies. '
            'You can install it alongside your current Python. On Windows, run py -3.13 apply.py with the same arguments, '
            'or use the Arcade Patches builder app to prepare its own runtime.')
    command=[sys.executable,'-I','-B','-c',
             "import numpy, scipy; assert (numpy.__version__, scipy.__version__) == ('2.2.6', '1.15.3')"]
    try:
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError('Python dependency check timed out before reconstruction. Check this Python installation.') from None
    require(result.returncode == 0,
            'This Python needs NumPy 2.2.6 and SciPy 1.15.3. Install them for the interpreter running this command:\n'
            f'  "{sys.executable}" -m pip install numpy==2.2.6 scipy==1.15.3\n'
            'On Windows with Python 3.13, use py -3.13 -m pip install numpy==2.2.6 scipy==1.15.3 '
            'and py -3.13 apply.py with the same arguments. The work directory has not been created.')


def module(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def postprocess(root, archives, out, platforms=('hbmame','fbneo','mister')):
    """Maintainer oracle bridge; each source ZIP is checked by the naming API."""
    root=Path(root).resolve();out=Path(out).resolve();manifest=validate(root)
    require(set(archives)=={'us','jp'}, 'Both original rebuilt regions required')
    require(platforms and len(set(platforms))==len(platforms) and set(platforms)<={'hbmame','fbneo','mister'}, 'Unknown or duplicate platform')
    require(not out.exists() and out.parent.is_dir(), 'Fresh output with existing parent required')
    names=json.loads(local(root,manifest['names']).read_text())
    with tempfile.TemporaryDirectory(prefix='.upper-platforms-',dir=out.parent) as scratch:
        scratch=Path(scratch);named=scratch/'named';publish=scratch/'publish';publish.mkdir()
        naming=module(root/'platform_tools/names/public_kit_platform_names.py','upper_names')
        final=manifest['release_final']
        leaf=module(local(root,final['entrypoint']),'upper_release_final')
        recipe=json.loads(local(root,final['recipe']).read_text());corrected={}
        for region,path in archives.items():
            with zipfile.ZipFile(path) as archive:
                members=archive.namelist()
                require(len(members)==len(set(members))==26, 'Complete unambiguous regional archive required')
                data=leaf.apply_release({name:archive.read(name) for name in members},region,recipe)
            target=scratch/(region+'.zip')
            with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_STORED) as archive:
                for name,content in sorted(data.items()):
                    info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.external_attr=0o100644<<16
                    archive.writestr(info,content)
            corrected[region]=target
        naming.export_archives(names,corrected,named)
        for platform in ('hbmame','fbneo'):
            if platform in platforms:shutil.copytree(named/platform,publish/platform)
        if 'mister' in platforms:
            tools=root/'platform_tools/mister';previous=list(sys.path)
            try:
                sys.path.insert(0,str(tools))
                mister=module(tools/'public_kit_mister.py','upper_mister')
                mister.package(named/'hbmame',names,local(root,manifest['core']),publish/'mister')
            finally:sys.path[:]=previous
        validate(root)
        files={p.relative_to(publish).as_posix():pin(p) for p in sorted(publish.rglob('*')) if p.is_file()}
        (publish/'verification.json').write_text(json.dumps({'schema':1,'platforms':sorted(platforms),'files':files},sort_keys=True,indent=2)+'\n',newline='\n')
        require(not out.exists(), 'Output appeared during build')
        publish.rename(out)
    return files


def run_builder(command, work, debug=False):
    if debug:
        subprocess.run(command,check=True)
        return
    result=subprocess.run(command,stderr=subprocess.PIPE)
    detail=''
    if result.stderr:
        log=work/'reconstruction-error.log'
        log.write_bytes(result.stderr[-65536:])
        detail=' Details: '+str(log)
    if result.returncode:
        raise ValueError(f'Reconstruction failed (exit {result.returncode}).'+detail)
    if detail:
        print('Reconstruction diagnostics saved.'+detail,file=sys.stderr)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--debug',action='store_true',help='show diagnostic tracebacks')
    parser.add_argument('--rompath',action='append',type=Path,required=True)
    for key in ('ps2-us','ps2-jp','naomi-chd','naomi-pic','work','out-dir'):
        parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--chdman',type=Path,required=True,help='chdman executable, containing folder, or command on PATH')
    parser.add_argument('--platform',choices=('all','hbmame','fbneo','mister'),default='all')
    args=parser.parse_args();root=Path(__file__).resolve().parent
    manifest = validate(root)
    prepare_paths(args,root,manifest)
    check_python()
    args.work.mkdir();raw=args.work/'rebuilt'
    launcher="import runpy,sys;from pathlib import Path;p=Path(sys.argv.pop(1));sys.path.insert(0,str(p.parent));runpy.run_path(str(p),run_name='__main__')"
    command=[sys.executable,'-I','-B','-c',launcher,str(root/'builder/build.py'),'--work',str((args.work/'conversion').resolve()),'--out',str(raw.resolve())]
    from disc_sources import dvd_iso
    with ExitStack() as media:
        for key in ('ps2_us','ps2_jp','naomi_chd','naomi_pic','chdman'):
            path=getattr(args,key).resolve()
            if key in ('ps2_us','ps2_jp'):
                path=media.enter_context(dvd_iso(path,chdman=args.chdman,parent=args.work))
            command+=['--'+key.replace('_','-'),str(path)]
        for path in args.rompath:command+=['--rompath',str(path.resolve())]
        run_builder(command,args.work,args.debug)
    validate(root)
    selected=('hbmame','fbneo','mister') if args.platform=='all' else (args.platform,)
    postprocess(root,{'us':raw/'sfa3u.zip','jp':raw/'sfz3j.zip'},args.out_dir,selected)
    print('Upper platform files written to '+str(args.out_dir),flush=True)


if __name__=='__main__':
    try:
        main()
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        if '--debug' in sys.argv:
            raise
        raise SystemExit(f'ERROR: {exc}')
