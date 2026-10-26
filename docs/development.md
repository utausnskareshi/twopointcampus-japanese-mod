# 開発メモ

## 開発環境

- Windows 10 / 11 と Steam版 Two Point Campus（スクリプトと試験は、手元にインストールされたゲームのデータを読みます）
- Python 3.11：`py -3.11 -m venv .venv` → `.venv\Scripts\python -m pip install -r requirements.txt`
  （UnityPy 1.25.4・openpyxl 3.1.5・msgpack 1.2.3。実行時は環境変数 `PYTHONIOENCODING=utf-8` を推奨）
- .NET SDK 8 以降（パッチャーは .NET Framework 4.8 向けにビルドされ、参照アセンブリと AssetsTools.NET は NuGet から自動取得）
- ゲームの場所は Steam の設定から自動で探します。別の場所を使うときは環境変数 `TPC_GAME_DIR` で指定します。

## フォルダー

| パス | 内容 | Git |
|---|---|---|
| `translations/ja/*.json` | 訳（`ja`・`status`・`note`。状態は draft / reviewed / edited） | 管理する |
| `translations/glossary_new.tsv` | 新出の固有名詞の訳語表（`tools/glossary_new.py` で作り直せる） | 管理する |
| `work/` | ゲームから抽出した原文・公式訳・翻訳バッチ・QA の結果など | **管理しない（ゲームの文章を含む）** |
| `review/` | レビュー用シート（英語の原文を含む） | **管理しない** |
| `dist/` | 翻訳データ（`dist/data/`）と配布物（`TpcJpPatch-<版>/` と ZIP） | 管理しない（ZIP は Releases で配布） |

## 作業の流れ

```
python tools/extract.py                       # ゲームの全言語のテキストを work/extract/strings.json に抽出（公式の日本語バンドルのコピーも work/backup/official/ に保存）
python tools/worklist.py                      # 公式訳が無い項目の一覧（work/worklist.json）と公式用語集
python tools/batch.py export <名前> [...]      # 翻訳バッチを書き出し、訳したものを import で取り込む
python tools/qa.py                            # エラー0を確認（work/qa_report.txt）
python tools/review_sheet.py export           # レビュー用シート（review/TPC_JP_review.xlsx）
python tools/review_sheet.py import review/TPC_JP_review.xlsx   # 修正・確認済みを取り込む（書き出し時の値と比べ、競合は取り込まない）
（patcher/README.txt の「更新履歴」に新しい版の行を追記）
python tools/package.py --version 1.0.1       # 翻訳データ生成 → exe ビルド → ZIP（既存の版は --force なしでは上書きしない）
python tools/test_patcher.py --exe dist/TpcJpPatch-1.0.1/TpcJpPatch.exe   # 一時フォルダーの偽ゲームで場面試験
```

- `package.py --status reviewed,edited` とすると、確認済み・修正済みの訳だけを収録する（0件ならエラーで止まる）。
- レビュー用シートを作り直すときは `python tools/review_sheet.py export --force`（既存のシートは `--force` なしでは上書きしない）。
- 配布 ZIP には `TpcJpPatch.exe`・`AssetsTools.NET.dll`（NuGet パッケージのファイルそのまま）・`TpcJpPatch.exe.config`・`README.txt`（`patcher/README.txt` から作成）・`LICENSE.txt`（`LICENSE` と同じ内容）・`THIRD-PARTY-NOTICES.txt` が入る。

## 主なツール

| コマンド | 用途 |
|---|---|
| `tools/extract.py` | ゲームの全言語の I2 キャッシュを `work/extract/strings.json` に抽出（日本語は `tpcloc/official.py` が選んだ公式データから。そのデータのコピーを `work/backup/official/` に保存） |
| `tools/worklist.py` | 公式訳が無い項目の一覧（`work/worklist.json`）と公式用語集（`work/glossary_official.tsv`）を作る |
| `tools/batch.py export/import` | 翻訳バッチの書き出し・取り込み |
| `tools/lookup.py <正規表現> [--en/--ja]` | 原文・公式訳・このプロジェクトの訳の検索 |
| `tools/qa.py [--batch FILE]` | タグ・パラメータ・改行・用語・文体の検査 |
| `tools/glossary_new.py` | `translations/glossary_new.tsv` を訳から作り直す |
| `tools/review_sheet.py export/import` | レビュー用 Excel の書き出し・取り込み |
| `tools/build_patch.py` | パッチャー用の翻訳データ（`dist/data/TpcJpPatch.data.tsv`） |
| `tools/package.py --version X.Y.Z` | 翻訳データ生成・exe ビルド・配布 ZIP 作成 |
| `tools/verify_bundle.py` | 適用後のゲームデータを公式データ＋翻訳データと全件照合（fill-only を考慮） |
| `tools/dev_patch.py`, `tools/switch_lang.py` | 開発用の簡易適用・表示言語の切り替え |
| `tools/screenshot.ps1`, `tools/click.ps1` | ゲーム内での表示確認用（画面の保存、ゲームのウィンドウへのクリック・キー入力） |

