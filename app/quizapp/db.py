"""SQLite のテーブル定義と接続。web と worker が同じファイルを共有する。

停電対策:
  - journal_mode=WAL ＋ synchronous=FULL：書き込みの途中で電源が切れても、
    最後に完了した書き込みまでの状態に戻るだけで、ファイルは壊れない
  - 定期バックアップは backup.py（worker が 1 日 1 回取る）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DEFAULT_USER_ID = 1


def utcnow() -> datetime:
    """UTC の naive datetime（SQLite にはタイムゾーンなしで保存する）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    """利用者。切り替え画面は後のフェーズで作る。今は既定の利用者（id=1）だけ。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Passage(Base):
    """本文（英語の長文、公共の資料など）。1 つの本文に複数の問題がつく。数学では使わない。"""

    __tablename__ = "passages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    source_title: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    # 本文を後から使い回すための記録（英語の読解：同じ本文に設問を足す）。前の版の本文は空
    template: Mapped[str] = mapped_column(String(64), default="")
    topic: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="")  # "ok"＝見直しを通った本文
    gen_model: Mapped[str] = mapped_column(String(128), default="")  # 本文作成役のモデル
    think: Mapped[str] = mapped_column(String(16), default="")  # 本文作成役の思考モード（runtime.THINK_RECORD のキー）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def log(self) -> list[dict[str, Any]]:
        return _loads(self.log_json, [])

    log_json: Mapped[str] = mapped_column("log", Text, default="[]")  # 本文の作成・点検・見直しの記録


class Job(Base):
    """作成依頼。

    status: pending（順番待ち）/ running（処理中）/ waiting（Ollama を待っている）/
            done（全問できた）/ partial（一部だけできた）/ failed /
            cancelling（取り消し中：worker が止まるのを待っている）/ cancelled（取り消し済み）
    scope: "" は科目全体。"unit:<単元ID>" と "tmpl:<小単元ID>" をカンマでつないだものは、その和集合。
    gen_model / verify_*：依頼したときに選んだモデル。後で /status の設定を変えても、この依頼はこのモデルで処理する
    *_eval_s / *_load_s：Ollama の応答の eval_duration（出力にかかった時間）と load_duration（モデルの読み込み）の合計。
                        モデルを比べるときに読み込みが混ざらないよう分けて持つ
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(1024), default="")
    levels: Mapped[str] = mapped_column(String(64), default="")  # LEVELS のキーをカンマで。"" はすべて
    format: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    progress: Mapped[str] = mapped_column(Text, default="")
    gen_attempts: Mapped[int] = mapped_column(Integer, default=0)
    made: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    # 検証のテスト：わざと正解をずらした問題を流す。合格してもストックには入れない
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    gen_model: Mapped[str] = mapped_column(String(128), default="")
    verify_model: Mapped[str] = mapped_column(String(128), default="")
    verify_count: Mapped[int] = mapped_column(Integer, default=0)
    verify_model_2: Mapped[str] = mapped_column(String(128), default="")
    explanation_review: Mapped[bool] = mapped_column(Boolean, default=True)  # 解説の見直しをするか
    review_model: Mapped[str] = mapped_column(String(128), default="")  # 見直し役（空なら検証役のモデル）
    # 依頼の種類："generate"（作成）か "review"（見直しのやり直し：target_ids の問題を review_model で見直し、
    # 結果を記録に足すだけ。合否は変えない。見直し役のモデルを比べる・前に作った問題を点検し直すため）
    kind: Mapped[str] = mapped_column(String(16), default="generate")
    target_ids_json: Mapped[str] = mapped_column("target_ids", Text, default="[]")
    # 科目だけの役割のモデル（英語の本文作成 {"passage": "anthropic/…"} など）
    extra_models_json: Mapped[str] = mapped_column("extra_models", Text, default="{}")
    # 処理したときの思考モード {モデル: "on" / "off" / "default" / "api"}（runtime.THINK_RECORD。前の版の依頼は空）
    think_json: Mapped[str] = mapped_column("think", Text, default="{}")
    gen_calls: Mapped[int] = mapped_column(Integer, default=0)
    gen_eval_s: Mapped[float] = mapped_column(Float, default=0.0)
    gen_load_s: Mapped[float] = mapped_column(Float, default=0.0)
    verify_calls: Mapped[int] = mapped_column(Integer, default=0)
    verify_eval_s: Mapped[float] = mapped_column(Float, default=0.0)
    verify_load_s: Mapped[float] = mapped_column(Float, default=0.0)
    verified: Mapped[int] = mapped_column(Integer, default=0)  # 検証にかけた下書きの数
    api_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)  # この依頼で使った API の料金（ドル）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def target_ids(self) -> list[int]:
        return [int(i) for i in (_loads(self.target_ids_json, []) or [])]

    @property
    def think(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (_loads(self.think_json, {}) or {}).items()}

    @property
    def extra_models(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (_loads(self.extra_models_json, {}) or {}).items()}


