"""Entry point: `python3 -m factory ...`, or `python3 <checkout>/factory ...` as bin/factory runs it."""

import os
import sys

if not __package__:
    # Run as a directory: sys.path[0] is factory/ itself, where a module named
    # like a stdlib one would shadow it. Put the checkout root there instead.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from factory.cli import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
