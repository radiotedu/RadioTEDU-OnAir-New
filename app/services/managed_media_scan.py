"""Complete managed-folder inventories: errors never mean an empty library."""
import os
import stat
from pathlib import Path


def scan_managed_media(root, *, recursive, extensions):
    root = Path(root).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f'Managed folder is unavailable: {root}')
    def failed(error):
        raise error
    def paths():
        if recursive:
            for folder, directories, files in os.walk(root, onerror=failed, followlinks=False):
                directories[:] = [d for d in directories if d != '.radiotedu-trash']
                for name in files:
                    yield Path(folder) / name
        else:
            yield from root.iterdir()
    result = []
    for path in paths():
        if path.suffix.lower() not in extensions or path.is_symlink():
            continue
        metadata = path.stat()
        if stat.S_ISREG(metadata.st_mode):
            result.append((path, metadata))
    # Catch a disappearing external drive even if iteration returned no files.
    root.stat()
    return result
