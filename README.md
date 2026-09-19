# luna-maro-cdn
LUNA MARO 配信インフラの公開ステージング領域。投稿直前の画像のみを一時的に置き、投稿完了後に削除する。素材のSSOTは非公開リポジトリ luna-maro 側。

## 自動投稿の仕組み

企画・承認とInstagram投稿の実行を分離している。

1. **企画**: CreaterBrain(または人間)が投稿案(画像・キャプション・日時)を
   [投稿カレンダー(Google Sheets)](https://docs.google.com/spreadsheets/d/1rZc7y95WA_7m406On-HY60p3G8d46sySgtqrB23oS9Y/edit)
   に1行1投稿で追加する(`status = draft` または `ready_for_review`)。
   月ごとにタブを分けて1ヶ月分まとめて計画できる。
2. **人間の最終確認**: 人間がシート上で内容を確認し、問題なければ
   `status` を `approved` に変更する。これが唯一の承認操作。
3. **自動投稿**: `.github/workflows/instagram-post.yml` が15分おきに実行され、
   `scripts/post_scheduled.py` が「`status = approved` かつ予定日時を過ぎている」
   行だけを対象に、画像をこのリポジトリの `staging/` に一時公開して
   Instagram Graph API で投稿し、完了後に `staging/` から削除、シートの
   `status` を `posted`(失敗時は `failed`)に更新する。

各行は完全に独立して処理されるため、1件の失敗や修正が他の投稿・他の月の
計画に影響しない。失敗した行は `error` 列に理由が入るので、修正して
`status` を `approved` に戻せば次回実行時に自動でリトライされる(過去分の
やり直しのために他の行を触る必要はない)。

ローカルの `git clone` した作業ディレクトリを使い回さず、GitHub Contents API
のみで `staging/` の追加・削除を行う設計のため、以前 Cowork 上の自動化で
発生していた「git lock により自動更新が止まる」問題は構造的に起きない
(各GitHub Actions実行は毎回クリーンな新規VMで動く)。

### シートの列

`id, date, time, account, image_ref, caption, hashtags, status, approved_by, approved_at, posted_at, ig_media_id, error, notes`

- `account`: `luna` または `maro`(Instagramアカウントの認証情報を選択するのに使う)
- `image_ref`: 非公開リポジトリ `luna-maro` 内の画像パス
- `status`: `draft` → `ready_for_review` → `approved` → `posted` / `failed`
  (このスクリプトが書き込むのは `approved` の行に対する `posted`/`failed` のみ)

### 必要なリポジトリ設定(Settings → Secrets and variables → Actions)

Secrets:
- `GOOGLE_SERVICE_ACCOUNT_JSON`: シート読み書き用サービスアカウントの鍵(JSON)。
  対象シートをこのサービスアカウントに編集者共有しておくこと。
- `GOOGLE_SHEET_ID`: 投稿カレンダーのスプレッドシートID
- `SOURCE_REPO_TOKEN`: 非公開リポジトリ `luna-maro` を読み取り専用で参照できるトークン
- `IG_ACCESS_TOKEN_LUNA` / `IG_BUSINESS_ACCOUNT_ID_LUNA`
- `IG_ACCESS_TOKEN_MARO` / `IG_BUSINESS_ACCOUNT_ID_MARO`

Variables:
- `SOURCE_REPO`: 例 `m-ochatomizu/luna-maro`
- `GOOGLE_SHEET_TAB`(任意): 未設定なら実行時点(JST)の `YYYY-MM` をタブ名として使う

初回導入時は Actions の `workflow_dispatch` から `dry_run: true` で実行し、
ログだけを確認してから定期実行を有効にすることを推奨する。
