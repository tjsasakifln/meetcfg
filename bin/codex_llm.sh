#!/usr/bin/env bash
# LLM bridge: stdin = user prompt, $LLM_SYSTEM_PROMPT = instruction,
# stdout = the model's reply. Wired in via CUSTOM_LLM_CMD.
#
# Uses the Codex CLI already logged in on this machine (subscription auth).
# No API key is read or needed; nothing here is billed per request.
set -euo pipefail

MODEL_EFFORT="${CODEX_EFFORT:-low}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

# codex exec has no separate system-prompt flag, so the instruction is
# prepended to the prompt. Flags, and why:
#   --skip-git-repo-check  the repo may not be a git checkout
#   --ephemeral            don't leave a session file per meeting turn
#   --ignore-user-config   skip the user's MCP servers/hooks (seconds of
#                          startup latency we can't afford); auth still works
#   -s read-only           the copilot never needs to write anything
#   -o FILE                clean final message, no event-log preamble
{
  printf '%s\n\n' "${LLM_SYSTEM_PROMPT:-}"
  cat
} | codex exec \
      --skip-git-repo-check \
      --ephemeral \
      --ignore-user-config \
      -s read-only \
      -c model_reasoning_effort="$MODEL_EFFORT" \
      ${CODEX_MODEL:+-m "$CODEX_MODEL"} \
      -o "$OUT" \
      - >/dev/null   # stderr flows through: the backend logs it on failure

cat "$OUT"
