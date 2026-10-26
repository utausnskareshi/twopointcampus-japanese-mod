# Two Point Campus 日本語化パッチ

Steam版「Two Point Campus」を日本語で遊ぶための非公式ツールです。
（An unofficial Japanese localization patch for the Steam version of Two Point Campus.）

- ゲームには公式の日本語訳が入っていますが、設定画面の言語に「日本語」が出てきません。このツールで表示言語を日本語に切り替えます。
- DLC（メディカルスクール、スクールスピリッツ）やチャレンジモード、MOD画面など、英語のまま残っている約2,400項目に日本語訳を補います。公式訳がある項目は一切変更しません。

## ダウンロード

[Releases](https://github.com/utausnskareshi/twopointcampus-japanese-mod/releases/latest) から `TpcJpPatch-<バージョン>.zip` をダウンロードしてください。

## 動作環境

- Windows 10 / 11（.NET Framework 4.8 を使います。Windows に標準で入っています）
- Steam版 Two Point Campus（動作を確認したバージョン：10.3.169253）

## 使い方

1. ゲームを一度起動してタイトル画面まで進め、終了します（初回のみ。言語の設定ファイルを作るためです）。
2. ZIP を好きなフォルダーに展開し、`TpcJpPatch.exe` を実行します（インストールは不要です。ZIP の中のファイルは同じフォルダーに置いたまま使ってください）。
3. 「日本語化する」を押します。完了のメッセージが出たら、ゲームを起動してください。

- 元に戻すときは「元に戻す」を押します（公式の日本語データと、以前の表示言語に戻ります）。
- ゲームのアップデートや「ゲームファイルの整合性を確認」の後は、もう一度「日本語化する」を押してください。
- Windows の SmartScreen の警告が出た場合は「詳細情報」→「実行」で起動できます。

注意事項・既知の制限・変更するファイル・コマンドラインでの使い方は、ZIP に同梱の `README.txt`（[patcher/README.txt](patcher/README.txt)）を参照してください。

## 注意

- 有志による非公式のツールです。SEGA および Two Point Studios とは関係ありません。このツールについて、公式へのお問い合わせはしないでください。
- このツールの使用によって生じたいかなる損害についても、作者は責任を負いません。自己責任でご利用ください。
- 訳は有志によるもので、公式の日本語訳と言い回しが異なる場合があります。

## 仕組み

- ゲームの日本語データ（`localisation_assets_localisationjapanese.bundle`）のうち、英語のまま残っている項目だけに訳を書き込みます。各訳には「書き換える前の公式の値」のハッシュを持たせており、アップデートで公式訳が追加された項目は上書きしません。
- 公式訳と同じ訳になる項目は、お使いのPCにある公式訳をそのままコピーします。配布物にはゲームのデータや公式の翻訳データを含めていません（訳文は公式訳と用語や言い回しを合わせているため、定型文の一部は公式訳と同じ表現になっています）。
- 書き換える前の公式データはバックアップとして同じフォルダーに保存し、「元に戻す」で元のファイルに戻します。
- 表示言語は、ゲームの設定ファイル（`preferences.json`）の言語番号を日本語（12）に書き換えます。

## リポジトリの内容

| パス | 内容 |
|---|---|
| `patcher/` | パッチャー本体（C# / .NET Framework 4.8 / AssetsTools.NET）、その試験、配布物の説明書 |
| `translations/ja/` | このプロジェクトの訳（キーごとの日本語訳・状態・訳注） |
| `translations/glossary_new.tsv` | 公式訳に無い固有名詞の訳語表 |
| `tools/` | 抽出・QA・翻訳データ生成・パッケージ作成・試験用の Python スクリプト |
| `tests/python/` | Python スクリプトの単体試験 |
| `docs/` | [調査レポート](docs/investigation-report.md)、[翻訳スタイルガイド](docs/style-guide.md)、[開発メモ](docs/development.md) |

このリポジトリには、ゲームのファイルや、そこから抽出した英語の原文・公式訳のデータは含まれていません（訳語表には固有名詞の英語表記を、訳注にはだじゃれなどを説明するための短い英語の引用を含みます）。開発用のスクリプトは、手元にインストールされたゲームから作業用のデータを `work/` に作ります（`work/` と `review/` はゲームの文章を含むため、Git の管理対象外にしています）。
開発環境・ビルド・試験の手順は [docs/development.md](docs/development.md) を参照してください。

## ライセンス・権利表記

- このリポジトリのソースコードと訳文は [MIT License](LICENSE) で公開しています（Copyright (c) 2026 歌うＬＩＮＥ彼氏）。
- ライセンスの対象は作者が作成した部分です。訳文はゲームの英語テキストを元にした翻訳であり、ゲーム本体・原作のテキスト・名称などの権利は SEGA および Two Point Studios に帰属します。
- 「Two Point Campus」「TWO POINT」は Two Point Studios Limited の、「SEGA」は株式会社セガの商標または登録商標です。
- パッチャーは [AssetsTools.NET](https://github.com/nesrak1/AssetsTools.NET)（MIT License。同梱の LZ4ps と元の LZ4 は BSD 2-Clause License、LZMA SDK はパブリックドメイン）を使用しています（配布 ZIP に `AssetsTools.NET.dll` として同梱）。ライセンスの全文は [patcher/THIRD-PARTY-NOTICES.txt](patcher/THIRD-PARTY-NOTICES.txt) を参照してください。
- 権利者の方から GitHub の Issues などでご連絡をいただいた場合は、速やかに対応します。
