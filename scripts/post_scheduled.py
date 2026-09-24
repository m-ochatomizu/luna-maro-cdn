#!/usr/bin/env python3
"""
Google Sheets の投稿カレンダーを読み、承認済みかつ投稿時刻を過ぎた行を
Instagram Graph API で自動投稿する。

設計方針(要件): 「1ヶ月分まとめて計画しても、1件の失敗や修正が他の投稿に
波及しない」こと。そのため各行は完全に独立して処理し、失敗しても他の行の
処理は継続する。承認(status=approved)は人間がスプレッドシート上で明示的に
行い、このスクリプトは approved 以外の行(draft/ready_for_review/rejected/
posted/failed)には一切書き込まない。

ローカルの git checkout は状態を持たないため(GitHub Contents API のみを
使用)、以前 Cowork 上の自動化で発生していた git ロックによる詰まりは
構造的に発生しない。
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

JST = ZoneInfo("Asia/Tokyo")
# Instagram ログイン方式のAPI(graph.facebook.com ではない)。旧方式(publish.py)と
# 同じホスト・バージョンに合わせてある。アカウントは常にトークンの持ち主(me)で、
# ビジネスアカウントIDという概念自体が無い(取り違えて別アカウントへ出す事故を
# そもそも起こせない設計。旧方式のコメントを踏襲)。
IG_API = os.environ.get("IG_API", "https://graph.instagram.com/v23.0")
IG_POLL_INTERVAL = 10   # 秒。旧方式(publish.py POLL_INTERVAL)と同じ
IG_POLL_TIMEOUT = 300   # 秒。旧方式(publish.py POLL_TIMEOUT)と同じ
# GitHub Pages の初回ビルドは数分かかることがある(旧方式で実測済み・2026-08-18)。
# raw.githubusercontent.com ではなく Pages 経由にする理由も旧方式を踏襲
# (画像として配信されることが仕様上明確なため・殿裁可2026-08-16)。
REACH_POLL_INTERVAL = 10  # 秒
REACH_TIMEOUT = 900       # 秒(15分)。旧方式(publish.py REACH_TIMEOUT)と同じ
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("post_scheduled")


class RowError(Exception):
    """1行の処理に失敗したことを表す。他の行の処理は止めない。"""


def env(name: str, required: bool = True, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"環境変数 {name} が設定されていません")
    return value


def with_retry(func, *, retries: int = 3, base_delay: float = 2.0, what: str = "request"):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            log.warning("%s に失敗(%d/%d): %s", what, attempt, retries, exc)
            if attempt < retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status >= 500 and attempt < retries:
                last_exc = exc
                log.warning("%s に失敗(5xx, %d/%d): %s", what, attempt, retries, exc)
                time.sleep(base_delay * (2 ** (attempt - 1)))
            else:
                raise
    raise last_exc


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_sheets_token(service_account_json_path: str) -> str:
    creds = service_account.Credentials.from_service_account_file(
        service_account_json_path, scopes=SHEETS_SCOPES
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


def sheets_get_values(token: str, spreadsheet_id: str, rng: str) -> list[list[str]]:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    )

    def _do():
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("values", [])

    return with_retry(_do, what="Sheets values.get")


def sheets_batch_update_cells(
    token: str, spreadsheet_id: str, updates: list[tuple[str, str]]
) -> None:
    """updates: [(a1_range, value), ...] を1回のAPI呼び出しでまとめて反映する。"""
    if not updates:
        return
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate"
    body = {
        "valueInputOption": "RAW",
        "data": [{"range": rng, "values": [[value]]} for rng, value in updates],
    }

    def _do():
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()

    with_retry(_do, what="Sheets values.batchUpdate")


def col_letter(index: int) -> str:
    """0-based column index -> A1形式の列名(A, B, ..., Z, AA, ...)"""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ---------------------------------------------------------------------------
# GitHub Contents API (ローカル git checkout を使わないので lock が発生しない)
# ---------------------------------------------------------------------------

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_get_file(repo: str, path: str, token: str) -> tuple[bytes, str | None]:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    def _do():
        resp = requests.get(url, headers=_gh_headers(token), timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    data = with_retry(_do, what=f"GitHub contents.get {repo}/{path}")
    if data is None:
        return b"", None
    content = base64.b64decode(data["content"])
    return content, data["sha"]


def github_put_file(
    repo: str, path: str, content: bytes, message: str, token: str, sha: str | None
) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
    }
    if sha:
        body["sha"] = sha

    def _do():
        resp = requests.put(url, headers=_gh_headers(token), json=body, timeout=60)
        resp.raise_for_status()
        return resp.json()

    data = with_retry(_do, what=f"GitHub contents.put {repo}/{path}")
    return data["content"]["sha"]


def github_delete_file(repo: str, path: str, sha: str, message: str, token: str) -> None:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = {"message": message, "sha": sha}

    def _do():
        resp = requests.delete(url, headers=_gh_headers(token), json=body, timeout=30)
        resp.raise_for_status()

    with_retry(_do, what=f"GitHub contents.delete {repo}/{path}")


# ---------------------------------------------------------------------------
# Instagram API (Instagram Login方式。常に me が対象)
# ---------------------------------------------------------------------------

def wait_reachable(url: str) -> None:
    """公開ステージングのURLがInstagram側から読める状態になるまで待つ。

    GitHub Pagesは初回ビルド直後だけ極端に遅いことがある(旧方式で実測)。
    ここを飛ばしてコンテナ作成へ進むと、Instagram側が画像を取得できず
    ERRORになる。
    """
    deadline = time.monotonic() + REACH_TIMEOUT
    last_status = None
    while True:
        try:
            resp = requests.get(url, timeout=30)
            last_status = resp.status_code
            if resp.status_code == 200:
                return
        except requests.RequestException as exc:
            last_status = str(exc)
        if time.monotonic() > deadline:
            raise RowError(
                f"公開ステージングのURLへ{REACH_TIMEOUT}秒たっても到達できません: "
                f"{url}(最後の状態: {last_status})"
            )
        time.sleep(REACH_POLL_INTERVAL)


def ig_create_container(image_url: str, caption: str, alt: str, token: str) -> str:
    url = f"{IG_API}/me/media"

    def _do():
        resp = requests.post(
            url,
            data={
                "image_url": image_url,
                "caption": caption,
                "alt_text": alt,
                "access_token": token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    data = with_retry(_do, what="IG media create")
    if not data.get("id"):
        raise RowError(f"コンテナIDが返りませんでした: {data}")
    return data["id"]


def ig_wait_until_ready(creation_id: str, token: str) -> None:
    url = f"{IG_API}/{creation_id}"
    deadline = time.monotonic() + IG_POLL_TIMEOUT
    while True:
        resp = requests.get(
            url,
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RowError(f"Instagramのメディア処理がERRORになりました(creation_id={creation_id})")
        if time.monotonic() > deadline:
            raise RowError(f"Instagramのメディア処理が{IG_POLL_TIMEOUT}秒たっても終わりません(最後の状態 {status})")
        time.sleep(IG_POLL_INTERVAL)


def ig_publish(creation_id: str, token: str) -> str:
    url = f"{IG_API}/me/media_publish"

    def _do():
        resp = requests.post(
            url,
            data={"creation_id": creation_id, "access_token": token},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    data = with_retry(_do, what="IG media_publish")
    if not data.get("id"):
        raise RowError(f"メディアIDが返りませんでした: {data}")
    return data["id"]


def ig_permalink(media_id: str, token: str) -> str:
    """取得できなくても投稿自体は成功しているので、失敗しても空文字を返すだけにする
    (旧方式 Instagram.permalink() と同じ判断: ここで例外を上げると
    「出したのに失敗扱い」になり、二重投稿を招く)。"""
    try:
        resp = requests.get(
            f"{IG_API}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("permalink", "") or ""
    except requests.RequestException:
        return ""


# ---------------------------------------------------------------------------
# 行の処理
# ---------------------------------------------------------------------------

@dataclass
class Row:
    row_number: int  # シート上の行番号(1-based, ヘッダーを含む)
    values: dict[str, str]


REQUIRED_COLUMNS = [
    "id", "date", "time", "account", "image_ref", "caption", "hashtags", "alt",
    "status", "approved_by", "approved_at", "posted_at", "ig_media_id",
    "permalink", "error", "notes",
]


def load_rows(token: str, spreadsheet_id: str, tab: str) -> tuple[list[Row], dict[str, int]]:
    raw = sheets_get_values(token, spreadsheet_id, f"'{tab}'!A1:Z1000")
    if not raw:
        raise SystemExit(f"シート '{tab}' が空、または見つかりません")
    header = raw[0]
    col_index = {name: i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_COLUMNS if c not in col_index]
    if missing:
        raise SystemExit(f"シート '{tab}' に必要な列がありません: {missing}")

    rows = []
    for i, raw_row in enumerate(raw[1:], start=2):
        values = {
            name: (raw_row[idx] if idx < len(raw_row) else "")
            for name, idx in col_index.items()
        }
        if not any(values.values()):
            continue  # 完全な空行はスキップ
        rows.append(Row(row_number=i, values=values))
    return rows, col_index


def is_due(row: Row, now: datetime) -> bool:
    if row.values["status"].strip().lower() != "approved":
        return False
    if row.values["posted_at"].strip():
        return False  # 既に投稿済み(念のための二重ガード)
    try:
        scheduled = datetime.strptime(
            f"{row.values['date'].strip()} {row.values['time'].strip()}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=JST)
    except ValueError as exc:
        raise RowError(f"date/timeの形式が不正です: {exc}") from exc
    return scheduled <= now


def pages_url(cdn_repo: str, cdn_path: str) -> str:
    """旧方式と同じくGitHub Pages経由のURLを使う(raw.githubusercontent.comではない。
    画像として配信されることが仕様上明確なため・殿裁可2026-08-16)。"""
    owner, repo = cdn_repo.split("/", 1)
    return f"https://{owner}.github.io/{repo}/{cdn_path}"


def process_row(row: Row, *, source_repo: str, source_token: str,
                 cdn_repo: str, cdn_token: str, ig_token: str, dry_run: bool) -> dict:
    """成功時は sheet に書き込むべき更新値を返す。失敗時は RowError を投げる。"""
    v = row.values
    account = v["account"].strip().lower()
    image_ref = v["image_ref"].strip()
    if not image_ref:
        raise RowError("image_ref が空です")

    log.info("[row %d] 画像を取得: %s", row.row_number, image_ref)
    content, _ = github_get_file(source_repo, image_ref, source_token)
    if not content:
        raise RowError(f"ソースリポジトリに画像が見つかりません: {source_repo}/{image_ref}")

    basename = image_ref.rsplit("/", 1)[-1]
    cdn_path = f"staging/{basename}"
    existing_content, existing_sha = github_get_file(cdn_repo, cdn_path, cdn_token)

    log.info("[row %d] CDNへ公開: %s/%s", row.row_number, cdn_repo, cdn_path)
    put_sha = github_put_file(
        cdn_repo, cdn_path, content, f"staging: {basename}", cdn_token, existing_sha
    )
    image_url = pages_url(cdn_repo, cdn_path)

    caption = v["caption"].strip()
    hashtags = v["hashtags"].strip()
    alt = v["alt"].strip()
    caption_full = f"{caption}\n\n{hashtags}" if hashtags else caption

    permalink = ""
    if dry_run:
        log.info("[row %d] DRY_RUN: Instagram投稿をスキップ (image_url=%s)", row.row_number, image_url)
        media_id = "DRY_RUN"
    else:
        log.info("[row %d] 公開ステージングの到達待ち: %s", row.row_number, image_url)
        wait_reachable(image_url)

        log.info("[row %d] Instagramへ投稿: account=%s", row.row_number, account)
        creation_id = ig_create_container(image_url, caption_full, alt, ig_token)
        ig_wait_until_ready(creation_id, ig_token)
        media_id = ig_publish(creation_id, ig_token)
        permalink = ig_permalink(media_id, ig_token)

        log.info("[row %d] CDNから削除: %s/%s", row.row_number, cdn_repo, cdn_path)
        github_delete_file(cdn_repo, cdn_path, put_sha, f"staging: {basename} を削除", cdn_token)

    now_iso = datetime.now(JST).isoformat(timespec="seconds")
    return {
        "status": "posted",
        "posted_at": now_iso,
        "ig_media_id": media_id,
        "permalink": permalink,
        "error": "",
    }


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    sa_path = env("GOOGLE_SERVICE_ACCOUNT_FILE")
    spreadsheet_id = env("GOOGLE_SHEET_ID")
    now = datetime.now(JST)
    # 投稿カレンダーは1タブに通年分をまとめる設計(2026-09-24)。
    # 月ごとにタブを分けない代わり、タブ名は固定で明示指定する。
    tab = env("GOOGLE_SHEET_TAB")

    source_repo = env("SOURCE_REPO")
    source_token = env("SOURCE_REPO_TOKEN")
    cdn_repo = env("CDN_REPO")
    cdn_token = env("CDN_REPO_TOKEN")
    ig_token = env("IG_ACCESS_TOKEN")

    sheets_token = get_sheets_token(sa_path)
    rows, col_index = load_rows(sheets_token, spreadsheet_id, tab)

    due_rows = [r for r in rows if is_due(r, now)]
    due_rows.sort(key=lambda r: (r.values["date"], r.values["time"]))

    log.info("シート '%s': %d行中 %d件が投稿対象(due)です", tab, len(rows), len(due_rows))

    failures = 0
    for row in due_rows:
        updates: dict[str, str]
        try:
            updates = process_row(
                row,
                source_repo=source_repo,
                source_token=source_token,
                cdn_repo=cdn_repo,
                cdn_token=cdn_token,
                ig_token=ig_token,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - 1行の失敗で他行を止めないため意図的に広く捕捉
            log.error("[row %d] 失敗: %s", row.row_number, exc)
            updates = {"status": "failed", "error": str(exc)[:500]}
            failures += 1

        if dry_run:
            log.info("[row %d] DRY_RUN: シート書き込みをスキップ: %s", row.row_number, updates)
            continue

        cell_updates = [
            (f"'{tab}'!{col_letter(col_index[name])}{row.row_number}", value)
            for name, value in updates.items()
        ]
        sheets_batch_update_cells(sheets_token, spreadsheet_id, cell_updates)

    if failures:
        log.warning("%d件が失敗しました。該当行を確認し、修正後 status を approved に戻してください。", failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
