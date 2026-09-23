#!/usr/bin/env python3
"""
Instagram アクセストークン(Instagram ログイン方式)を定期的に更新し、
GitHub Secrets の IG_ACCESS_TOKEN を新しい値で上書きする。

旧方式(ig_token.py)は殿のMac上のファイルを直接上書きしていたが、本方式は
GitHub Actions の毎回クリーンなVMで動くため、更新した値をどこかへ
永続化しないと次回実行時に失われる。永続化先として GitHub Secrets を
書き換える(GitHub Secrets APIはデフォルトのGITHUB_TOKENでは操作できないため、
Secrets権限のみを持つ専用PATが別途必要。README参照)。

旧方式と違い、このスクリプトは「30日を切ったら更新」のような閾値判定はせず、
呼ばれるたびに無条件で更新して保存する(週1回程度の低頻度実行を想定しており、
毎回更新しても実害がなく、ロジックが単純なほうが事故りにくいため)。

発行から24時間経っていないトークンは更新できない(APIの仕様)。この場合は
異常ではないので、その旨を出力して正常終了(exit 0)する。それ以外の失敗は
異常として exit 1 で終える(GitHub Actionsの実行が失敗として記録され、
リポジトリの通知で気づける)。
"""
from __future__ import annotations

import base64
import os
import sys

import requests
from nacl import encoding, public

IG_API = os.environ.get("IG_API", "https://graph.instagram.com")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"環境変数 {name} が設定されていません")
    return value


def refresh_token(current_token: str) -> tuple[str, int]:
    resp = requests.get(
        f"{IG_API}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": current_token},
        timeout=30,
    )
    if resp.status_code != 200:
        body = resp.text[:500]
        raise RuntimeError(f"Instagram APIがエラーを返しました(HTTP {resp.status_code}): {body}")
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
    current_token = env("IG_ACCESS_TOKEN")
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
    print(f"トークンを更新しました。有効期限: 残り約{days}日")

    update_github_secret(cdn_repo, gh_token, "IG_ACCESS_TOKEN", new_token)
    print(f"GitHub Secrets の IG_ACCESS_TOKEN を更新しました({cdn_repo})")

    if days < 14:
        print(f"警告: 更新後も残り{days}日しかありません。Instagram側の認証設定を確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
