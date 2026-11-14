"""Speech Experimental Test Suite."""

import sys
from pathlib import Path

# Ensure package root (/botworkspace/speech_experimental) is on sys.path
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
