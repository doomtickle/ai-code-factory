"""Entry point for the factory broker (the agentbroker package beside this file).

Launched by broker/run.sh inside the broker sandbox.
"""

from agentbroker import tools  # noqa: F401 - registers every tool
from agentbroker.core.protocol import main

if __name__ == "__main__":
    main()
