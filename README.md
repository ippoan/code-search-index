# code-search-index

ippoan の **public repo 横断・意味検索(セマンティックコード検索)** のインデックスと MCP server。

- 索引: GitHub Actions が日次で全 public repo を diff 駆動で再インデックスし、
  sqlite-vec の DB を [Release `index`](../../releases/tag/index) に publish する
- 検索: `mcp/server.py` が DB を取得して MCP tool `semantic_code_search` を提供する
- 呼び出し関係: SCIP 由来の `calls.db` から MCP tool `find_callers` が呼び出し元を列挙する
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

同期される asset は 3 本 (`code-index.db.gz` / `dup-pairs.json` / `calls.db.gz`)。
いずれも Release API の `digest` と突き合わせた **sha256 検証付き**で、合わない
バイト列は捨てて前回のファイルを維持する。未公開の asset は skip するだけで
失敗にはしない。`mcp/server.py` も同じ検証を通して同じキャッシュを読む。

## 呼び出し関係 (find_callers)

grep は trait 越し・Router 経由の呼び出しを取りこぼす。`calls.db`
(Release asset `calls.db.gz`、SCIP 抽出ジョブが生成) を引いて**呼び出し元**を
列挙する MCP tool が `find_callers`:

```
find_callers(symbol="resolve_tenant")                  # 定義の名前で
find_callers(path="src/router.rs", lines="40-80")      # その行にある定義で
find_callers(symbol="save", repo="ippoan/auth-worker") # 定義を repo で絞る
```

返すのは呼び出し元の `repo/path:line`・それを囲む定義の名前・role
(reference / implementation / type_definition)、そして**鮮度**
(各 repo の `commit_sha` と `meta.updated_at`) — いつ・どの木から作られた答えかを
必ず添える。MCP tool が遅延ロードで見えないときは同じ検索を CLI から叩ける:

```bash
python -m indexer.calls --symbol resolve_tenant
python -m indexer.calls --path indexer/db.py --lines 11-20 --json
```

`calls.db` がまだ Release に無い間、tool は落ちずに「呼び出し関係の索引が
まだありません」と返す。

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
