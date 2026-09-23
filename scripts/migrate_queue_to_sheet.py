#!/usr/bin/env python3
"""
旧方式(luna-maro/distribution/queue/*.json)を新方式の投稿カレンダー
(Google Sheets・1タブに通年分をまとめる設計)の行フォーマットへ変換する、
一度きりの移行スクリプト。

Sheetsへ直接書き込まず、単一のCSVを出力するだけにしてある。本番データの
一括書き込みをスクリプトに無条件に任せず、人間が中身を見てから貼り付けられる
ようにするため(移行計画で重視している「戻りの少なさ」と同じ理由:
自動化の失敗が本番シートを壊さない)。

前提: このスクリプトは post_runner.sh の自動commit/push化(移行計画Step1)が
殿のMacへ反映され、少なくとも一度pushされた後に実行すること。反映前の
luna-maroリポジトリは古い状態(2026-09-13/14時点)のままで、直近の投稿結果や
新規候補が欠けている。

使い方:
    python3 migrate_queue_to_sheet.py /path/to/luna-maro/distribution/queue --out schedule.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

COLUMNS = [
    "id", "date", "time", "account", "image_ref", "caption", "hashtags", "alt",
    "status", "approved_by", "approved_at", "posted_at", "ig_media_id",
    "permalink", "error", "notes",
]

# 旧方式(post_queue.py)のstatus enum: draft/approved/published/failed
STATUS_MAP = {
    "draft": "draft",
    "approved": "approved",
    "published": "posted",
    "failed": "failed",
}


def convert(post: dict) -> dict:
    scheduled_at = post["scheduled_at"]  # 例: 2026-09-18T19:00:00+09:00
    date, time_part = scheduled_at.split("T")
    time_hhmm = time_part[:5]

    media = post["media"][0] if post.get("media") else {}
    character = media.get("character", "")
    account = character.strip().lower()

    published = post.get("published") or {}
    failed = post.get("failed") or {}

    if len(post.get("media", [])) > 1:
        notes_extra = f"[要確認: メディア{len(post['media'])}件中1件目のみ変換]"
    else:
        notes_extra = ""

    return {
        "id": post["post_id"],
        "date": date,
        "time": time_hhmm,
        "account": account,
        "image_ref": media.get("source_path", ""),
        "caption": post.get("caption", ""),
        "hashtags": " ".join(post.get("hashtags", [])),
        "alt": media.get("alt", ""),
        "status": STATUS_MAP.get(post["status"], post["status"]),
        "approved_by": post.get("created_by", "") if post.get("approved_at") else "",
        "approved_at": post.get("approved_at", "") or "",
        "posted_at": published.get("published_at", "") or "",
        "ig_media_id": published.get("media_id", "") or "",
        "permalink": published.get("permalink", "") or "",
        "error": failed.get("reason", "") or "",
        "notes": " ".join(filter(None, [character, media.get("era", ""), notes_extra])),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queue_dir", help="luna-maro/distribution/queue のパス")
    parser.add_argument("--out", default="schedule.csv", help="出力するCSVのパス")
    args = parser.parse_args(argv)

    queue_dir = Path(args.queue_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for path in sorted(queue_dir.glob("*.json")):
        try:
            post = json.loads(path.read_text(encoding="utf-8"))
            rows.append(convert(post))
        except Exception as exc:  # noqa: BLE001 - 1件の変換失敗で全体を止めない
            skipped.append((path.name, str(exc)))
            continue

    rows.sort(key=lambda r: (r["date"], r["time"]))
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"書き出し: {out_path}({len(rows)}件)")

    if skipped:
        print("\n変換をスキップした(要確認)ファイル:", file=sys.stderr)
        for name, reason in skipped:
            print(f"  {name}: {reason}", file=sys.stderr)

    print(
        "\n次の手順: CSVの内容を確認のうえ、Google Sheetsの投稿カレンダー(1タブ・"
        "通年分)へファイル→インポート→アップロードで取り込んでください。"
        "status=posted の行は再投稿されないことをpost_scheduled.pyのロジックで"
        "再確認してから本番運用を開始してください。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
