#!/bin/sh
# codex shim (issue #1) — the only `codex` on PATH in the dev-agent image.
# The codex-review skill *instructs* the safe invocation; this shim *enforces*
# it (walls vs tripwires, ARCHITECTURE.md §8):
#   - burst credentials are scrubbed unconditionally — the reviewer never
#     inherits the agent's tokens (codex's own auth is the mounted ~/.codex)
#   - sandbox-weakening flags are refused before the real binary ever runs
#     (the bash wall logs the attempt as a violation one layer earlier)
for arg in "$@"; do
  case "$arg" in
    --dangerously-bypass-approvals-and-sandbox|--yolo|*danger-full-access*)
      echo "codex: refused by the platform shim: '$arg' weakens the sandbox." >&2
      echo "codex: the read-only review is the only supported invocation (codex-review skill)." >&2
      exit 64
      ;;
  esac
done
exec env -u GH_TOKEN -u GITHUB_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY \
  /usr/local/bin/codex-real "$@"
