# Golden bursts

Fixtures for `control-plane replay` (G4, `control_plane/replay.py`): one node
and one task, run as an identical burst under each runtime, emitting the
criteria matrix.

- **`check.json`** — the real dev-agent image in its boot-smoke mode
  (`dev-agent check`: no network, no key, no spend), started from a node that
  ships with the fixture (`check-node/`, archived at replay time), told to
  resume that node's session, on discord-bot pinned to one commit. It measures
  what a runtime does to a burst — boot, the init phase's GET and clone, the
  restore, the harvest — and nothing a model does. CI replays it under
  `default` and `gvisor` on a kind cluster and uploads the matrix.

A fixture for a real node needs no seed: `--node <burst id>` replays from that
burst's snapshot and session as the ledger recorded them, and `"command": "run"`
runs the engine on this machine's credential — it costs what the burst cost.
A replay writes no ledger row and holds no GitHub credential.

```bash
uv run control-plane replay examples/golden/check.json \
  --runtime default --runtime gvisor --out data/replays/matrix --strict
```
