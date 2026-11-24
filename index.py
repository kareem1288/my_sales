"""Vercel entrypoint.

Vercel loads its entrypoint as a standalone file, not as part of a package, so
the app (which uses package-relative imports) is imported from here instead.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

from app.main import app  # noqa: E402

__all__ = ["app"]
