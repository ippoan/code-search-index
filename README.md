# code-search-index

ippoan の **public repo 横断・意味検索(セマンティックコード検索)** のインデックスと MCP server。

- 索引: GitHub Actions が日次で全 public repo を diff 駆動で再インデックスし、
  sqlite-vec の DB を [Release `index`](../../releases/tag/index) に publish する
- 検索: `mcp/server.py` が DB を取得して MCP tool `semantic_code_search` を提供する
- 埋め込みモデル: `jinaai/jina-embeddings-v2-base-code`(768 次元、ONNX/CPU)。
  **索引側とクエリ側は必ず同一モデル**。モデルを変えたら自動で全量再構築される

## 仕組み

```
.github/workflows/index.yml   日次 cron + workflow_dispatch
  ├─ Release から前回 DB を取得(なければ全量build)
  ├─ 全 public repo を clone --filter=blob:none
  ├─ 前回 commit との git diff --name-status で変更ファイルのみ再処理
  ├─ tree-sitter で関数/クラス単位にチャンク化 (indexer/chunker.py)
  ├─ fastembed (ONNX) で埋め込み → sqlite-vec
  ├─ gzip して Release asset `code-index.db.gz` を差し替え(常に最新 1 本)
  └─ 同じ DB を Actions artifact にも保存(直近 14 日分の履歴・デバッグ用)
```

pip は setup-python の cache、埋め込みモデル(~150MB)は actions/cache で
キャッシュされるため、2 回目以降の run はダウンロードなしで始まる。

## MCP server のセットアップ(常駐マシン)

```bash
git clone https://github.com/ippoan/code-search-index.git
cd code-search-index
python3 -m venv venv && venv/bin/pip install -r mcp/requirements.txt
claude mcp add code-search -- $PWD/venv/bin/python $PWD/mcp/server.py
```

DB は `~/.cache/code-search-index/` に置かれ、6 時間ごとに Release の
更新をチェックして差し替える(`CODE_INDEX_REFRESH_SECONDS` で変更可)。
初回クエリ時にモデルをロードするため、最初の 1 回だけ数十秒かかる。

MCP を介さず手動で最新 DB をローカルへ同期するには:

```bash
./scripts/sync-db.sh
```

## 手動再構築

Actions → index → Run workflow。`full_rebuild=true` で全量作り直し、
`only=repo1,repo2` で対象 repo を絞れる。

## ローカルでインデックスを作る

```bash
venv/bin/pip install -r requirements.txt
venv/bin/python -m indexer --org ippoan --db code-index.db --only cc-relay
```

## 範囲外(v1)

- private repo(rust-ichibanboshi / nuxt-dtako-admin 等)は索引に**含まれない**。
  含める場合は PAT + 非公開ストレージ(R2)の別レーンが必要
- 完全一致検索(識別子の全件列挙)は対象外 — それは clone + rg の仕事
