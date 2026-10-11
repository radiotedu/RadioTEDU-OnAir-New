"""Boot a compiled app on an isolated database without starting any broadcaster."""
import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('executable', type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    with tempfile.TemporaryDirectory(prefix='onair-package-check-') as directory:
        root = Path(directory)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ)
        for key in ('CLEANROOM_SKIP_WORKER_AUTOSTART', 'CLEANROOM_DISABLE_LIBRARY_WATCHER',
                    'CLEANROOM_DISABLE_PRODUCT_CATALOG', 'CLEANROOM_SKIP_STARTUP_AI',
                    'CLEANROOM_DISABLE_BPM_MAINTENANCE'):
            env[key] = '1'
        env['OPENBLAS_NUM_THREADS'] = '1'
        env['OMP_NUM_THREADS'] = '1'
        with (root / 'startup-private.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(
                [str(executable), '--data-root', str(root), '--port', str(port), '--no-browser'],
                env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 90
                health = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'Compiled app exited before readiness: {process.returncode}')
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health/live', timeout=2) as response:
                            health = json.load(response)
                        break
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        time.sleep(0.25)
                if health is None:
                    raise RuntimeError('Compiled app did not become ready within 90 seconds')
                expected = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
                if health.get('version') != expected:
                    raise RuntimeError('Compiled app reports an unexpected version')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/app', timeout=5) as response:
                    panel = response.read().decode('utf-8')
                if 'live-microphone.js' not in panel:
                    raise RuntimeError('Compiled app is missing microphone panel assets')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/static/onair/microphone-worklet.js', timeout=5) as response:
                    if 'radiotedu-microphone' not in response.read().decode('utf-8'):
                        raise RuntimeError('Compiled app is missing the microphone AudioWorklet')
                print(json.dumps({'packaged_http_startup': 'passed', 'version': expected,
                                  'worker_autostart': False, 'isolated_database': True}))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
