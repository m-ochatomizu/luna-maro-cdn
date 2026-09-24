#!/usr/bin/env python3
"""
dry_run検証用にテスト行を1行追加/削除する、使い捨てのツール。

post_scheduled.py と同じ Google Sheets API 認証(サービスアカウント)を
再利用する。本番の投稿ロジックには一切関与しない。

使い方:
    python3 sheet_test_row.py append   # 末尾にテスト行を1行追加し、書き込んだ
                                        # 行番号を標準出力に表示する
    python3 sheet_test_row.py clear ROW_NUMBER
                                        # 指定した行のセル内容だけを空にする
                                        # (行そのものは削除しない。空行は
                                        # post_scheduled.py側で無視されるため
                                        # 安全)
"""
from __future__ import annotations

import os
import sys

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
