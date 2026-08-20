from __future__ import annotations

import argparse
import json

from app.services.media_mirror import media_mirror_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or verify an atomic RadioTEDU media mirror")
    parser.add_argument("source", help="Verified source media root")
    parser.add_argument("destination", help="Destination media root")
    parser.add_argument("--check", action="store_true", help="Compare only; do not modify the destination")
    args = parser.parse_args()
    expected = media_mirror_service.manifest(args.source)
    result = media_mirror_service.compare(expected, args.destination) if args.check else media_mirror_service.synchronize(args.source, args.destination)
    print(json.dumps({key: value for key, value in result.items() if key != "actual"}, indent=2))
    return 0 if result.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
