# luna-maro-cdn
LUNA MARO 配信インフラの公開ステージング領域。投稿直前の画像のみを一時的に置き、投稿完了後に削除する。素材のSSOTは非公開リポジトリ luna-maro 側。

## 自動投稿の仕組み

企画・承認とInstagram投稿の実行を分離している。

1. **企画**: CreaterBrain(または人間)が投稿案(画像・キャプション・日時)を
   [投稿カレンダー(Google Sheets)](https://docs.google.com/spreadsheets/d/1rZc7y95WA_7m406On-HY60p3G8d46sySgtqrB23oS9Y/edit)
   に1行1投稿で追加する(`status = draft` または `ready_for_review`)。
   1タブに通年分をまとめる設計で、月をまたいでまとめて計画できる
   (行は`id`ごとに独立して処理されるため、タブを分けなくても1件の失敗が
   他の行に波及しない設計は変わらない)。
2. **人間の最終確認**: 人間がシート上で内容を確認し、問題なければ
   `status` を `approved` に変更する。これが唯一の承認操作。
3. **自動投稿**: `.github/workflows/instagram-post.yml` が15分おきに実行され、
   `scripts/post_scheduled.py` が「`status = approved` かつ予定日時を過ぎている」
   行だけを対象に、画像をこのリポジトリの `staging/` に一時公開して
   Instagram API(Instagram ログイン方式・`graph.instagram.com`)で投稿し、
   完了後に `staging/` から削除、シートの `status` を `posted`(失敗時は
   `failed`)に更新する。

各行は完全に独立して処理されるため、1件の失敗や修正が他の投稿・他の月の
計画に影響しない。失敗した行は `error` 列に理由が入るので、修正して
`status` を `approved` に戻せば次回実行時に自動でリトライされる(過去分の
やり直しのために他の行を触る必要はない)。

ローカルの `git clone` した作業ディレクトリを使い回さず、GitHub Contents API
のみで `staging/` の追加・削除を行う設計のため、以前 Cowork 上の自動化で
発生していた「git lock により自動更新が止まる」問題は構造的に起きない
(各GitHub Actions実行は毎回クリーンな新規VMで動く)。

### シートの列

`id, date, time, account, image_ref, caption, hashtags, alt, status, approved_by, approved_at, posted_at, ig_media_id, permalink, error, notes`

- `account`: `luna` または `maro`(投稿する画像がどちらの猫の話題かを示す情報。
  Instagramアカウントは1つしかなく認証情報の選択には使わない。下記参照)
- `image_ref`: 非公開リポジトリ `luna-maro` 内の画像パス
- `alt`: 画像の代替テキスト(視覚障害者向け説明文)。旧方式では全投稿に必須で
  付けていたため、本方式でも引き継ぐ
- `status`: `draft` → `ready_for_review` → `approved` → `posted` / `failed`
  (このスクリプトが書き込むのは `approved` の行に対する `posted`/`failed` のみ)
- `permalink`: 投稿成功後にInstagramの公開URLを自動で書き込む(取得できなくても
  投稿自体の成否には影響させない)

**Instagramアカウントは「ルナまろ」1つのみ。** 旧方式(`ig_token.py`/
`publish.py`)を調査した結果、LUNA/MARO用に別アカウント・別トークンが
存在するわけではなく、単一のInstagramログイン方式トークンで `me/media` へ
投稿する設計だった(取り違えて別アカウントへ出す事故をそもそも起こせない
ようにするため、旧方式はビジネスアカウントIDを一切埋め込んでいない)。
本方式もこれに合わせ、`account`列は投稿先の選択には使わず、情報整理用の
メタデータとして扱う。

### 必要なリポジトリ設定(Settings → Secrets and variables → Actions)

Secrets:
- `GOOGLE_SERVICE_ACCOUNT_JSON`: シート読み書き用サービスアカウントの鍵(JSON)。
  対象シートをこのサービスアカウントに編集者共有しておくこと。
- `GOOGLE_SHEET_ID`: 投稿カレンダーのスプレッドシートID
- `SOURCE_REPO_TOKEN`: 非公開リポジトリ `luna-maro` を読み取り専用で参照できるトークン
- `IG_ACCESS_TOKEN`: 「ルナまろ」アカウントのInstagramログイン方式アクセストークン
  (旧方式で `~/CreatorBrain_secrets/instagram_token.txt` に保管されているものと同じ
  性質のトークン)。初回はMeta for Developersの「LUNA MARO 配信」アプリ、または
  旧方式で発行済みのトークンをそのまま設定する。以降は下記「アクセストークンの
  自動更新」により自動で更新され続ける
- `GH_ADMIN_TOKEN`: 上記 `IG_ACCESS_TOKEN` をGitHub Actionsが自動更新するための
  権限。fine-grained PAT を作成し、対象リポジトリを `luna-maro-cdn` のみに限定、
  Repository permissions の **Secrets を Read and write** のみ付与する(他の権限は
  一切不要)。有効期限は無期限、または定期的に手動更新できる長さに設定する

Variables:
- `SOURCE_REPO`: 例 `m-ochatomizu/luna-maro`
- `GOOGLE_SHEET_TAB`(必須): 投稿カレンダーのタブ名(1タブに通年分をまとめる設計のため固定値)

初回導入時は Actions の `workflow_dispatch` から `dry_run: true` で実行し、
ログだけを確認してから定期実行を有効にすることを推奨する。

### アクセストークンの自動更新

Instagramログイン方式の長期トークンは60日で失効する(放置すると投稿が
静かに止まる、このプロジェクトが繰り返し警戒してきた事故そのもの)。
旧方式は `ig_token.py` が投稿の都度トークンを自己更新していたが、本方式は
GitHub Actionsの毎回クリーンなVMで動くため、更新した値をどこかへ永続化する
必要がある。

`.github/workflows/refresh-ig-token.yml` が**毎週月曜**に
`scripts/refresh_ig_token.py` を実行し、Instagram APIでトークンを更新した
うえで、GitHub Secrets API経由で `IG_ACCESS_TOKEN` を新しい値に書き換える
(週1回なら60日の失効まで常に大きな余裕がある)。GitHub Secrets APIは
デフォルトの `GITHUB_TOKEN` では操作できないため、専用の `GH_ADMIN_TOKEN`
(Secrets権限のみのfine-grained PAT)を使う。

更新に失敗した場合はGitHub Actionsの実行自体が失敗扱いになり、リポジトリの
通知(既定でメール)で気づける。より積極的にSlackへ通知したい場合は、
Slack Incoming Webhookの secrets を追加して `refresh_ig_token.py` の末尾に
POSTを1行足すだけで対応できる(現時点では未実装)。

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
python3 scripts/migrate_queue_to_sheet.py /path/to/luna-maro/distribution/queue --out schedule.csv
```

1本のCSVが出力されるので、内容を確認してから投稿カレンダーのタブへ
「ファイル→インポート→アップロード」で取り込む。`status = posted` の行を
取り込んでも本方式が再投稿することはない(`posted_at` が入っている行は
処理対象外)。

### 3. 本方式の検証・本番切り替え

1. 上記「必要なリポジトリ設定」のSecrets/Variablesを設定
2. `workflow_dispatch` + `dry_run: true` で実行し、ログを確認
3. `dry_run: false` で1件、実際に画像投稿を通して検証
4. `.github/workflows/instagram-post.yml` の `schedule` (現在コメントアウトで凍結中)を有効化
5. 数投稿サイクル安定稼働を確認したら、旧方式(`publish.py`・`post_runner.sh`・
   launchd plist・`git_lock_sweep`)を撤去する
