"""Make ``src/`` importable, matching how the original project runs.

Present so a task fails for the reason it is meant to test, rather than on an
import error that would be blamed on the model.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
