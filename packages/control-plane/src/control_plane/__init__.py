"""control-plane — the scheduler side of the platform.

Owns all persistence (session archive, ledger) and telemetry; spawns burst
containers and harvests their volumes. May import ``dev_agent`` (for the env
contract); the reverse import is forbidden.
"""
