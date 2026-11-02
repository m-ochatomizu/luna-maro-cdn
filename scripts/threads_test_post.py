#!/usr/bin/env python3
"""
Threads APIへの単発テスト投稿スクリプト(2026-11-02、Threads同時投稿導入の動作確認用)。

Google Sheet/Instagramには一切触れない。既存の素材(image_ref)を一時的に
CDN staging へ公開し、Threadsにだけ投稿して結果を表示したあと、staging
から削除する。post_scheduled.py のThreads関連関数・GitHub Contents API
ヘルパーをそのまま再利用する(ロジックの重複を避けるため)。

使い方(環境変数で指定):
    THREADS_IMAGE_REF: 非公開リポジトリ luna-maro 内の画像/動画パス
    THREADS_TEXT: 投稿文
    THREADS_MEDIA_TYPE: IMAGE(既定) または VIDEO
"""
from __future__ import annotations

import sys

from post_scheduled import (
    THREADS_POLL_TIMEOUT,
    THREADS_POLL_TIMEOUT_VIDEO,
    env,
    github_delete_file,
    github_get_file,
    github_put_file,
    pages_url,
    threads_create_container,
    threads_permalink,
    threads_publish,
    threads_wait_until_ready,
    wait_reachable,
)


def main() -> int:
    source_repo = env("SOURCE_REPO")
    source_token = env("SOURCE_REPO_TOKEN")
    cdn_repo = env("CDN_REPO")
    cdn_token = env("CDN_REPO_TOKEN")
    threads_token = env("THREADS_ACCESS_TOKEN")
    threads_user_id = env("THREADS_USER_ID")

    image_ref = env("THREADS_IMAGE_REF")
    text = env("THREADS_TEXT")
    media_type = (env("THREADS_MEDIA_TYPE", required=False, default="IMAGE") or "IMAGE").upper()
    is_video = media_type == "VIDEO"

    print(f"素材を取得: {image_ref}")
    content, _ = github_get_file(source_repo, image_ref, source_token)
    if not content:
        raise SystemExit(f"ソースリポジトリに素材が見つかりません: {source_repo}/{image_ref}")

    basename = image_ref.rsplit("/", 1)[-1]
    cdn_path = f"staging/{basename}"
    existing_content, existing_sha = github_get_file(cdn_repo, cdn_path, cdn_token)

    print(f"CDNへ公開: {cdn_repo}/{cdn_path}")
    put_sha = github_put_file(
        cdn_repo, cdn_path, content, f"staging: {basename}(Threadsテスト投稿用)", cdn_token, existing_sha
    )
    media_url = pages_url(cdn_repo, cdn_path)

    try:
        print(f"公開ステージングの到達待ち: {media_url}")
        wait_reachable(media_url)

        print(f"Threadsへ投稿: media_type={media_type}")
        creation_id = threads_create_container(
            threads_user_id, text, threads_token,
            media_url=media_url, alt="", media_type=media_type,
        )
        threads_wait_until_ready(
            creation_id, threads_token,
            timeout=THREADS_POLL_TIMEOUT_VIDEO if is_video else THREADS_POLL_TIMEOUT,
        )
        media_id = threads_publish(threads_user_id, creation_id, threads_token)
        permalink = threads_permalink(media_id, threads_token)
        print(f"成功: media_id={media_id} permalink={permalink}")
    finally:
        print(f"CDNから削除: {cdn_repo}/{cdn_path}")
        github_delete_file(
            cdn_repo, cdn_path, put_sha, f"staging: {basename} を削除(Threadsテスト投稿用)", cdn_token
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
