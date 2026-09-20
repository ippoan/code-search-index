# ranker — 「この chunk を注入すべきか」の判断層 (試作)

検索結果 1 件ごとに「作業中のクエリに対して注入する価値があるか」を返す LightGBM。
教師データは **git の過去コミットから自動生成** (LLM は使わない)。学習した木は
LightGBM 非依存の JSON に書き出し、`ranker/predict.py` (stdlib のみ) で推論する。

確かめたかったのは 3 つだけ: **学習できる / 速い / 単純な閾値より良い**。
配送層 (フックや MCP で実際に注入する部分) はこの PR の範囲外。

```
ranker/dataset.py   git 履歴 → (クエリ, 候補 30 件, ラベル) の JSONL
ranker/features.py  特徴量 15 個の定義 (検索が既に返す量 + パス/記号の一致)
ranker/train.py     学習 + 時系列 hold-out 評価 + 木の書き出し
ranker/predict.py   書き出した木の推論 (依存なし)
ranker/model.json   書き出した木 (下記の条件で学習したもの)
ranker/report.json  下の表の元データ (全項目)
```

## 教師データの作り方

コミット 1 本 = クエリ 1 本。件名を「指示文」、そのコミットが触った
**インデックス済みファイル**を「注入されるべきだったもの」とみなす。

1. 索引対象の public repo のうち手元に clone があるものの `git log` を読む
   (merge / bump / revert / 12 文字未満の件名は捨てる、触ったファイルが 10 を超える
   コミットも捨てる — 一括整形は「どの chunk か」を何も教えないため)
2. 件名をそのまま既存の検索 (`indexer.search.search`) に通して上位 30 件を候補にする
3. 候補の**ファイル**がそのコミットで変わっていれば正例、それ以外は負例

ラベルはファイル単位。行の重なりは見ていないので、変わった関数の隣の関数も正例に
なる — これがラベルノイズの最大の出どころ。

データセット本体 (`*.jsonl`) は **commit しない** (コミット件名が入るため。CLAUDE.md)。

## 条件 (数字はこの条件でのみ有効)

| | |
|---|---|
| 母集団 | 索引済み 63 repo のうち、手元に full clone があった **20 repo** (ippoan / ohishi-exp の public) |
| 索引 | Release `index` の `code-index.db` (updated_at 2026-09-19、102,137 chunks) |
| 期間 | `--since 2025-01-01` の全コミット。採れたクエリ 2,018 本 |
| 使ったクエリ | そのうち **上位 30 件に正例が 1 つ以上あった 1,259 本** (62.4%)。残りは学習・評価の両方から除外 |
| 分割 | コミット日時の 0.8 分位で時系列 hold-out。train 1,007 本 (2026-02-21〜09-04、18 repo)、test **252 本** (2026-09-04〜09-19、12 repo) |
| 候補 | クエリあたり 30 件 (train 30,210 / test 7,560 件、test の正例率 21.8%) |
| k | 表の NDCG・P は @5 / @10 |

索引は HEAD 時点のものなので、古いコミットほど「当時あったが今は無い/改名された」
ファイルが正例から抜ける。test 期間は索引更新の直前 2 週間なのでこのズレは小さい。

## 結果 (test 252 クエリ / 7,560 候補)

| 判定ルール | P | R | F1 | PR-AUC | NDCG@5 | NDCG@10 | P@5 |
|---|---|---|---|---|---|---|---|
| cos ≥ 0.93 (`similar.py` の既定値) | 0 | 0 | 0 | — | — | — | — |
| cos ≥ 0.589 (train で F1 最大に調整) | .304 | .605 | .405 | .343 | .468 | .502 | .375 |
| 同じ repo なら全部 | .415 | **1.000** | .587 | — | .547 | .610 | .504 |
| 同じ repo かつ cos ≥ 0.477 (train で調整) | .415 | .979 | .583 | .508 | .690 | .724 | .504 |
| **LightGBM (38 本)** | **.745** | .749 | **.747** | **.823** | **.820** | **.840** | **.610** |

閾値はすべて train 側だけで決めて test にそのまま当てている。NDCG / P@k のベースライン
順位は「cos 順 = 索引がそのまま返す順」と「同 repo を先頭に寄せた cos 順」。

