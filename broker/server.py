"""Entry point for the broker (the passdesk package beside this file).

A deployment launches this however it runs the process (e.g. run.sh under
sbx, a container entrypoint elsewhere).
"""

from passdesk import tools  # noqa: F401 - registers every tool
from passdesk.core.protocol import main

if __name__ == "__main__":
    main()
