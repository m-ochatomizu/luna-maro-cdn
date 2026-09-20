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

## 本番切り替え(旧Mac+launchd方式からの移行)

半兵衛レビューにより、旧方式(Mac常駐launchd+`publish.py`)から本方式への
全面移行がGOとなった(2026-09-20)。初回移行は画像投稿のみを対象とし、
動画/Reelsは新方式が安定稼働した後に別途実装する。

進め方: **旧方式停止 → 本方式の実装完了・検証 → 画像投稿で本番検証 →
本番運用開始 → 旧方式撤去**。新旧を並行稼働させないため、旧方式は検証開始前に
必ず停止する(検証・移行期間中は投稿間隔が一時的に空くことを許容する)。

### 1. 旧方式の停止(殿のMacで実行)

```bash
launchctl list | grep creatorbrain   # 現在の登録状態を確認

# 投稿ジョブ(com.creatorbrain.lunamaro.publish)のみ止める。
# git_lock_sweep(com.creatorbrain.gitlocksweep)は、Cowork側の候補生成・
# 報告タスクがまだgit操作を行う間の安全網として、旧方式撤去まで残す。
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.creatorbrain.lunamaro.publish.plist
# 上記が通らない場合(古いlaunchctl構文):
# launchctl unload ~/Library/LaunchAgents/com.creatorbrain.lunamaro.publish.plist

launchctl list | grep creatorbrain   # com.creatorbrain.lunamaro.publish が
                                      # 消えていればOK
```

### 2. 既存キューデータの移行

`luna-maro/distribution/queue/*.json`(旧方式のキュー)を投稿カレンダーの
行フォーマットへ変換する。**移行計画Step1(`post_runner.sh`の自動commit/push化)
が反映され、少なくとも一度pushされた後に実行すること**(反映前は
`luna-maro`のGitHub側が古い状態のままで、直近の投稿結果や新規候補が
欠けている)。

```bash
python3 scripts/migrate_queue_to_sheet.py /path/to/luna-maro/distribution/queue --out-dir ./out
```

月ごとのCSVが出力されるので、内容を確認してから投稿カレンダーの対応する
月タブへ貼り付ける。`status = posted` の行を貼り付けても本方式が
再投稿することはない(`posted_at` が入っている行は処理対象外)。

### 3. 本方式の検証・本番切り替え

1. 上記「必要なリポジトリ設定」のSecrets/Variablesを設定
2. `workflow_dispatch` + `dry_run: true` で実行し、ログを確認
3. `dry_run: false` で1件、実際に画像投稿を通して検証
4. `.github/workflows/instagram-post.yml` の `schedule` (現在コメントアウトで凍結中)を有効化
5. 数投稿サイクル安定稼働を確認したら、旧方式(`publish.py`・`post_runner.sh`・
   launchd plist・`git_lock_sweep`)を撤去する
