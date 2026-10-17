"""データの場所の確認・SQLite の設定・バックアップ（停電対策と再起動後のデータ）。"""

import os
from datetime import datetime, timedelta

import pytest
from quizapp import backup
from quizapp.config import Settings
from quizapp.datadir import MARKER, DataDirMissing, check, ensure_or_exit
from quizapp.db import Job, init_db, make_engine


def test_missing_marker_refuses_to_start(tmp_path):
    empty = tmp_path / "not-mounted"
    empty.mkdir()
    with pytest.raises(DataDirMissing):
        check(str(empty))
    with pytest.raises(SystemExit) as exc:
        ensure_or_exit(str(empty), wait_seconds=0)
    assert exc.value.code == 1
    # 何も書かずに終わる（空のデータベースを作らない）
    assert os.listdir(empty) == []


def test_web_does_not_start_without_marker(tmp_path, monkeypatch):
    from quizapp.web import create_app

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SKIP_DATA_CHECK", raising=False)
    monkeypatch.setattr("quizapp.datadir.time.sleep", lambda _s: None)
    with pytest.raises(SystemExit):
        create_app(Settings())
    assert not (tmp_path / "quiz.db").exists()


def test_marker_present_is_ok(settings):
    check(settings.data_dir)
    assert os.path.isfile(os.path.join(settings.data_dir, MARKER))


def test_sqlite_is_set_up_for_power_loss(settings, sessions):
    engine = make_engine(settings.database_path)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL


def test_data_survives_restart(settings, sessions):
    """完了の確認：再起動（データベースを開き直す）してもデータが残る。"""
    with sessions.begin() as s:
        s.add(Job(subject="math", scope="", format="choice", count=3))
    again = init_db(settings.database_path)
    with again() as s:
        assert s.query(Job).count() == 1


def test_backup_rotation_and_restore(settings, sessions):
    with sessions.begin() as s:
        s.add(Job(subject="math", scope="", format="choice", count=7))
    base = datetime(2026, 10, 1, 3, 0, 0)
    for i in range(5):
        backup.make_backup(settings.database_path, settings.backup_dir, keep=3, now=base + timedelta(days=i))
    files = backup.list_backups(settings.backup_dir)
    assert [os.path.basename(f) for f in files] == [
        "quiz-20261005-030000.db", "quiz-20261004-030000.db", "quiz-20261003-030000.db"]
    assert not any(f.endswith(".tmp") for f in os.listdir(settings.backup_dir))
    # バックアップのファイルはそのまま開ける（復元できる）
    restored = init_db(files[0])
    with restored() as s:
        assert s.query(Job).one().count == 7


def test_backup_due(settings, sessions):
    assert backup.due(settings.backup_dir, 24)
    backup.make_backup(settings.database_path, settings.backup_dir, keep=14)
    assert not backup.due(settings.backup_dir, 24)
    assert backup.due(settings.backup_dir, 24, now=datetime.now() + timedelta(hours=25))


def test_worker_takes_daily_backup(settings, sessions, sandbox):
    from quizapp.worker import Worker

    from .conftest import OracleLLM

    w = Worker(settings, sessions, OracleLLM(sessions), sandbox)
    w.maybe_backup()
    assert len(backup.list_backups(settings.backup_dir)) == 1
