#!/usr/bin/env bash
# Claude Code PostToolUse hook (matcher: Read|Edit|Write): when the file just
# read or edited has a known near-duplicate in another org repo (per the
# dup-pairs.json ledger synced next to the index DB), feed a warning back to
# Claude so it can propose consolidation on the spot. Silent no-op when the
# ledger, jq, or repo context is missing. Reads the hook JSON on stdin.
set -u

LEDGER="${CODE_INDEX_CACHE:-$HOME/.cache/code-search-index}/dup-pairs.json"
[ -f "$LEDGER" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

fp=$(jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -n "$fp" ] && [ -e "$fp" ] || exit 0

dir=$(dirname "$fp")
root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || exit 0
slug=$(git -C "$dir" remote get-url origin 2>/dev/null \
  | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')
[ -n "$slug" ] || exit 0
key="$slug/${fp#"$root"/}"

hits=$(jq -r --arg k "$key" \
  '.[] | select(.a==$k or .b==$k)
       | "\(if .a==$k then .b else .a end) (chunks \(.n), sim \(.max_sim))"' \
  "$LEDGER" 2>/dev/null)
[ -n "$hits" ] || exit 0

{
  echo "⚠ [code-search] このファイルには他 repo にほぼ同一の実装があります:"
  printf '%s\n' "$hits" | sed 's/^/  - /'
  echo "編集する場合は相方にも同じ変更が必要か確認してください。可能なら共通化を提案 (重複解消の issue 化も可)。"
} >&2
exit 2
