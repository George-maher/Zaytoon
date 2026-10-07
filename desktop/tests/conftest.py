"""Make the ``app`` package importable from the desktop test suite.

The desktop application is a standalone package with its own import root, so
its tests need ``desktop/`` on ``sys.path`` exactly the way ``desktop/main.py``
does.
"""

import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = DESKTOP_DIR.parent

for path in (str(DESKTOP_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
