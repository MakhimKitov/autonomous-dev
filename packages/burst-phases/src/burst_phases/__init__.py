"""The phases that run beside a burst, not inside the control plane (TO-BE §6).

A Job is a three-container pod: **init** fills the volume, **agent** runs against
it, **harvester** ships what it left. This package is the first and the third.

What makes them safe is what they are *not given*. Neither holds a credential for
the archive: they hold **presigned URLs minted at spawn** — a GET for exactly one
snapshot key, PUTs for exactly the result keys, expiring inside the burst's own
rail. A capability for one object is not a key to a store: it cannot list, cannot
reach another burst's snapshot, and stops working before the next burst starts.
The clone rides a `contents:read`, single-repo token confined to init, so a
reviewer or tester agent still never sees a GitHub credential.

And neither may import `control_plane`. That is checked in CI on the built image,
the way the webhook receiver's is: the surest way to keep ledger access out of a
pod is to keep the code that speaks to a ledger out of the image.

Stdlib only — urllib for the two HTTP verbs, `session-volume` for the shape of
what is packed. A sidecar that needs a dependency tree is a sidecar that needs
patching.
"""
