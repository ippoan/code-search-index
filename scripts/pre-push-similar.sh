#!/usr/bin/env bash
# git pre-push hook: warn — never block — when the commits being pushed add
# code that already exists in another indexed org repo. Reads the standard
# pre-push stdin (local_ref local_sha remote_ref remote_sha per line) and
# prints warnings to stderr, so a Claude session running `git push` sees them
# in the tool result and can react immediately.
#
# Requirements on the machine:
#   - a clone of ippoan/code-search-index with its venv (CODE_SEARCH_HOME,
#     default ~/code-search-index; needs `pip install -r requirements.txt`)
#   - the index DB synced locally (the MCP server keeps
#     ~/.cache/code-search-index/code-index.db fresh; scripts/sync-db.sh too)
# Anything missing -> exit 0 silently (advisory tooling must never break push).
set -u

CSH="${CODE_SEARCH_HOME:-$HOME/code-search-index}"
PY="$CSH/venv/bin/python"
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
  out=$(PYTHONPATH="$CSH" "$PY" -m indexer.similar \
          --db "$DB" --repo "$slug" --base "$base" 2>/dev/null \
        | grep '^::warning' \
        | sed 's/^::warning[^:]*:://') || true
  if [ -n "$out" ]; then
    {
      echo "⚠ [code-search] push に既存実装と酷似したコードが含まれています (advisory、push は続行):"
      printf '%s\n' "$out" | sed 's/^/  - /'
      echo "  既存側の再利用・共通化を検討してください。詳細検索: semantic_code_search"
    } >&2
  fi
done
exit 0
