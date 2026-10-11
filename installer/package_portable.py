"""Archive a compiled portable release while preserving POSIX executable modes."""
import argparse
import re
import shutil
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--platform', choices=('Windows', 'Linux', 'macOS'), required=True)
    parser.add_argument('--arch', choices=('X64', 'ARM64'), required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[0-9A-Za-z.+-]+', version):
        parser.error('Invalid product version')
    executable = 'RadioTEDU-OnAir.exe' if args.platform == 'Windows' else 'RadioTEDU-OnAir'
    if not (root / executable).is_file():
        parser.error('Compiled portable executable is missing')
    (root / 'README-FIRST.txt').write_text(
        f'RadioTEDU OnAir {version} ({args.platform} {args.arch})\n\n'
        'Install FFmpeg and ffprobe with the exact encoders your station requires.\n'
        'Run Start-OnAir.cmd (Windows) or start-onair.sh (Linux/macOS).\n'
        'The browser panel opens at http://127.0.0.1:18110/.\n'
        'Existing services and occupied ports are not replaced.\n'
        'Use --port 18210 --data-root /separate/state for isolated commissioning.\n'
        'The first administrator password is generated in initial-admin-password.txt\n'
        'inside the instance data directory. Treat that file as private.\n'
        'Prepare/select the microphone in On Air; grant permission on localhost.\n'
        'See docs/PORTABLE_SETUP.md in the source repository for setup and backups.\n', encoding='utf-8')
    if args.platform == 'Windows':
        (root / 'Start-OnAir.cmd').write_text('@echo off\ncd /d "%~dp0"\n"RadioTEDU-OnAir.exe" %*\nif errorlevel 1 pause\n', encoding='utf-8')
    else:
        launcher = root / 'start-onair.sh'
        launcher.write_text('#!/bin/sh\ncd "$(dirname "$0")" || exit 1\nexec ./RadioTEDU-OnAir "$@"\n', encoding='utf-8')
        launcher.chmod(0o755)
    args.output.mkdir(parents=True, exist_ok=True)
    name = f'RadioTEDU-OnAir-{version}-{args.platform}-{args.arch}'
    if args.platform == 'Windows':
        archive = shutil.make_archive(str(args.output / name), 'zip', root_dir=root.parent, base_dir=root.name)
    else:
        archive = args.output / (name + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as handle:
            handle.add(root, arcname='RadioTEDU-OnAir')
    print(archive)


if __name__ == '__main__':
    main()
