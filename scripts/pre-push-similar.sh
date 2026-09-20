#!/usr/bin/env bash
# git pre-push hook: warn — never block — when the commits being pushed add
# code that already exists in another indexed org repo. Reads the standard
# pre-push stdin (local_ref local_sha remote_ref remote_sha per line) and
# prints warnings to stderr, so a Claude session running `git push` sees them
# in the tool result and can react immediately.
#
# Requirements on the machine:
#   - an interpreter with `pip install -r requirements.txt` (CODE_SEARCH_PY,
#     default ~/.venvs/code-search then <checkout>/venv; note this is
#     requirements.txt, not mcp/requirements.txt — indexer.similar chunks code)
#   - the checkout providing `indexer` is the one this script lives in
#     (override with CODE_SEARCH_HOME); do not make a second clone for it
#   - the index DB synced locally (the MCP server keeps
#     ~/.cache/code-search-index/code-index.db fresh; scripts/sync-db.sh too)
# Not set up at all -> exit 0 silently. Set up but unusable -> one line on
# stderr. Either way the push continues (advisory tooling must never block).
set -u

# This script lives in a checkout that provides `indexer`, so find it from
# here instead of guessing a path: a guessed ~/code-search-index means a second
# clone that nobody pulls, and it goes stale in silence.
CSH="${CODE_SEARCH_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The interpreter is a separate question — the venv need not live in the
# checkout. Either layout works with no environment set.
PY="${CODE_SEARCH_PY:-}"
if [ -z "$PY" ]; then
  for c in "$HOME/.venvs/code-search/bin/python" "$CSH/venv/bin/python"; do
    [ -x "$c" ] && PY="$c" && break
  done
fi
DB="${CODE_INDEX_CACHE:-$HOME/.cache/code-search-index}/code-index.db"
[ -x "$PY" ] && [ -f "$DB" ] || exit 0

slug=$(git remote get-url origin 2>/dev/null \
  | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')
[ -n "$slug" ] || exit 0

Z=0000000000000000000000000000000000000000
while read -r _local_ref local_sha _remote_ref remote_sha; do
  [ "$local_sha" = "$Z" ] && continue # branch deletion
  base="$remote_sha"
  if [ "$remote_sha" = "$Z" ]; then # new branch: compare against default branch
    base=$(git merge-base "$local_sha" origin/HEAD 2>/dev/null) || continue
    [ -n "$base" ] || continue
  fi
  # Not configured is silent (above); configured-but-broken must not be —
  # swallowing stderr here is how a safety net dies without anyone noticing.
  if ! out=$(PYTHONPATH="$CSH" "$PY" -m indexer.similar \
               --db "$DB" --repo "$slug" --base "$base" 2>&1); then
    printf '⚠ [code-search] 重複チェックを実行できませんでした (push は続行): %s\n' \
      "$(printf '%s' "$out" | tail -1)" >&2
    continue
  fi
  out=$(printf '%s\n' "$out" | grep '^::warning' | sed 's/^::warning[^:]*:://')
  if [ -n "$out" ]; then
    {
      echo "⚠ [code-search] push に既存実装と酷似したコードが含まれています (advisory、push は続行):"
      printf '%s\n' "$out" | sed 's/^/  - /'
      echo "  既存側の再利用・共通化を検討してください。詳細検索: semantic_code_search"
    } >&2
  fi
done
exit 0