## 試験

| コマンド | 内容 |
|---|---|
| `python -m unittest discover -s tests/python -t .` | Python スクリプトの単体試験 |
| `dotnet build patcher/TpcJpPatch.Tests -c Debug` → `patcher/TpcJpPatch.Tests/bin/Debug/net48/TpcJpPatch.Tests.exe` | C# の試験（終了コード＝失敗数） |
| `python tools/test_patcher.py [--exe PATH]` | パッチャーの場面試験（一時フォルダーの偽ゲーム） |
| `python tools/test_language.py` | 表示言語の切り替え試験（Debug 版の exe と偽の設定フォルダーで実行） |
| `python tools/fuzz_patcher.py --iterations N` | 壊したバンドル・翻訳データ・設定ファイルを与えるファジング（一時フォルダー） |

- 試験の前に `python tools/extract.py`・`python tools/worklist.py`・`python tools/build_patch.py` を実行しておく。試験は公式の日本語バンドルのコピー（`work/backup/official/localisation_assets_localisationjapanese.bundle`）、抽出したテキスト（`work/extract/strings.json`）と翻訳データ（`dist/data/TpcJpPatch.data.tsv`）を使う。
- 試験はすべて一時フォルダーの偽ゲームで行い、本物のゲームと設定ファイルには触れない（ゲームのデータは一時フォルダーにコピーして使う）。Release 版の exe を試すときは必ず `--game <一時フォルダー>` と `--no-language` / `--keep-language` を付ける。
- パッチャーは同時に1つしか動かない（名前付きミューテックス `Local\TpcJpPatch`）。試験は順番に実行する。
- C# の試験は、環境変数 `TPCJP_REAL_BUNDLES` に公式の他の言語のバンドルのコピーを置いたフォルダーを指定すると、それらも読み込めるか試す（無ければその試験は SKIP）。
  `%TEMP%\tpcjp_cstests\preferences.orig.json` に本物の `preferences.json` のコピーを置くと、そのコピーでも読み書きを試す（無ければ2件が SKIP）。
  `--compare-exe <基準の exe> --compare-exe <新しい exe>` を付けると、2つの Release 版の動作を比べる試験も行う（付けなければ SKIP。同じ版の2つのビルドを比べるためのもので、版番号や埋め込みリソースが違うと失敗する）。
- コードや試験のコメントにある `FZ-01`・`S1`・`CS-11` などの番号と `work/localtest/…` への参照は、公開していない内部の試験・レビューの記録を指す。
- Debug 版の exe だけが次の環境変数を読む：`TPCJP_SIMULATE_NOT_INSTALLED=1`（未インストール扱い）、`TPCJP_PREFS_ROOT`（設定フォルダーの差し替え）、`TPCJP_APPDATA`（記録フォルダーの差し替え）、`TPCJP_SIMULATE_NO_POSIX_RENAME=1`（POSIX 方式の名前変更が使えないボリュームとして動く）、`TPCJP_SIMULATE_MOVEFILE_ERROR=<Win32 のエラー番号>`（MoveFileEx をそのエラーで失敗させる。使用中を表す 5・32・33 は POSIX 方式の名前変更で置き換わるので、失敗させるには `TPCJP_SIMULATE_NO_POSIX_RENAME=1` も指定する）。

## パッチャーの仕組み

- 日本語バンドル内の各 I2LS_* TextAsset を読み、このプロジェクトの訳がある項目だけ値を置き換える。
- 各訳には「置き換える前の公式の値」のハッシュを持たせ、ゲーム更新で公式訳が入った項目や原文が変わった項目は上書きしない。
- 公式訳と全く同じ訳は、公式訳のキーへの参照として保存し、適用時にゲーム内のデータからコピーする（公式の文章を配布物に含めない。ただし定型文の一部は、公式訳と同じ言い回しで訳している）。
- 適用前の公式データを `.tpcjp-original`、状態を `.tpcjp-state` としてバンドルの隣に保存。元に戻すとハッシュまで一致する公式データに戻る。
- 現在のデータが「このパッチの出力」か「公式（更新後を含む）」かは、状態ファイルのハッシュ、または内容（このパッチの訳と公式の値のどちらが多いか）で判定する。バックアップは使う前に確認し、判断できないときは何も変更せずに止まる。ファイルは一時ファイル経由で置き換える。
- 表示言語は `preferences.json`（MessagePack）の `_selectedLanguage` を 12（日本語）に書き換える。
- AssetsTools.NET.dll は exe に内蔵せず、exe と同じフォルダーに置く（ビルドで出力フォルダーにコピーされ、ZIP にも入る）。1.0.0 は exe に内蔵して実行時にメモリーから読み込んでいたが、この形はウイルス対策ソフトに誤検出されやすいため 1.0.1 でやめた。exe は起動時に DLL があるか確かめ、無ければ ZIP の展開を促して終了する。
