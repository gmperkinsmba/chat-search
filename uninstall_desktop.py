#!/usr/bin/env python3
"""Remove only this product's marked installed app bundle."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from build_desktop import APP_NAME, owned_bundle


DESTINATION = Path.home() / "Applications" / f"{APP_NAME}.app"


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("Uninstall the native application on macOS.")
    if not DESTINATION.exists():
        print("No installed app was found.")
        return 0
    if not owned_bundle(DESTINATION):
        raise SystemExit(f"Refusing to remove an app without the expected identity and ownership marker: {DESTINATION}")
    shutil.rmtree(DESTINATION)
    print(f"Removed {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
