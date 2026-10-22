# Two Point Campus 日本語化 調査レポート

調査日: 2026-10-19 / 対象: Steam版 Two Point Campus（バージョン 10.3.169253）

---

## 1. 調査した環境

| 項目 | 内容 |
|---|---|
| インストール先 | `C:\Program Files (x86)\Steam\steamapps\common\Two Point Campus`（既定の場所） |
| ゲームバージョン | `10.3.169253+2024-12-06.1241`（`version.txt`）／ Steam build 20226581 |
| エンジン | Unity 2020.3.47f1、IL2CPP、Addressables 1.21.10 |
| テキスト系 | I2 Localization ＋ TextMeshPro |
| DRM | Denuvo Anti-tamper（Steam ストアに表記あり） |
| DLC | 調査環境には未導入（DLC のテキストはゲーム内で確認していない） |
| 設定ファイル | `%USERPROFILE%\AppData\LocalLow\Two Point Studios\Two Point Campus\Cloud\<SteamID3>\preferences.json`（MessagePack形式、Steam Cloud同期対象） |
| mod.io | ゲームID 4081。MOD の保存先は `C:\Users\Public\mod.io` |
| ローカルMODフォルダ | `%USERPROFILE%\AppData\LocalLow\Two Point Studios\Two Point Campus\Mods\<MOD名>\`（公式ガイド記載） |

## 2. ローカライズの仕組み

- テキストは I2 Localization の「言語キャッシュ」形式 `キー=訳文[i2t]キー=訳文…`（UTF-8）で、言語ごとの Addressables バンドルに TextAsset として格納されている。
  - `TPC_Data\StreamingAssets\aa\StandaloneWindows64\localisation_assets_localisation.bundle` … 25個の LanguageSourceAsset（I2LS_UI, I2LS_Items, I2LS_Levels …）
  - `localisation_assets_localisation<言語>.bundle` … 13言語分。**`localisation_assets_localisationjapanese.bundle` が存在する**
- I2 の言語リストは13言語で、Japanese（ja）も有効な言語として登録されている。
- Addressables カタログ上、ローカルバンドルは `m_Crc = 0`（CRC検証なし）。日本語バンドルを再パックしても読み込みは拒否されない構成。
  - コピーに対して UnityPy で TextAsset を書き換えて再パックし、同一オブジェクト構成で読み直せることを確認済み（ゲーム本体のファイルは未変更）。

### フォント

- `I2LS_Fonts` で、日本語選択時は全フォントが `NotoSans_JP` に置き換わる。
- `NotoSans_JP`（静的アトラス 2,025字）… 公式日本語訳で使われている文字をほぼ全て収録。
- フォールバック `NotoSans_JP_Dynamic` … `NotoSansJP-Medium.otf`（フル収録）を動的に読み込む設定。
  → 新規訳で静的アトラスにない漢字を使っても、理論上は動的生成で表示される（**実機確認が必要**）。

## 3. 日本語訳の現状

| 項目 | 件数 |
|---|---|
| 総キー数（フォント定義を除く） | 15,651 |
| 公式日本語訳あり | 12,778（**81.6%**） |
| 未訳（英語のまま／空欄、未使用キーは除外） | **約2,713件・英語 約26,400語** |

未訳の内訳（キー名から分類）:

| 内容 | 件数 | 英単語数 |
|---|---|---|
| Medical School（DLC） | 約889 | 約10,960 |
| School Spirits（DLC） | 約494 | 約4,550 |
| チャレンジモード | 約232 | 約4,210 |
| MOD（mod.io）ブラウザ画面 | 約204 | 約940 |
| 季節イベント・Prime・カフェテリア・プロモ等の無料アップデート | 約236 | 約1,170 |
| その他（上記に分類できない後期追加分） | 約658 | 約4,580 |

- 本編と Space Academy DLC はほぼ翻訳済み。翻訳が止まったのはそれ以降の追加コンテンツ。
- 訳文中のマークアップ: `\n`、`<br>`、`<style="…">…</style>`、`<sprite=…>`、`<color=…>`、`{COUNT}` 等のパラメータ、`[i2p_One]`（複数形タグ）。翻訳時に保持が必須。
- 公式訳の用語例: Kudosh → 勲章ポイント、Two Point County → ツーポイント州、Janitor → 用務員、Library → 図書館、Freshleigh Meadows → フレッシュリーメドウズ。

## 4. Steam版で日本語が選べない理由

- SEGA は 2023年11月に日本語版の発売中止を発表。Steamストアの対応言語も11言語で日本語なし。
- 設定画面の言語の選択肢には ID 0〜5, 7〜11 の11言語だけが表示され、Japanese（ID 12）は出てこない。
- 一方、起動時には `preferences.json` に保存されている `_languagePreferences._selectedLanguage` がそのまま適用される
  （`_hasExpectedPlatformLanguage` が true で、`_expectedPlatformLanguage` が Steam の言語設定と一致している場合）。
- よって `_selectedLanguage` を 12 にすると公式日本語で起動する（5ch で報告されている設定ファイルの書き換えと同じ結果）。
  - 値の位置は他の設定値の長さで変わるため、固定アドレスの書き換えではなく MessagePack を解析して書き換えるべき。
- 副作用:
  - 設定画面を開くと、言語欄には一致する項目が無いため別の言語（トルコ語）が表示される。開くだけでは言語は戻らない。
  - プルダウンで別の言語を選ぶと、ゲームの画面からは日本語に戻せない。
  - Steam の言語設定を変えると、Steam の言語に合わせてリセットされる。
  - 音声言語は English/German/Mandarin のみ。日本語音声は無い（ラジオ・構内放送の字幕は日本語訳あり）。

## 5. MOD（mod.io）システム

- 公式ガイド: Unity 2020.3.47f1 ＋ 公式モッディングプロジェクト → Addressables ビルド → zip を mod.io にアップロード → **運営の承認後に公開**。
- 対応するMOD種別は2つだけ:
  - アイテム（既存アイテムをベースにした新規アイテム）
  - 部屋の壁・床
  - アイテム名・説明は単なる文字列で、多言語対応の仕組みは無い。
- MOD はキャリア／サンドボックスのロード時に読み込まれ、言語データの読み込み（起動時）より後になる。言語データの再読み込みは言語を変更したときだけ。
- ゲームは IL2CPP でビルドされているため、MOD に C# のコードを含めて実行させることはできない。
- mod.io 上の Two Point Campus の MOD は 467件で、すべてアイテム系。言語・翻訳系のMODは 0件（調査時点）。

**結論:** 正規の mod.io MOD 機構では「日本語の有効化」も「翻訳データの補完」も行えない。そのため、このプロジェクトはゲームの日本語データと設定ファイルを書き換える外部ツールとして作成した。

## 6. 参考情報

- Steam ストア（対応言語・Denuvo 表記）: https://store.steampowered.com/app/1649080/Two_Point_Campus/
- 日本語版発売中止の報道（AUTOMATON）: https://automaton-media.com/articles/newsjp/20231113-271844/
- mod.io 公式ガイド: https://mod.io/g/twopointcampus/r/initial-modding-setup ／ https://mod.io/g/twopointcampus/r/modding-items-how-to ／ https://mod.io/g/twopointcampus/r/uploaddownload-mods
- Steam 公式スレッド（MOD対応の説明）: https://steamcommunity.com/app/1649080/discussions/3/3828665650629679870
- 5ch の設定ファイル書き換え報告: https://egg.5ch.io/test/read.cgi/game/1697107948/964

## 7. 調査に使ったツール

- Python 3.11 + UnityPy 1.25.4（バンドルの解析。バンドルの Unity バージョン表記が削除されているため `FALLBACK_UNITY_VERSION = "2020.3.47f1"` を指定）
