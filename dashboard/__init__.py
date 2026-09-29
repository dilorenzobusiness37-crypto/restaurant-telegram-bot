"""Web dashboard for the restaurant owner."""

import sys
from pathlib import Path

# Make the project root importable (bot.py, database.py) from any working directory.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
