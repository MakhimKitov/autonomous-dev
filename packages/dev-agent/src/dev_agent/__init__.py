"""dev-agent — the burst worker that runs inside the dev container.

Standalone by design: this package must never import ``control_plane``.
The image contains this code and the product clone, nothing else.
"""

from dev_agent.burst import BurstResult, run_burst, run_burst_sync
from dev_agent.env import BurstEnv, EnvError

__all__ = ["BurstEnv", "BurstResult", "EnvError", "run_burst", "run_burst_sync"]
