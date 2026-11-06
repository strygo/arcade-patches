"""Concise standalone errors, with full diagnostics available through --debug."""
import json
import os
import struct
import subprocess
import sys
import zipfile


def message(exc):
    if isinstance(exc,ModuleNotFoundError):
        return ("Required Python component '"+str(exc.name)+"' is unavailable. Use the Arcade Patches builder to prepare dependencies, "
                'or follow the Python setup instructions in this kit. Interpreter: '+sys.executable)
    if isinstance(exc,FileNotFoundError):
        return 'File not found: '+str(exc.filename or exc)+'. Check the path; restore missing kit files from the original ZIP.'
    if isinstance(exc,PermissionError):
        return 'Cannot access '+str(exc.filename or 'the selected path')+'. Choose a writable output/cache folder and check file permissions.'
    if isinstance(exc,json.JSONDecodeError):
        return 'A JSON configuration or kit metadata file is invalid. Check edited configuration files, or extract a fresh copy of the kit.'
    if isinstance(exc,zipfile.BadZipFile):
        return 'The selected ZIP is invalid or incomplete. Extract any outer download archive and select a valid original ROM ZIP.'
    if isinstance(exc,struct.error):
        return 'An input file is truncated or has an unsupported format. Check that you selected the original disc/ROM image and the correct input type.'
    if isinstance(exc,subprocess.CalledProcessError):
        tool=exc.cmd[0] if isinstance(exc.cmd,(tuple,list)) else exc.cmd
        return str(tool)+' failed (exit '+str(exc.returncode)+'). Check the preceding tool message and the source input.'
    if isinstance(exc,subprocess.TimeoutExpired):
        return 'A required tool timed out. Check the source input, available storage and tool installation.'
    return str(exc).strip() or type(exc).__name__


def install():
    def hook(kind,exc,trace):
        if '--debug' in sys.argv:
            sys.__excepthook__(kind,exc,trace)
        elif isinstance(exc,KeyboardInterrupt):
            print('Cancelled.',file=sys.stderr)
        else:
            print('ERROR: '+message(exc),file=sys.stderr)
            print('Run the same command with --debug for diagnostic details.',file=sys.stderr)
    sys.excepthook=hook
