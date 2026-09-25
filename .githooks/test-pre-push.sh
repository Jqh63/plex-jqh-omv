#!/usr/bin/env bash
# Bench for .githooks/pre-push — pushes for real to a throwaway bare remote.
# Case B is the one a per-commit check got wrong (bump in its own commit).
set -uo pipefail
unset $(env | sed -n 's/^\(GIT_[A-Z_]*\)=.*/\1/p') 2>/dev/null
HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pre-push"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
git init -q --bare "$T/remote.git"; git init -q -b main "$T/r"; cd "$T/r" || exit 1
git config user.email t@t; git config user.name t
mkdir -p .githooks tests relay; cp "$HOOK" .githooks/
echo "var CACHE = 'x-v8-2026-01-01a';" > sw.js; echo a > app.js; echo a > README.md
git add -A; git commit -q -m seed; git remote add origin "$T/remote.git"
git push -q origin main; git fetch -q origin; git config core.hooksPath .githooks
fail=0; n=0
touch_() { if [ "$1" = BUMP ]; then n=$((n+1)); echo "var CACHE = 'x-v8-2026-01-01$n';" > sw.js; set -- sw.js; else echo "$RANDOM" >> "$1"; fi; git add "$1"; }
try() { # $1 label, $2 expected (pass|block), then commits separated by "--"
  local label="$1" want="$2"; shift 2
  git checkout -q -b "b$RANDOM" main
  for f in "$@"; do [ "$f" = -- ] && { git commit -q -m c; continue; }; touch_ "$f"; done
  git commit -q -m c
  if git push -q origin HEAD 2>/dev/null; then got=pass; else got=block; fi
  git checkout -q main
  [ "$got" = "$want" ] && echo "  ok   $label" || { echo "  FAIL $label (got $got)"; fail=1; }
}
try "A — served file, no bump"                 block app.js
try "B — bump in its OWN commit (same branch)" pass  app.js -- BUMP
try "C — sw.js edited, CACHE untouched"        block sw.js
try "D — prose only"                           pass  README.md
try "E — tests + relay only"                   pass  tests/x.py relay/y.py
try "F — prose + served, no bump"              block README.md index.html
[ $fail -eq 0 ] && echo "ALL CASES PASS" || echo "SOME CASES FAIL"
exit $fail
