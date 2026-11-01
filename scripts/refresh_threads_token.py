#!/usr/bin/env python3
"""
Threads アクセストークンを定期的に更新し、GitHub Secrets の
THREADS_ACCESS_TOKEN を新しい値で上書きする。

scripts/refresh_ig_token.py と全く同じ構造・同じ判断基準をThreads API
(graph.threads.net)向けに移植したもの(2026-11-01、Threads同時投稿対応で追加)。
長期トークンは60日で失効するため、週1回の更新なら常に大きな余裕がある。

発行から24時間経っていないトークンは更新できない(APIの仕様)。この場合は
異常ではないので、その旨を出力して正常終了(exit 0)する。それ以外の失敗は
異常として exit 1 で終える(GitHub Actionsの実行が失敗として記録され、
リポジトリの通知で気づける)。

THREADS_ACCESS_TOKEN が未設定(Threads同時投稿を使っていない)場合は、
更新対象が無いだけなので異常ではなく正常終了する。
"""
from __future__ import annotations

import base64
import os
import sys

import requests
from nacl import encoding, public

THREADS_API = os.environ.get("THREADS_API", "https://graph.threads.net")


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name)
    if required and not value:
        raise SystemExit(f"環境変数 {name} が設定されていません")
    return value or ""


def refresh_token(current_token: str) -> tuple[str, int]:
    resp = requests.get(
        f"{THREADS_API}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": current_token},
        timeout=30,
    )
    if resp.status_code != 200:
        body = resp.text[:500]
        raise RuntimeError(f"Threads APIがエラーを返しました(HTTP {resp.status_code}): {body}")
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"更新後のトークンが返りませんでした: {data}")
    return data["access_token"], int(data.get("expires_in", 0))


def github_public_key(repo: str, gh_token: str) -> tuple[str, str]:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["key_id"], data["key"]


def encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def update_github_secret(repo: str, gh_token: str, secret_name: str, secret_value: str) -> None:
    key_id, public_key_b64 = github_public_key(repo, gh_token)
    encrypted_value = encrypt_secret(public_key_b64, secret_value)
    resp = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
        json={"encrypted_value": encrypted_value, "key_id": key_id},
        timeout=30,
    )
    resp.raise_for_status()


def main() -> int:
    current_token = env("THREADS_ACCESS_TOKEN", required=False)
    if not current_token:
        print("THREADS_ACCESS_TOKENが未設定のため、更新対象がありません(Threads同時投稿は未導入)")
        return 0

    cdn_repo = env("CDN_REPO")
    gh_token = env("GH_ADMIN_TOKEN")

    try:
        new_token, expires_in = refresh_token(current_token)
    except RuntimeError as exc:
        message = str(exc)
        if "24" in message and ("hour" in message.lower() or "time" in message.lower()):
            # 発行から24時間経っていない可能性が高い。異常ではないので正常終了する。
            print(f"更新をスキップしました(発行直後の可能性): {message}")
            return 0
        print(f"エラー: トークン更新に失敗しました: {message}", file=sys.stderr)
        return 1

    days = expires_in // 86400
    print(f"Threadsトークンを更新しました。有効期限: 残り約{days}日")

    update_github_secret(cdn_repo, gh_token, "THREADS_ACCESS_TOKEN", new_token)
    print(f"GitHub Secrets の THREADS_ACCESS_TOKEN を更新しました({cdn_repo})")

    if days < 14:
        print(f"警告: 更新後も残り{days}日しかありません。Threads側の認証設定を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