「同じ repo なら全部」の再現率が 1.000 なのは、ラベルの作り方の帰結
(正例は必ずクエリと同じ repo のコミットから来る)。つまり **repo 横断の正例は
この教師データでは原理的に出ない** — cross-repo の注入価値はここでは測れていない。

### 特徴量を抜いたとき (同じ test)

| 構成 | F1 | NDCG@10 |
|---|---|---|
| 全 15 特徴量 | .747 | .840 |
| `same_repo` を抜く | .600 | .616 |
| **検索スコア系 6 個** (`cos` `distance` `rank` `cos_gap_top` `cos_z` `cos_top1`) を抜く | **.746** | .815 |

正直な負の結果: **検索スコアを特徴量にしたことの寄与はほとんど無い**。効いているのは
`same_repo` と、パス由来の構造的な特徴 (`same_file_frac` / `is_excluded_path` /
`path_overlap`)。gain 上位も `same_repo` 66,256 → `is_excluded_path` 20,514 →
`same_file_frac` 14,484 で、`cos` は 537 と下から数えた方が早い。
「RAG のスコアを特徴量にして判断層を作る」という当初の筋は、少なくともこのラベルでは
**スコアではなく構造が効いている**と読むべき。

### 正例が無いクエリも含めた場合 (参考)

上位 30 件に正例が 1 つも無い 759 本も入れると (test 404 本、正例率 14.6%):
モデル F1 .666 / NDCG@10 .565、同 repo 全部 .514、同 repo かつ cos .504。
順位は変わらないが全体に下がる。上の表は「索引に答えがある場合」の数字。

## 速さ (実測)

| | |
|---|---|
| 推論 | **5.9 µs / 候補**、**0.176 ms / クエリ** (30 候補) |
| 実装 | `ranker/predict.py` (stdlib のみ、1 スレッド、numpy も LightGBM も無し) |
| 書き出した木 | 38 本 / JSON 88.8 KB |
| LightGBM との一致 | 確率の最大絶対差 **2.2e-16** (test 7,560 行) |
| 学習 | 0.19 秒 (CPU) |
| データ作り | 履歴の走査 9.8 秒 + 埋め込み・検索 344 秒 (2,018 クエリ = 170 ms/クエリ。内訳は埋め込み 8 ms + sqlite-vec の全走査 156 ms) |

履歴の走査が 9.8 秒で済んだのは**手元の full clone を使ったから**。`gitsync.sync_repo`
の `--filter=blob:none` な clone で同じことをすると commit ごとに blob fetch が走るので、
この数字は当てはまらない (その経路は測っていない)。

## 再現手順

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-ml.txt
# 1) 教師データ (workdir = 索引対象 repo の clone が並んでいるディレクトリ)
venv/bin/python -m ranker.dataset --db ~/.cache/code-search-index/code-index.db \
    --workdir ~/src --since 2025-01-01 --k 30 --out dataset.jsonl
# 2) 学習 + 評価 + 書き出し
venv/bin/python -m ranker.train --data dataset.jsonl \
    --out-model ranker/model.json --out-report ranker/report.json
# 3) 特徴量を抜いた確認
venv/bin/python -m ranker.train --data dataset.jsonl --out-model /dev/null \
    --drop-features cos,distance,rank,cos_gap_top,cos_z,cos_top1
```

`lightgbm` / `scikit-learn` は **`requirements-ml.txt`** にしかない。日次索引
(`index.yml`) と `full-rebuild.yml` が入れるのは `requirements.txt` だけで、
そちらは一切変えていない。ML 依存が無い環境では ranker のテストは
`pytest.importorskip` で skip される (`predict` と `features` のテストは依存なしで走る)。

## 測れていないこと

- repo 横断の注入価値 (上記のとおりラベルが原理的に作れない)
- 関数単位の正しさ (ラベルはファイル単位)
- 手元に clone が無い 43 repo、および 2025-01-01 より前のコミット
- 実際に注入して役に立ったか (配送層とセッションログが要る。この PR の範囲外)
- wasm / Workers 上での速度 (測ったのは CPython の 1 スレッドのみ)
