"""Shared ISO/DVD-CHD normalization for standalone PS2 disc consumers."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile


@contextmanager
def dvd_iso(source, *, chdman=None, parent=None):
    """Decode DVD CHDs temporarily; preserve the producer's own source checks."""
    source=Path(source).expanduser().resolve()
    if not source.is_file():raise ValueError('Disc input not found: '+str(source))
    with source.open('rb') as stream:
        header=stream.read(124)
        stream.seek(0x8000);pvd=stream.read(7)
    if header[:8] != b'MComprHD':
        if pvd != b'\x01CD001\x01':
            raise ValueError('Expected a PS2 DVD ISO or DVD CHD: '+str(source)+'. Supply the original disc image; renaming an archive does not convert it.')
        yield source
        return
    if len(header)!=124 or int.from_bytes(header[12:16],'big')!=5:
        raise ValueError('Unsupported CHD header: '+str(source)+'. Use a CHD v5 DVD image or its original ISO.')
    size=int.from_bytes(header[32:40],'big')
    if not 0<size<=9*1024**3:raise ValueError('Invalid DVD size in CHD: '+str(source))
    from tool_controls import executable_path
    if chdman:
        tool=executable_path(chdman,('chdman','chdman.exe'),label='chdman')
    else:
        from fetch_optional_tools import disc_tool
        tool=disc_tool('chdman')
    base=Path(parent) if parent is not None else Path(tempfile.gettempdir())
    if shutil.disk_usage(base).free<size+1024**3:
        raise ValueError('Not enough temporary space to decode '+source.name+'. Free at least '+str((size+1024**3+1024**3-1)//1024**3)+' GiB in '+str(base)+'.')
    with tempfile.TemporaryDirectory(prefix='dvd-input-',dir=base) as folder:
        iso=Path(folder)/'disc.iso'
        print('Decoding DVD CHD: '+source.name,flush=True)
        try:
            result=subprocess.run([str(tool),'extractdvd','-i',str(source),'-o',str(iso)],
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=900)
        except subprocess.TimeoutExpired:
            raise ValueError('DVD decoding timed out for '+source.name+'. Check the disc and chdman installation.') from None
        if result.returncode:
            detail=(result.stderr or result.stdout).strip()[-1500:]
            raise ValueError('Could not decode '+source.name+' as a DVD CHD. CD/GD-ROM CHDs cannot be substituted for a PS2 DVD.\n'+detail)
        if not iso.is_file() or iso.stat().st_size!=size:
            raise ValueError('Decoded DVD size differs from its CHD: '+source.name+'. Verify or redump the source disc.')
        yield iso
