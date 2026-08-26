# code-search-index 作業ルール

- **PR 作成 = merge 行き**: auto-merge 有効 (required check `test`、squash、branch 自動削除)。試し PR は draft で。
- 全量再構築は `full-rebuild` workflow (shard 並列 + chunk/vector cache)。直列の index run で全量をやると runner VM 死の実績あり — やらない。
- 埋め込みモデル・`embed_text`・チャンク形状を変えたら full rebuild 必須。cache key の `caches-v1` prefix も bump する。
- モデル名の正は `indexer/db.py` の `MODEL_NAME`。索引側とクエリ側 (mcp/server.py) は必ず同一モデル。
- mcp/server.py は mcp 1.x/2.x 両対応 import と `check_same_thread=False` を維持する (どちらも実地で踏んだ)。
- 重複検知の台帳は Release asset `dup-pairs.json` (indexer/dedup.py が CI で更新)。閾値や除外は dedup.py の定数が正。
