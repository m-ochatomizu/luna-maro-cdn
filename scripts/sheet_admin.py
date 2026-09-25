#!/usr/bin/env python3
"""
投稿カレンダー(Google Sheets)を操作する管理ツール。

post_scheduled.py と同じ Google Sheets API 認証(サービスアカウント)を
再利用する。本番の投稿実行ロジック(post_scheduled.py)には一切関与しない。
GitHub Actions(sheet-admin.yml)から呼ばれる想定。

役割分担: Cowork側の投稿案生成タスクは内容(キャラクター・素材・キャプション・
ハッシュタグ・ALT)を決めるところまでを担当し、git/GitHub操作(このツールの
呼び出しを含む)は一切行わない。候補内容をCode(Claude Code)に渡し、Codeが
propose(空き枠の割り当てとSheetへの追加)・approve等を実行する。

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
                                        # JSON_FILEがオブジェクトなら、id/date/time
                                        # まで指定済みの候補を1件そのまま追加する
                                        # (手動テスト用)。
                                        # JSON_FILEが配列なら、Cowork側が内容だけ
                                        # (account/image_ref/caption/hashtags/alt/
                                        # notes省略可)決めた候補群に、次の空き投稿枠
                                        # (火・金19:00)を順番に割り当てて追加する。
                                        # id/date/timeはこちらで自動採番するため
                                        # 含めない。90日再利用禁止(image_ref)に
                                        # かかる候補は追加せずスキップする。
                                        # 1件の失敗が他の候補に波及しないよう、
                                        # 候補ごとに独立して結果を報告する。
                                        # git/GitHub操作はCowork側では行わず、
                                        # この経路(Code側からの呼び出し)に一本化する。
                                        # いずれの形式でもキャプションの改行・絵文字・
                                        # 引用符をシェル引数展開なしで安全に渡すため
                                        # JSONファイル経由にしている。caption/alt/
                                        # hashtagsのブランド規則(旧post_queue.pyの
                                        # validate()相当)をpropose_row内で検証し、
                                        # 違反があれば追加しない。
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
from datetime import datetime, timedelta
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


# 以下、旧方式 post_queue.py の next_slot()/history.py の in_cooldown() を移植したもの。
# 「Coworkは内容だけ決め、枠の割り当てとSheetへの書き込み(git/GitHub操作)はCode側が
# 担当する」という役割分担のため、空き枠の判定にはSheetの現在の状態を読む必要があり、
# それを行うのは投稿実行(post_scheduled.py)と同じくこちら側の役目になる。
SLOT_WEEKDAYS = (1, 4)  # 火・金(月曜=0)。増やさない
SLOT_HOUR = 19
SLOT_MINUTE = 0
REPOST_COOLDOWN_DAYS = 90


def next_slot(after: datetime) -> datetime:
    """`after` より後で最初に来る投稿枠(火・金19:00 JST)を返す。"""
    candidate = after.replace(hour=SLOT_HOUR, minute=SLOT_MINUTE, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    while candidate.weekday() not in SLOT_WEEKDAYS:
        candidate += timedelta(days=1)
    return candidate


def make_post_id(slot: datetime, account: str) -> str:
    return f"{slot:%Y%m%d_%H%M}_{account.lower()}_01"


def occupied_slots(rows: list[list[str]]) -> set[tuple[str, str]]:
    """既に埋まっている(date, time)の集合。statusを問わず埋まっているとみなす
    (下書き中の枠も次の候補が二重に狙わないようにするため)。"""
    date_i, time_i = COLUMNS.index("date"), COLUMNS.index("time")
    occupied = set()
    for row in rows:
        if not row or not row[0]:
            continue
        date = row[date_i] if len(row) > date_i else ""
        time = row[time_i] if len(row) > time_i else ""
        if date and time:
            occupied.add((date, time))
    return occupied


def last_posted_at(rows: list[list[str]], image_ref: str) -> datetime | None:
    image_ref_i = COLUMNS.index("image_ref")
    status_i = COLUMNS.index("status")
    posted_at_i = COLUMNS.index("posted_at")
    latest = None
    for row in rows:
        if len(row) <= status_i or row[status_i] != "posted":
            continue
        if len(row) <= image_ref_i or row[image_ref_i] != image_ref:
            continue
        posted_at = row[posted_at_i] if len(row) > posted_at_i else ""
        if not posted_at:
            continue
        try:
            t = datetime.fromisoformat(posted_at).astimezone(JST)
        except ValueError:
            continue
        if latest is None or t > latest:
            latest = t
    return latest


def in_cooldown(rows: list[list[str]], image_ref: str, now: datetime) -> bool:
    """この素材を今投稿すると90日間隔より近すぎるか(旧history.py in_cooldown()相当)。"""
    last = last_posted_at(rows, image_ref)
    if last is None:
        return False
    return (now - last).total_seconds() / 86400 < REPOST_COOLDOWN_DAYS


def schedule_candidates(token: str, spreadsheet_id: str, tab: str, candidates: list[dict]) -> None:
    """Cowork側が内容だけ決めた候補群(id/date/timeなし)に、次の空き投稿枠を順番に
    割り当てて追加する。90日再利用禁止にかかる候補はスキップする。

    「1件の失敗や修正が他の投稿に波及しない」という設計方針(post_scheduled.pyと同じ)
    に合わせ、候補ごとに独立してtry/exceptし、1件のエラーで残りの処理を止めない。"""
    rows = get_rows(token, spreadsheet_id, tab)
    occupied = occupied_slots(rows)
    now = datetime.now(JST)
    cursor = now

    for i, cand in enumerate(candidates, start=1):
        try:
            missing = [k for k in ("account", "image_ref", "caption", "hashtags", "alt") if not cand.get(k)]
            if missing:
                raise SystemExit(f"必要なキーがありません: {missing}")
            image_ref = cand["image_ref"]
            if in_cooldown(rows, image_ref, now):
                raise SystemExit(f"{image_ref} は90日再利用禁止の期間内のため候補から除外しました")

            slot = next_slot(cursor)
            while (f"{slot:%Y-%m-%d}", f"{slot:%H:%M}") in occupied:
                slot = next_slot(slot)
            post_id = make_post_id(slot, cand["account"])

            propose_row(
                token, spreadsheet_id, tab,
                post_id=post_id, date=f"{slot:%Y-%m-%d}", time=f"{slot:%H:%M}",
                account=cand["account"], image_ref=image_ref, caption=cand["caption"],
                hashtags=cand["hashtags"], alt=cand["alt"], notes=cand.get("notes", ""),
            )
            occupied.add((f"{slot:%Y-%m-%d}", f"{slot:%H:%M}"))
            cursor = slot
        except SystemExit as e:
            print(f"[{i}件目] スキップ: {e}")


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
        if isinstance(data, list):
            schedule_candidates(token, spreadsheet_id, tab, data)
        else:
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
