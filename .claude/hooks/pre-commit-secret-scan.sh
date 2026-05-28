#!/bin/bash
# PreToolUse hook: blocks a `git commit` when the staged diff adds a
# HIGH-CONFIDENCE secret (a full key shape, not a bare prefix).
#
# Deliberate choice: only full shapes (prefix + enough entropy) match, so
# docs that mention bare prefixes (sk-, ghp_, Bearer) as examples are NOT
# blocked. This repo is PUBLIC, so a leaked secret would enter public
# history — this is the highest-value guard here.
#
# exit 0 = allow / not applicable ; exit 2 = block the commit.

INPUT=$(cat)

CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
# Only act on a git commit
echo "$CMD" | grep -qE '\bgit\b.*\bcommit\b' || exit 0

DIFF=$(git diff --cached 2>/dev/null)
[ -z "$DIFF" ] && exit 0

# Full shapes only (bare prefixes in docs won't match):
# - ghp_/gho_ + 36, github_pat_ + long  (GitHub PAT)
# - sk-ant- + long, sk- + 40+           (Anthropic / OpenAI-like)
# - AKIA + 16                           (AWS access key id)
# - PEM private key block
PATTERNS='ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,}|sk-ant-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{40,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

# Scan ADDED lines only (removing a secret must not block)
HITS=$(echo "$DIFF" | grep -E '^\+' | grep -onE "$PATTERNS" 2>/dev/null | head -3)

if [ -n "$HITS" ]; then
  {
    echo "✗ secret-scan: high-confidence secret(s) detected in the staged diff — commit blocked."
    echo "  Type(s): $(echo "$HITS" | sed -E 's/[A-Za-z0-9_-]{6,}/****/g' | sort -u | tr '\n' ' ')"
    echo "  Action: remove from the diff, use an env var / placeholder, check .gitignore."
    echo "  This repo is PUBLIC — never commit real secrets."
  } >&2
  exit 2
fi

exit 0
