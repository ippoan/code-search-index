#!/usr/bin/env bash
# Pull the latest published index DB into the local cache
# (the same path mcp/server.py reads: ~/.cache/code-search-index/code-index.db).
set -euo pipefail

DIR="${CODE_INDEX_CACHE:-$HOME/.cache/code-search-index}"
ORG="${CODE_INDEX_ORG:-ippoan}"
REPO="${CODE_INDEX_REPO:-code-search-index}"
URL="https://github.com/$ORG/$REPO/releases/download/index/code-index.db.gz"

mkdir -p "$DIR"
curl -fsSL "$URL" -o "$DIR/code-index.db.gz.tmp"
gunzip -c "$DIR/code-index.db.gz.tmp" > "$DIR/code-index.db.tmp"
rm "$DIR/code-index.db.gz.tmp"
mv "$DIR/code-index.db.tmp" "$DIR/code-index.db"
echo "synced -> $DIR/code-index.db ($(du -h "$DIR/code-index.db" | cut -f1))"
