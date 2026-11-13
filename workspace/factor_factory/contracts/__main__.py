"""`python -m contracts` -> self-check harness CLI (see selfcheck.py)."""
import sys

from .selfcheck import main

sys.exit(main())
