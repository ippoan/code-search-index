#!/usr/bin/env bash
# Pull the published release assets into the local cache (the same paths
# mcp/server.py and the warn hooks read:
#  ~/.cache/code-search-index/{code-index.db,dup-pairs.json,calls.db}).
#
# Change detection AND integrity use the release assets' sha256 digest from
# the GitHub API: unchanged digest -> skip the heavy download; downloaded
# bytes that do not match the digest -> ⚠ warning, keep the previous file,
# exit 1 (a timer simply retries later). An asset that is not published at all
# is not an error. Safe to run unattended.
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

# Fetch one asset, verifying the digest the Release API publishes for it.
# The digest doubles as the change stamp, so unchanged assets cost one API
# call and no download. mcp/server.py does the same three fetches in Python.
sync_asset() { # <asset> <dest> <stamp file> <gunzip: yes|no>
  local name="$1" dest="$2" stampf="$3" gz="$4"
  local digest updated want stamp got tmp

  digest=$(asset_field "$name" digest)        # "sha256:<hex>" or ""
  updated=$(asset_field "$name" updated_at)
  if [ -z "$digest" ] && [ -z "$updated" ]; then
    echo "$name: リリースに未公開 (skip)"      # e.g. calls.db.gz before its first run
    return 0
  fi
  want="${digest:-$updated}"
  stamp=$(cat "$DIR/$stampf" 2>/dev/null || echo "")
  if [ "$want" = "$stamp" ] && [ -f "$dest" ]; then
    echo "$name up to date ($want)"
    return 0
  fi

  tmp="$dest.dl.tmp"
  if ! curl -fsSL "$BASE/$name" -o "$tmp"; then
    rm -f "$tmp"
    echo "⚠ [code-search] $name: ダウンロードに失敗 — 前回のファイルを維持します" >&2
    fail=1
    return 0
  fi
  got="sha256:$(sha256sum "$tmp" | cut -d' ' -f1)"
  if [ -n "$digest" ] && [ "$got" != "$digest" ]; then
    echo "⚠ [code-search] $name: checksum mismatch: expected $digest got $got — 前回のファイルを維持します (アップロード途中か改竄の可能性)" >&2
    rm -f "$tmp"
    fail=1
    return 0
  fi
  if [ "$gz" = "yes" ]; then
    if ! gunzip -c "$tmp" > "$dest.raw.tmp"; then
      echo "⚠ [code-search] $name: 展開に失敗 — 前回のファイルを維持します" >&2
      rm -f "$tmp" "$dest.raw.tmp"
      fail=1
      return 0
    fi
    rm -f "$tmp"
    tmp="$dest.raw.tmp"
  fi
  mv "$tmp" "$dest"
  printf '%s' "$want" > "$DIR/$stampf"
  echo "$name -> $dest ($(du -h "$dest" | cut -f1))"
}

sync_asset code-index.db.gz "$DIR/code-index.db"  db-digest.txt    yes
sync_asset dup-pairs.json   "$DIR/dup-pairs.json" dup-digest.txt   no
sync_asset calls.db.gz      "$DIR/calls.db"       calls-digest.txt yes

exit "$fail"
