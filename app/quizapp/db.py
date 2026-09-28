"""SQLite のテーブル定義と接続。web と worker が同じファイルを共有する。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    """UTC の naive datetime（SQLite にはタイムゾーンなしで保存する）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Problem(Base):
    """問題。status: ready（出題可）/ rejected（検証で不合格）/ reported（誤り報告あり）。"""

    __tablename__ = "problems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(32), default="math")
    topic_id: Mapped[str] = mapped_column(String(64), index=True)
    subtopic: Mapped[str] = mapped_column(String(128), default="")
    difficulty: Mapped[int] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text, default="")
    answer_kind: Mapped[str] = mapped_column(String(32), default="value")
    answer_srepr: Mapped[str] = mapped_column(Text, default="")
    answer_latex: Mapped[str] = mapped_column(Text, default="")
    answer_var: Mapped[str] = mapped_column(String(16), default="x")
    explanation: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), index=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    report_note: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(Text, default="{}")
    models: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    served_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="problem", order_by="Attempt.created_at", cascade="all, delete-orphan"
    )

    @property
    def verification_log(self) -> dict[str, Any]:
        try:
            return json.loads(self.verification or "{}")
        except ValueError:
            return {}


class Attempt(Base):
    """解答の記録（当時の回答内容と正誤）。"""

    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), index=True)
    user_input: Mapped[str] = mapped_column(Text, default="")
    user_latex: Mapped[str] = mapped_column(Text, default="")
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    gave_up: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    problem: Mapped[Problem] = relationship(back_populates="attempts")


class Job(Base):
    """画面から依頼された問題生成。status: pending / running / done / failed / cancelled。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(64))
    difficulty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    progress: Mapped[str] = mapped_column(Text, default="")
    problem_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class KV(Base):
    """worker の状態や画面から切り替える設定を入れる小さな key-value 表。"""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def make_engine(path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def init_db(path: str) -> sessionmaker[Session]:
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def kv_get(session: Session, key: str, default: str = "") -> str:
    row = session.get(KV, key)
    return row.value if row else default


def kv_set(session: Session, key: str, value: str) -> None:
    row = session.get(KV, key)
    if row is None:
        session.add(KV(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def kv_updated_at(session: Session, key: str) -> datetime | None:
    row = session.execute(select(KV.updated_at).where(KV.key == key)).scalar_one_or_none()
    return row
