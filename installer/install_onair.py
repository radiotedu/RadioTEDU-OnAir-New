"""Install a separate version without stopping or editing an existing broadcast."""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Install RadioTEDU OnAir in a separate directory')
    parser.add_argument('--prefix', type=Path, default=Path.home() / 'RadioTEDU-OnAir')
    parser.add_argument('--check', action='store_true', help='Check prerequisites without writing an installation')
    parser.add_argument('--no-deps', action='store_true', help='Offline staging only; install locked dependencies before starting')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error('The source installer requires Python 3.12. Portable binaries include their own Python.')
    if args.check:
        print('Python 3.12: OK')
        for name in ('ffmpeg', 'ffprobe'):
            print(f'{name}: ' + ('OK' if shutil.which(name) else 'Install or select an explicit binary path'))
        print('Target: ' + str(args.prefix.expanduser().resolve()))
        return
    source = Path(__file__).resolve().parents[1]
    prefix = args.prefix.expanduser().resolve()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    version = (source / 'VERSION').read_text(encoding='utf-8').strip()
    release = prefix / 'versions' / (version + '-' + stamp)
    release.mkdir(parents=True, exist_ok=False)
    ignored = shutil.ignore_patterns('.git', '.venv', '.tmp', '__pycache__', '.test-output', 'node_modules', '*.db', '*.db-*', '.env', '*.key')
    for folder in ('app', 'scripts', 'installer'):
        shutil.copytree(source / folder, release / folder, ignore=ignored)
    for filename in ('onair_portable.py', 'run_cleanroom.py', 'requirements.lock', 'VERSION', 'LICENSE.md'):
        shutil.copy2(source / filename, release / filename)
    venv.EnvBuilder(with_pip=True).create(release / '.venv')
    python = release / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not args.no_deps:
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(release / 'requirements.lock')], check=True)
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    if os.name == 'nt':
        launcher = prefix / ('Start-OnAir-' + stamp + '.cmd')
        launcher.write_text(f'@echo off\r\n"{python}" "{release / "onair_portable.py"}" %*\r\n', encoding='utf-8')
    else:
        import shlex
        launcher = prefix / ('start-onair-' + stamp + '.sh')
        launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' ' + shlex.quote(str(release / 'onair_portable.py')) + ' "$@"\n', encoding='utf-8')
        launcher.chmod(0o700)
    print('Installed separately: ' + str(release))
    print('Launcher: ' + str(launcher))
    print('Existing services, databases and broadcasts were not modified.')


if __name__ == '__main__':
    main()
