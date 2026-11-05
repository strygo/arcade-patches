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


def native_pair(root, actual):
    """The builder-app pair is generated after release-kit.json, so it pins that file, not the reverse."""
    present = NATIVE & actual
    if not present:
        return
    require(present==NATIVE, 'Incomplete builder-app definition pair')
    files = json.loads((root/'manifest.json').read_text()).get('files')
    require(isinstance(files,dict) and set(files)==actual-NATIVE|{'release-kit.json'}, 'Builder-app manifest inventory differs')
    for name, expected in files.items():
        require(pin(local(root,name))['sha256']==expected, 'Builder-app manifest differs: '+name)


def validate(root):
    manifest = json.loads((root/'release-kit.json').read_text())
    require(set(manifest)=={'schema','files','names','core','release_final'} and manifest['schema']==1, 'Release kit schema')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=root/'release-kit.json'}
    native_pair(root, actual)
    actual-=NATIVE
    require(actual==set(manifest['files']), 'Complete release file inventory differs')
    require(not any(p.is_symlink() for p in root.rglob('*')), 'Release symlink forbidden')
    for name, expected in manifest['files'].items():
        require(pin(local(root,name))==expected, 'Changed release file: '+name)
    require(manifest['names'] in actual and manifest['core'] in actual, 'Pinned platform assets required')
    require(set(manifest['release_final'])=={'entrypoint','recipe'} and
            all(name in actual for name in manifest['release_final'].values()), 'Pinned final release assets required')
    return manifest


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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rompath',action='append',type=Path,required=True)
    for key in ('ps2-us','ps2-jp','naomi-chd','naomi-pic','chdman','work','out-dir'):
        parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--platform',choices=('all','hbmame','fbneo','mister'),default='all')
    args=parser.parse_args();root=Path(__file__).resolve().parent
    validate(root)
    require(not args.out_dir.exists() and args.out_dir.parent.is_dir(), 'Fresh output directory required')
    require(not args.work.exists() and args.work.parent.is_dir(), 'Fresh work directory required')
    require(not args.work.resolve().is_relative_to(root) and not args.out_dir.resolve().is_relative_to(root), 'Generated output must be outside the kit')
    args.work.mkdir();raw=args.work/'rebuilt'
    launcher="import runpy,sys;from pathlib import Path;p=Path(sys.argv.pop(1));sys.path.insert(0,str(p.parent));runpy.run_path(str(p),run_name='__main__')"
    command=[sys.executable,'-I','-B','-c',launcher,str(root/'builder/build.py'),'--work',str((args.work/'conversion').resolve()),'--out',str(raw.resolve())]
    for key in ('ps2_us','ps2_jp','naomi_chd','naomi_pic','chdman'):
        command+=['--'+key.replace('_','-'),str(getattr(args,key).resolve())]
    for path in args.rompath:command+=['--rompath',str(path.resolve())]
    subprocess.run(command,check=True)
    validate(root)
    selected=('hbmame','fbneo','mister') if args.platform=='all' else (args.platform,)
    postprocess(root,{'us':raw/'sfa3u.zip','jp':raw/'sfz3j.zip'},args.out_dir,selected)
    print('Upper platform files written to '+str(args.out_dir),flush=True)


if __name__=='__main__':main()
