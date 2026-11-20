"""Private pipe used by the local Canvas dev server; never log its connection output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from relay_core import resolve_home
from relay_images import ImageRelayManager


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source-path', action='store_true')
    mode.add_argument('--connection-json', action='store_true')
    args = parser.parse_args(arguments)
    try:
        home = args.home.expanduser().resolve() if args.home else resolve_home()
        source = home / 'relay-switcher' / 'image-profiles.dpapi'
        if args.source_path:
            result = str(source)
        else:
            result = ImageRelayManager(home).canvas_connection() if source.is_file() else None
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        # Credentials and backend exceptions must never enter Vite's terminal or browser logs.
        sys.stderr.write('Cannot read the applied Switcher image connection.\n')
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
