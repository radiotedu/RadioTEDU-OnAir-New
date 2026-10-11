"""Portable browser-based OnAir launcher; never imports a production instance."""
import argparse
import json
import os
import shutil
import socket
import sys
from pathlib import Path


def platform_root():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/RadioTEDU/OnAir'
    if os.name == 'nt':
        return Path(os.getenv('LOCALAPPDATA', str(Path.home()))) / 'RadioTEDU/OnAir'
    return Path(os.getenv('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'radiotedu/onair'


def configure_environment(data_root=None):
    root = Path(data_root or os.getenv('CLEANROOM_DATA_ROOT') or platform_root()).expanduser().resolve()
    if data_root is not None:
        # Explicit isolation must override inherited production-service paths.
        os.environ['CLEANROOM_DATA_ROOT'] = str(root)
        os.environ['CLEANROOM_DB_PATH'] = str(root / 'cleanroom.db')
        os.environ['CLEANROOM_USER_CONFIG_ROOT'] = str(root / 'config')
        os.environ['CLEANROOM_CREDENTIAL_STORE_FILE'] = str(root / 'config/secrets/station-credentials.json')
        os.environ['CLEANROOM_CREDENTIAL_KEY_FILE'] = str(root / 'config/secrets/credential-encryption.key')
        os.environ['CLEANROOM_JWT_SECRET_FILE'] = str(root / 'config/secrets/jwt-secret.key')
    os.environ.setdefault('CLEANROOM_DATA_ROOT', str(root))
    os.environ.setdefault('CLEANROOM_DB_PATH', str(root / 'cleanroom.db'))
    os.environ.setdefault('CLEANROOM_USER_CONFIG_ROOT', str(root / 'config'))
    os.environ.setdefault('CLEANROOM_PORT', '18110')
    if os.name != 'nt':
        os.environ.setdefault('CLEANROOM_CREDENTIAL_BACKEND', 'fernet')
    return root


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {'station-worker-process', 'rotate-jwt-secret'}:
        configure_environment()
        from run_cleanroom import main as run
        return run()
    parser = argparse.ArgumentParser(description='RadioTEDU OnAir portable launcher')
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--host', default=os.getenv('CLEANROOM_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('CLEANROOM_PORT', '18110')))
    parser.add_argument('--ffmpeg', type=Path)
    parser.add_argument('--ffprobe', type=Path)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    root = configure_environment(args.data_root)
    if args.ffmpeg:
        os.environ['RADIOTEDU_FFMPEG_PATH'] = str(args.ffmpeg.resolve())
    if args.ffprobe:
        os.environ['RADIOTEDU_FFPROBE_PATH'] = str(args.ffprobe.resolve())
    from app.runtime_paths import resolve_binary
    binaries = {name: bool(resolve_binary(name)) for name in ('ffmpeg', 'ffprobe')}
    if args.check:
        print(json.dumps({'data_root': str(root), 'port': args.port, 'binaries': binaries,
                          'credential_provider': 'DPAPI' if os.name == 'nt' else 'owner-only Fernet key',
                          'python': sys.version.split()[0]}))
        return 0 if all(binaries.values()) else 2
    if not all(binaries.values()):
        parser.error('Install FFmpeg/ffprobe or provide --ffmpeg and --ffprobe. No codec fallback is performed.')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    with socket.socket() as probe:
        try:
            probe.bind((args.host, args.port))
        except OSError:
            parser.error('The requested address is already in use. Existing broadcasts were not changed.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['CLEANROOM_HOST'] = args.host
    os.environ['CLEANROOM_PORT'] = str(args.port)
    os.environ['CLEANROOM_OPEN_PANEL'] = 'false' if args.no_browser else 'true'
    sys.argv = [sys.argv[0]]
    from run_cleanroom import main as run
    run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
