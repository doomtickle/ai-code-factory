"""Entry point for the broker (the passdesk package beside this file).

Launched by run.sh inside the broker sandbox.
"""

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core.protocol import main

if __name__ == "__main__":
    main()