class Problem(Base):
    """問題。

    status: draft（文章ができ、検証待ち）/ ready（検証に合格。ストック）/ rejected（不合格）/
            deleted（問題検証モードで削除。出題しない。回答の記録は残す）/
            test（検証のテスト用。出題しない）
    format: choice（スマホ向け選択式）/ input（キーボード入力）
    unit: 単元ID、template: 小単元ID（数学ではテンプレートの ID）
    choices: [{"text": 表示（$…$ の LaTeX を含む）, "correct": bool, "mistake": 間違いの種類 or null}]
    answer: 採点用の正解（科目ごとの形。数学は {"kind", "srepr", "latex", "var"}）
    """

    __tablename__ = "problems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(32), index=True)
    unit: Mapped[str] = mapped_column(String(64), index=True)
    template: Mapped[str] = mapped_column(String(64), index=True)
    # レベル（LEVELS のキー）と、主な単元とは別に関係する単元（カンマ区切り）。作成時に小単元から写す
    level: Mapped[str] = mapped_column(String(16), default="", index=True)
    related_units: Mapped[str] = mapped_column(String(255), default="")
    format: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    passage_id: Mapped[int | None] = mapped_column(ForeignKey("passages.id"), nullable=True)

    question: Mapped[str] = mapped_column(Text, default="")
    choices_json: Mapped[str] = mapped_column("choices", Text, default="[]")
    answer_json: Mapped[str] = mapped_column("answer", Text, default="{}")
    answer_display: Mapped[str] = mapped_column(Text, default="")
    input_hint: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    hint: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="")

    # 作り方の記録（テンプレートの数値など）。同じ数値の問題を作らないための鍵も持つ
    params_json: Mapped[str] = mapped_column("params", Text, default="{}")
    params_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    verification_json: Mapped[str] = mapped_column("verification", Text, default="[]")
    reject_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    gen_model: Mapped[str] = mapped_column(String(128), default="")
    verify_models: Mapped[str] = mapped_column(String(255), default="")

    reported: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    report_note: Mapped[str] = mapped_column(Text, default="")
    reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    passage: Mapped[Passage | None] = relationship()
    job: Mapped[Job | None] = relationship()

    @property
    def choices(self) -> list[dict[str, Any]]:
        return _loads(self.choices_json, [])

    @property
    def answer(self) -> dict[str, Any]:
        return _loads(self.answer_json, {})

    @property
    def params(self) -> dict[str, Any]:
        return _loads(self.params_json, {})

    @property
    def verification(self) -> list[dict[str, Any]]:
        return _loads(self.verification_json, [])


class StudySession(Base):
    """解き始めてから終わるまでの 1 セット。kind: normal / retry（1 問だけの再挑戦）。"""

    __tablename__ = "study_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    subject: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(1024), default="")
    levels: Mapped[str] = mapped_column(String(64), default="")
    device: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16), default="normal")
    problem_ids_json: Mapped[str] = mapped_column("problem_ids", Text, default="[]")
    requested: Mapped[int] = mapped_column(Integer, default=0)
    # いま表示している問題と、最初に表示した時刻（読み込み直しても、かかった時間を最初から数える）
    current_problem_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_shown_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def problem_ids(self) -> list[int]:
        return [int(i) for i in _loads(self.problem_ids_json, [])]


class Attempt(Base):
    """回答の記録。

    attempt_no: この利用者がこの問題に答えた何回目か（1 = 初回）
    duration_ms: 問題を表示してから答えるまでの時間（サーバーの時計で測る）
    hint_viewed: 答える前にヒントを開いたか
    explanation_viewed: 答える前に解説を見たか（「わからない」で解説を開いた）。
                        答えた後は必ず解説を表示するので、ここには数えない
    response: 当時の回答（選択式は選んだ選択肢の表示、キーボード入力は入力した文字列）
    chosen_index / chosen_mistake: 選択式で選んだ選択肢の番号と、その間違いの種類
    subject / unit / template / format は集計しやすいよう問題から写しておく
    """

    __tablename__ = "attempts"
    # 同じセットの同じ問題には 1 回だけ答える（二重送信で記録が 2 件にならないように）
    __table_args__ = (UniqueConstraint("session_id", "problem_id", name="uq_attempt_session_problem"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("study_sessions.id"), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(64))
    template: Mapped[str] = mapped_column(String(64))
    format: Mapped[str] = mapped_column(String(16))
    device: Mapped[str] = mapped_column(String(16), default="")
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    gave_up: Mapped[bool] = mapped_column(Boolean, default=False)
    hint_viewed: Mapped[bool] = mapped_column(Boolean, default=False)
    explanation_viewed: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[str] = mapped_column(Text, default="")
    response_display: Mapped[str] = mapped_column(Text, default="")
    chosen_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chosen_mistake: Mapped[str] = mapped_column(String(32), default="")
    shown_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    problem: Mapped[Problem] = relationship()


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
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def _add_missing_columns(engine: Engine) -> None:
    """あとから足した列を、既存のデータベースに足す（SQLite の ALTER TABLE ADD COLUMN）。

    列を足すだけの変更はこれで済む。列の削除・型の変更は行わない。
    """
    from sqlalchemy import inspect

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if default is not None:
                    ddl += f" DEFAULT {default!r}" if not isinstance(default, bool) else f" DEFAULT {int(default)}"
                conn.exec_driver_sql(ddl)


def init_db(path: str) -> sessionmaker[Session]:
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as s:
        if s.get(User, DEFAULT_USER_ID) is None:
            s.add(User(id=DEFAULT_USER_ID, name="わたし"))
    return sessions


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


def kv_json(session: Session, key: str, default: Any = None) -> Any:
    return _loads(kv_get(session, key), default)


def kv_updated_at(session: Session, key: str) -> datetime | None:
    return session.execute(select(KV.updated_at).where(KV.key == key)).scalar_one_or_none()
