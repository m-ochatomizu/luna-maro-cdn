#!/usr/bin/env python3
"""
投稿カレンダー(Google Sheets)を操作する管理ツール。

post_scheduled.py と同じ Google Sheets API 認証(サービスアカウント)を
再利用する。本番の投稿実行ロジック(post_scheduled.py)には一切関与しない。
GitHub Actions(sheet-admin.yml)から呼ばれる想定で、Cowork側の投稿案生成
タスクが候補行を追加する際にも使う(propose)。

使い方:
    python3 sheet_admin.py append      # dry_run検証用のテスト行を末尾に追加
    python3 sheet_admin.py clear ROW_NUMBER
                                        # 指定した行のセル内容だけを空にする
                                        # (行そのものは削除しない。空行は
                                        # post_scheduled.py側で無視されるため
                                        # 安全)
    python3 sheet_admin.py list        # 全行をid/status/date/account付きで
                                        # 一覧表示(行番号・空き枠確認用)
    python3 sheet_admin.py used        # status=posted の行を image_ref と
                                        # posted_at 付きで一覧表示
                                        # (90日再利用禁止の判定に使う)
    python3 sheet_admin.py show POST_ID
                                        # 指定idの全列を表示
    python3 sheet_admin.py propose JSON_FILE
                                        # status=draft で新しい候補行を1件追加
                                        # (Cowork側の投稿案生成タスクが使う)。
                                        # JSON_FILEはid/date/time/account/
                                        # image_ref/caption/hashtags/alt/notes
                                        # をキーに持つファイル(notesは省略可)。
                                        # キャプションの改行・絵文字・引用符を
                                        # シェル引数展開なしで安全に渡すため。
                                        # caption/alt/hashtagsのブランド規則
                                        # (旧post_queue.pyのvalidate()相当)を
                                        # ここで検証し、違反があれば追加しない。
    python3 sheet_admin.py approve POST_ID [APPROVED_BY]
                                        # 指定したidの行の status を approved にし、
                                        # approved_by/approved_at を記録する
                                        # (半兵衛レビュー・殿承認を経た本番行に対して
                                        # 使う。テスト行専用ではない)。本文に絵文字が
                                        # 無い場合は拒否する(旧post_queue.approve()と同じ)。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

JST = ZoneInfo("Asia/Tokyo")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
COLUMNS = [
    "id", "date", "time", "account", "image_ref", "caption", "hashtags", "alt",
    "status", "approved_by", "approved_at", "posted_at", "ig_media_id",
    "permalink", "error", "notes",
]

# 以下、旧方式 post_queue.py の validate()/approve() が担っていたブランド運用の
# 検証ルールを移植したもの(設計書§2.2・§2.4・§2.8、殿指示 2026-08-15/2026-08-20)。
# post_scheduled.py(投稿実行)はSheetの内容を検証しない前提のため、propose/approve
# の入口で防がないとブランド規則が仕組みで守られなくなる(「検証で縛るのは
# 忘れても仕組みで防ぐため」という旧方式のコメントをそのまま踏襲)。
CAPTION_MAX = 100
ALT_MAX = 100
HASHTAG_MAX = 10
BRAND_HASHTAG = "#ルナまろ🐾"
DEPRECATED_HASHTAGS = ("#ルナとまろ",)

_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x1F000, 0x1F0FF),
)


def has_emoji(text: str) -> bool:
    return any(any(lo <= ord(c) <= hi for lo, hi in _EMOJI_RANGES) for c in text or "")


def validate_candidate(*, caption: str, hashtags: str, alt: str) -> None:
    """propose(draft追加)の入口で弾く。承認可否に関わる絵文字チェックはここでは
    行わない(旧方式と同じく、書きかけの下書きを弾かないため。approve側で見る)。"""
    if len(caption) > CAPTION_MAX:
        raise SystemExit(f"caption が{len(caption)}字あります。{CAPTION_MAX}字以内にしてください。")

    if not alt or not alt.strip():
        raise SystemExit("alt(代替テキスト)が空です。全投稿に必須です。")
    if len(alt) > ALT_MAX:
        raise SystemExit(f"alt が{len(alt)}字あります。{ALT_MAX}字以内にしてください。")
    if "#" in alt or "http" in alt:
        raise SystemExit("alt にハッシュタグやURLを含めないでください。見えるものの説明だけを書きます。")
    if has_emoji(alt):
        raise SystemExit("alt に絵文字を入れないでください(読み上げソフトが絵文字名を読み上げてしまいます)。")

    tags = [t for t in hashtags.split() if t]
    if len(tags) > HASHTAG_MAX:
        raise SystemExit(f"hashtags が{len(tags)}個あります。{HASHTAG_MAX}個以内にしてください。")
    for t in tags:
        if not t.startswith("#"):
            raise SystemExit(f"ハッシュタグは # で始めてください: {t!r}")
    if BRAND_HASHTAG not in tags:
        raise SystemExit(f"ブランドの識別タグ {BRAND_HASHTAG} が入っていません。hashtags に加えてください。")
    for t in tags:
        if t in DEPRECATED_HASHTAGS:
            raise SystemExit(f"{t} は使わないと決めたタグです。{BRAND_HASHTAG} へ一本化してください。")

# post_scheduled.py の REQUIRED_COLUMNS と同じ並び(A〜P列)
TEST_ROW = [
    "TEST_dryrun_001",
    "2026-09-24",
    "00:01",
    "maro",
    "子猫時代/IMG_3502.jpg",
    "テスト投稿(dry_run検証用)",
    "#test",
    "テスト画像",
    "approved",
    "maria",
    "",
    "",
    "",
    "",
    "",
    "dry_run検証用の仮データ。検証後に削除",
]


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"環境変数 {name} が設定されていません")
    return value


def get_token(sa_path: str) -> str:
    creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SHEETS_SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds.token


def append_row(token: str, spreadsheet_id: str, tab: str) -> None:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        f"/values/'{tab}'!A1:append"
    )
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [TEST_ROW]},
        timeout=30,
    )
    resp.raise_for_status()
    updated_range = resp.json()["updates"]["updatedRange"]
    print(f"追加しました: {updated_range}")


def clear_row(token: str, spreadsheet_id: str, tab: str, row_number: int) -> None:
    rng = f"'{tab}'!A{row_number}:P{row_number}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}:clear"
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    print(f"クリアしました: {rng}")


def get_rows(token: str, spreadsheet_id: str, tab: str) -> list[list[str]]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/'{tab}'!A1:P1000"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("values", [])


def list_rows(token: str, spreadsheet_id: str, tab: str) -> None:
    rows = get_rows(token, spreadsheet_id, tab)
    for i, row in enumerate(rows, start=1):
        post_id = row[0] if row else ""
        date = row[1] if len(row) > 1 else ""
        account = row[3] if len(row) > 3 else ""
        status = row[8] if len(row) > 8 else ""
        print(f"行{i}: id={post_id!r} date={date!r} account={account!r} status={status!r}")


def used_images(token: str, spreadsheet_id: str, tab: str) -> None:
    """status=posted の行を image_ref・posted_at 付きで一覧表示する。
    候補生成側が90日再利用禁止を判定するための材料(旧方式のhistory.pyに相当)。"""
    rows = get_rows(token, spreadsheet_id, tab)
    image_ref_i = COLUMNS.index("image_ref")
    status_i = COLUMNS.index("status")
    posted_at_i = COLUMNS.index("posted_at")
    for row in rows:
        if len(row) <= status_i or row[status_i] != "posted":
            continue
        image_ref = row[image_ref_i] if len(row) > image_ref_i else ""
        posted_at = row[posted_at_i] if len(row) > posted_at_i else ""
        print(f"image_ref={image_ref!r} posted_at={posted_at!r}")


def show_row(token: str, spreadsheet_id: str, tab: str, post_id: str) -> None:
    rows = get_rows(token, spreadsheet_id, tab)
    for i, row in enumerate(rows, start=1):
        if row and row[0] == post_id:
            padded = row + [""] * (len(COLUMNS) - len(row))
            for name, value in zip(COLUMNS, padded):
                print(f"{name}: {value!r}")
            return
    raise SystemExit(f"id={post_id!r} の行が見つかりません")


def col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def propose_row(
    token: str, spreadsheet_id: str, tab: str, *,
    post_id: str, date: str, time: str, account: str, image_ref: str,
    caption: str, hashtags: str, alt: str, notes: str,
) -> None:
    """status=draft で新しい候補行を追加する(Cowork側の投稿案生成タスク用)。
    半兵衛レビュー・殿承認は別途status=approvedへの変更(approveコマンド)で行う
    ため、ここでは絶対にapprovedを書き込まない。"""
    validate_candidate(caption=caption, hashtags=hashtags, alt=alt)
    rows = get_rows(token, spreadsheet_id, tab)
    for row in rows:
        if row and row[0] == post_id:
            raise SystemExit(f"id={post_id!r} は既に存在します(重複追加を防止)")

    row = [
        post_id, date, time, account, image_ref, caption, hashtags, alt,
        "draft", "", "", "", "", "", "", notes,
    ]
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/'{tab}'!A1:append"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]},
        timeout=30,
    )
    resp.raise_for_status()
    updated_range = resp.json()["updates"]["updatedRange"]
    print(f"候補を追加しました(draft): {updated_range}(id={post_id})")


def approve_row(token: str, spreadsheet_id: str, tab: str, post_id: str, approved_by: str) -> None:
    rows = get_rows(token, spreadsheet_id, tab)
    row_number = None
    matched_row: list[str] = []
    for i, row in enumerate(rows, start=1):
        if row and row[0] == post_id:
            row_number = i
            matched_row = row
            break
    if row_number is None:
        raise SystemExit(f"id={post_id!r} の行が見つかりません")

    caption_i = COLUMNS.index("caption")
    caption = matched_row[caption_i] if len(matched_row) > caption_i else ""
    if not has_emoji(caption):
        # 旧方式 post_queue.approve() と同じ判断: 絵文字チェックは下書き保存時ではなく
        # 承認の瞬間に行う(殿指示 2026-08-20)。書き直し中の下書きを弾かないため。
        raise SystemExit(
            f"{post_id} の本文に絵文字がありません(殿指示 2026-08-20)。"
            f"1〜2つ入れてから承認してください。現在の本文: {caption!r}"
        )

    status_col = col_letter(COLUMNS.index("status"))
    approved_by_col = col_letter(COLUMNS.index("approved_by"))
    approved_at_col = col_letter(COLUMNS.index("approved_at"))
    now_iso = datetime.now(JST).isoformat(timespec="seconds")

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate"
    body = {
        "valueInputOption": "RAW",
        "data": [
            {"range": f"'{tab}'!{status_col}{row_number}", "values": [["approved"]]},
            {"range": f"'{tab}'!{approved_by_col}{row_number}", "values": [[approved_by]]},
            {"range": f"'{tab}'!{approved_at_col}{row_number}", "values": [[now_iso]]},
        ],
    }
    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=30)
    resp.raise_for_status()
    print(f"承認しました: 行{row_number}(id={post_id}, approved_by={approved_by}, approved_at={now_iso})")


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 1

    sa_path = env("GOOGLE_SERVICE_ACCOUNT_FILE")
    spreadsheet_id = env("GOOGLE_SHEET_ID")
    tab = env("GOOGLE_SHEET_TAB")
    token = get_token(sa_path)

    if argv[0] == "append":
        append_row(token, spreadsheet_id, tab)
    elif argv[0] == "clear":
        if len(argv) < 2:
            raise SystemExit("clear には行番号が必要です: clear ROW_NUMBER")
        clear_row(token, spreadsheet_id, tab, int(argv[1]))
    elif argv[0] == "list":
        list_rows(token, spreadsheet_id, tab)
    elif argv[0] == "used":
        used_images(token, spreadsheet_id, tab)
    elif argv[0] == "propose":
        if len(argv) < 2:
            raise SystemExit("propose には JSON_FILE が必要です: propose JSON_FILE")
        with open(argv[1], encoding="utf-8") as f:
            data = json.load(f)
        missing = [
            k for k in ("id", "date", "time", "account", "image_ref", "caption", "hashtags", "alt")
            if k not in data
        ]
        if missing:
            raise SystemExit(f"JSON_FILEに必要なキーがありません: {missing}")
        propose_row(
            token, spreadsheet_id, tab,
            post_id=data["id"], date=data["date"], time=data["time"], account=data["account"],
            image_ref=data["image_ref"], caption=data["caption"], hashtags=data["hashtags"],
            alt=data["alt"], notes=data.get("notes", ""),
        )
    elif argv[0] == "show":
        if len(argv) < 2:
            raise SystemExit("show には post_id が必要です: show POST_ID")
        show_row(token, spreadsheet_id, tab, argv[1])
    elif argv[0] == "approve":
        if len(argv) < 2:
            raise SystemExit("approve には post_id が必要です: approve POST_ID [APPROVED_BY]")
        approved_by = argv[2] if len(argv) > 2 else "殿承認(半兵衛レビュー済み・Slack)"
        approve_row(token, spreadsheet_id, tab, argv[1], approved_by)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
