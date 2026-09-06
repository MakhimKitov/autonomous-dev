"""The webhook receiver — the platform's only public surface (TO-BE §5, E5).

Deliberately dumb, and credential-free: it holds the Apps' webhook secrets and
nothing else — no ledger DSN, no GitHub key, no state that outlives the process.
Three things, in order, per delivery: verify the HMAC signature against the
App's secret, drop a delivery id it has seen (an in-memory LRU), and forward a
*wake* — the repository and the event name — to the control plane's internal
endpoint. The control plane polls GitHub as the truth; the wake only makes the
next poll happen now. So a forged delivery is refused for free, a replayed one
is dropped for free, and the worst either can ever cost after a receiver
restart is one extra poll of real GitHub state — never a state transition.

This package never imports the control plane. `secret_refs` is the one thing
the two share.
"""
