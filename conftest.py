"""Make the tests import the checkout they sit in, not another one.

The package is installed editable, so site-packages carries a path entry
pointing at one particular checkout's ``src``. A worker running the suite
inside its own git worktree would therefore import Foreman from the
supervisor's checkout and test code it never wrote. Putting this
checkout's ``src`` first, from the directory this file is in, makes
``python -m pytest tests -q`` mean the same thing in every worktree.
"""

import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent / "src")
if sys.path[:1] != [SRC]:
    sys.path.insert(0, SRC)
