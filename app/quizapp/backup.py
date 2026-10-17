"""データベースの定期バックアップ。

worker が BACKUP_INTERVAL_HOURS ごとに <データの場所>/backups/ に 1 つ作り、新しい順に
BACKUP_KEEP 個だけ残す。SQLite のオンラインバックアップ機能を使うので、web が動いたままでも
一貫した状態のコピーが取れる。手動で取るときは README の「バックアップ」を参照。

    python -m quizapp.backup          # いますぐ 1 つ作る
    python -m quizapp.backup --list   # 一覧
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta

PREFIX = "quiz-"
SUFFIX = ".db"


def list_backups(backup_dir: str) -> list[str]:
    if not os.path.isdir(backup_dir):
        return []
    names = [n for n in os.listdir(backup_dir) if n.startswith(PREFIX) and n.endswith(SUFFIX)]
    return [os.path.join(backup_dir, n) for n in sorted(names, reverse=True)]


def latest_time(backup_dir: str) -> datetime | None:
    files = list_backups(backup_dir)
    return datetime.fromtimestamp(os.path.getmtime(files[0])) if files else None


def make_backup(db_path: str, backup_dir: str, keep: int, now: datetime | None = None) -> str:
    """バックアップを 1 つ作り、古いものを消す。作ったファイルのパスを返す。"""
    os.makedirs(backup_dir, exist_ok=True)
    now = now or datetime.now()
    dest = os.path.join(backup_dir, f"{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}")
    tmp = dest + ".tmp"
    src = sqlite3.connect(db_path, timeout=30)
    try:
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
        finally:
            out.close()
    finally:
        src.close()
    # 書き終わってから名前を付け替える（途中で電源が切れても中途半端なファイルが残らない）
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    for old in list_backups(backup_dir)[max(keep, 1):]:
        os.remove(old)
    return dest


def due(backup_dir: str, interval_hours: int, now: datetime | None = None) -> bool:
    last = latest_time(backup_dir)
    now = now or datetime.now()
    return last is None or now - last >= timedelta(hours=interval_hours)


def main() -> None:
    from .config import Settings
    from .datadir import check

    parser = argparse.ArgumentParser(description="データベースのバックアップ")
    parser.add_argument("--list", action="store_true", help="バックアップの一覧を表示する")
    args = parser.parse_args()
    settings = Settings()
    check(settings.data_dir)
    if args.list:
        for path in list_backups(settings.backup_dir):
            print(path)
        return
    print(make_backup(settings.database_path, settings.backup_dir, settings.backup_keep))


if __name__ == "__main__":
    main()
