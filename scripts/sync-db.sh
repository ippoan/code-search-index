#!/usr/bin/env bash
# Pull the latest published index DB + duplicate ledger into the local cache
# (the same paths mcp/server.py and the warn hooks read:
#  ~/.cache/code-search-index/{code-index.db,dup-pairs.json}).
#
# Change detection AND integrity use the release assets' sha256 digest from
# the GitHub API: unchanged digest -> skip the heavy download; downloaded
# bytes that do not match the digest -> ⚠ warning, keep the previous file,
# exit 1 (a timer simply retries later). Safe to run unattended.
set -uo pipefail

DIR="${CODE_INDEX_CACHE:-$HOME/.cache/code-search-index}"
ORG="${CODE_INDEX_ORG:-ippoan}"
REPO="${CODE_INDEX_REPO:-code-search-index}"
BASE="https://github.com/$ORG/$REPO/releases/download/index"
API="https://api.github.com/repos/$ORG/$REPO/releases/tags/index"

mkdir -p "$DIR"

meta=$(curl -fsSL "$API" 2>/dev/null) || { echo "release API unreachable" >&2; exit 1; }
asset_field() { # name field
  printf '%s' "$meta" | python3 -c "
import json, sys
name, field = sys.argv[1], sys.argv[2]
d = json.load(sys.stdin)
print(next((a.get(field) or '' for a in d.get('assets', []) if a['name'] == name), ''))
" "$1" "$2"
}

fail=0

# --- index DB (heavy: download only when the digest changed) ---
db_digest=$(asset_field code-index.db.gz digest)   # "sha256:<hex>" or ""
db_updated=$(asset_field code-index.db.gz updated_at)
local_digest=$(cat "$DIR/db-digest.txt" 2>/dev/null || echo "")

if [ -n "$db_digest" ] && [ "$db_digest" = "$local_digest" ] && [ -f "$DIR/code-index.db" ]; then
  echo "db up to date ($db_digest)"
else
  curl -fsSL "$BASE/code-index.db.gz" -o "$DIR/code-index.db.gz.tmp"
  got="sha256:$(sha256sum "$DIR/code-index.db.gz.tmp" | cut -d' ' -f1)"
  if [ -n "$db_digest" ] && [ "$got" != "$db_digest" ]; then
    echo "⚠ [code-search] DB checksum mismatch: expected $db_digest got $got — 前回の DB を維持します (アップロード途中か改竄の可能性)" >&2
    rm -f "$DIR/code-index.db.gz.tmp"
    fail=1
  else
    gunzip -c "$DIR/code-index.db.gz.tmp" > "$DIR/code-index.db.tmp"
    rm "$DIR/code-index.db.gz.tmp"
    mv "$DIR/code-index.db.tmp" "$DIR/code-index.db"
    printf '%s' "$db_digest" > "$DIR/db-digest.txt"
    # mcp/server.py compares this stamp against the asset's updated_at
    [ -n "$db_updated" ] && printf '%s' "$db_updated" > "$DIR/updated_at.txt"
    echo "db synced -> $DIR/code-index.db ($(du -h "$DIR/code-index.db" | cut -f1))"
  fi
fi

# --- duplicate ledger (tiny: fetch every run, verify the same way) ---
ledger_digest=$(asset_field dup-pairs.json digest)
if curl -fsSL "$BASE/dup-pairs.json" -o "$DIR/dup-pairs.json.tmp" 2>/dev/null; then
  got="sha256:$(sha256sum "$DIR/dup-pairs.json.tmp" | cut -d' ' -f1)"
  if [ -n "$ledger_digest" ] && [ "$got" != "$ledger_digest" ]; then
    echo "⚠ [code-search] ledger checksum mismatch: expected $ledger_digest got $got — 前回の台帳を維持します" >&2
    rm -f "$DIR/dup-pairs.json.tmp"
    fail=1
  else
    mv "$DIR/dup-pairs.json.tmp" "$DIR/dup-pairs.json"
    echo "ledger -> $DIR/dup-pairs.json"
  fi
else
  rm -f "$DIR/dup-pairs.json.tmp"
fi

exit "$fail"
