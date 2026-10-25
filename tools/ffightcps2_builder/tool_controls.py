"""Shared command-line cache/offline controls for standalone recipient tools."""
import argparse
import os
from pathlib import Path


def add_arguments(parser):
    parser.add_argument('--tools-cache', type=Path, help='verified media/helper cache')
    parser.add_argument('--tools-bundle', type=Path, help='offline tools ZIP or directory')
    parser.add_argument('--offline', action='store_true', help='never download tools')


def configure(argv):
    parser = argparse.ArgumentParser(add_help=False)
    add_arguments(parser)
    args, rest = parser.parse_known_args(argv)
    for option, key in (('tools_cache', 'CPSPLUS_SWITCH_TOOLS_CACHE'),
                        ('tools_bundle', 'CPSPLUS_SWITCH_TOOLS_BUNDLE')):
        value = getattr(args, option)
        if value is not None:
            os.environ[key] = str(value.expanduser().resolve())
    if args.offline:
        os.environ['CPSPLUS_SWITCH_TOOLS_OFFLINE'] = '1'
    return rest
