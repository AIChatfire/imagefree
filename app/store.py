#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务库：**事实源**。

为什么必须有库：受理请求**不碰上游**（docs/INTERFACE.md §1）⇒ 任务必须先落地，
否则"受理成功"这件事没有载体。上游侧的 `taskId` 只是本地任务的一个属性。

并发模型（与 ../hailuo 一致）：协调器靠**数据库租约**选主 ——
多副本部署时同一时刻只有一个进程会去推导某个任务，崩了租约自然过期被别人接管。
默认副本数是 1（见 gunicorn_conf.py），租约承担的是「崩了能自愈」。
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# ---------------------------------------------------------------------------
# 任务状态（**内部**词汇；对外映射见 service.py）
# ---------------------------------------------------------------------------

QUEUED = "queued"
IN_PROGRESS = "in_progress"
SUCCESS = "success"
FAILURE = "failure"

TERMINAL_STATUSES: frozenset[str] = frozenset({SUCCESS, FAILURE})
ACTIVE_STATUSES: frozenset[str] = frozenset({QUEUED, IN_PROGRESS})


def utcnow() -> datetime:
    """朴素 UTC。库列全是 naive datetime，避免 SQLite 的 tz 陷阱。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 调用方 Key 的不可逆指纹（明文永不落库）。空 key = 关闭鉴权时的调用方。
    key_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    model: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    aspect_ratio: Mapped[str] = mapped_column(String(8))
    #: 图生图（model=image-i2i）的参考图引用（http(s) URL 或 data:image/… URI）。
    #: **只进不出**：查询响应不带它（data URI 可能几 MB，且不属于对外契约）。
    image_ref: Mapped[str | None] = mapped_column(Text, default=None)
    #: 受理时留下的降级痕迹（`degradations`），**随任务一起存**，查询时原样回。
    degradations: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    status: Mapped[str] = mapped_column(String(16), index=True, default=QUEUED)
    #: 上游 `taskId`。为空 = 还没提交上游（协调器负责补上）。
    upstream_task_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    #: 提交时选定的**出口标签**（`direct` / `proxy1` / 自定义）。
    #: 🔴 之后所有轮询都必须走同一个出口 —— 上游下发的浏览器身份 cookie
    #: 就住在那个出口的 cookie jar 里，换出口轮询等于换了身份。
    egress: Mapped[str | None] = mapped_column(String(32), index=True, default=None)

    image_url: Mapped[str | None] = mapped_column(Text, default=None)
    progress: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    #: 已尝试的轮询次数（诊断用，也用于"别把同一任务问爆"）。
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: 下一次该被协调器处理的时间。空 = 立即。
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, default=None)

    lease_owner: Mapped[str | None] = mapped_column(String(64), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    #: 🔴 **提交窗口**标记：本服务「已经（或正在）向上游发出提交、但还不知道结果」的开始时刻。
    #: · 提交前写入；拿到**明确**响应后清空。
    #: · ⚠️ **超时 / 5xx 时保留**（结果未知 ⇒ 上游可能已建）；
    #:   撞墙（`FREE_TASK_*`，已取证=零创建）时清空。
    #: · 重启后若有值且**早于本进程启动时刻** ⇒ 上个进程死在提交窗口里（见 coordinator）。
    submit_started_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key_fingerprint": self.key_fingerprint,
            "model": self.model,
            "aspect_ratio": self.aspect_ratio,
            "status": self.status,
            "upstream_task_id": self.upstream_task_id,
            "egress": self.egress,
            "image_url": self.image_url,
            "progress": self.progress,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class TaskStore:
    """薄封装：所有 SQL 都只出现在这里，service/coordinator 不碰 Session API。"""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self._engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        self._session: sessionmaker[Session] = sessionmaker(
            bind=self._engine, expire_on_commit=False, future=True
        )
        Base.metadata.create_all(self._engine)
        self._ensure_egress_column()
        self._ensure_image_ref_column()
        self._ensure_submit_started_column()

    def _ensure_egress_column(self) -> None:
        """给"加代理之前就已存在"的库补上 `egress` 列。

        `create_all` **只建表、不改表** ⇒ 老库上读写这一列会直接报错
        （而报错发生在轮询线程里，表现为"任务全都卡住"）。
        所以这里做一次幂等补列；两次 `engine.begin()` 是**两个事务**
        —— 探测失败会把事务标记为 aborted，同一事务里再 ALTER 会失败。
        """
        with self._engine.begin() as conn:
            try:
                conn.execute(text("SELECT egress FROM tasks LIMIT 1"))
                return
            except Exception:  # noqa: BLE001 - 列不存在就是要补，别的错也会在 ALTER 处再炸一次
                pass
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN egress VARCHAR(32)"))

    def _ensure_submit_started_column(self) -> None:
        """同上：给加「提交窗口」标记之前的旧库补 `submit_started_at` 列（幂等）。"""
        with self._engine.begin() as conn:
            try:
                conn.execute(text("SELECT submit_started_at FROM tasks LIMIT 1"))
                return
            except Exception:  # noqa: BLE001 - 列不存在就是要补
                pass
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN submit_started_at TIMESTAMP"))

    def _ensure_image_ref_column(self) -> None:
        """同上：给接入图生图之前的旧库补 `image_ref` 列（幂等）。"""
        with self._engine.begin() as conn:
            try:
                conn.execute(text("SELECT image_ref FROM tasks LIMIT 1"))
                return
            except Exception:  # noqa: BLE001 - 列不存在就是要补
                pass
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN image_ref TEXT"))

    # ------------------------------------------------------------------ 基础
    def session(self) -> Session:
        return self._session()

    def close(self) -> None:
        self._engine.dispose()

    # ------------------------------------------------------------------ 写入
    def create_task(
        self,
        *,
        task_id: str,
        key_fingerprint: str | None,
        model: str,
        prompt: str,
        aspect_ratio: str,
        degradations: list[str] | None,
        image_ref: str | None = None,
        now: datetime | None = None,
    ) -> TaskRow:
        """落库即"已受理"。`next_poll_at=now` ⇒ 下一轮 tick 就会被提交上游。"""
        stamp = now or utcnow()
        row = TaskRow(
            id=task_id,
            key_fingerprint=key_fingerprint,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            image_ref=image_ref,
            degradations=degradations or None,
            status=QUEUED,
            next_poll_at=stamp,
            created_at=stamp,
            updated_at=stamp,
        )
        with self._session.begin() as s:
            s.add(row)
        return row

    def update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self._session.begin() as s:
            s.execute(update(TaskRow).where(TaskRow.id == task_id).values(**fields))

    def mark_terminal(
        self,
        task_id: str,
        *,
        status: str,
        image_url: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """终态收口。**同时释放租约** —— 别让已终态的任务再被任何 worker 捡起来。

        🔴 **只在非终态时才写**（`WHERE status IN (queued, in_progress)`）：
        终态一旦写下就**不可覆盖** —— 迟到的收口（租约交接、慢响应、并发副本）
        不能让已经 announced 的结果翻面（调用方可能已经拿着 `completed` + url 走了）。

        返回**是否真的写入**：`False` = 对方已是终态，本次收口被忽略（调用方应记一笔）。
        """
        stamp = now or utcnow()
        with self._session.begin() as s:
            result = s.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.status.in_(ACTIVE_STATUSES))
                .values(
                    status=status,
                    image_url=image_url,
                    error_code=error_code,
                    error_message=error_message,
                    progress=100 if status == SUCCESS else None,
                    finished_at=stamp,
                    updated_at=stamp,
                    next_poll_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            return int(result.rowcount or 0) > 0

    def delete_task(self, task_id: str) -> None:
        with self._session.begin() as s:
            s.execute(delete(TaskRow).where(TaskRow.id == task_id))

    # ------------------------------------------------------------------ 读取
    def get_task(self, task_id: str) -> TaskRow | None:
        with self._session() as s:
            return s.get(TaskRow, task_id)

    def list_tasks(
        self, key_fingerprint: str | None, *, limit: int = 50, offset: int = 0
    ) -> list[TaskRow]:
        # 🔴 用 `==` 而不是 `.is_()`：`is_()` 只在 **SQLite** 上成立（它的 `IS` 是广义相等），
        #    而 PostgreSQL 的 `IS` 只接受 NULL/TRUE/FALSE/UNKNOWN ⇒ `IS '<指纹>'` 是**语法错误**。
        #    2026-09-22 真机部署实测：列表端点因此在 PG 上 500（`psycopg2.errors.SyntaxError`），
        #    而套件跑在 SQLite 上全绿 ⇒ 这类缺陷必须靠 `tests/test_dialect.py` 的编译期断言钉住。
        #    `==` 的语义恰好覆盖两种情形：值为 None 时渲染 `IS NULL`（匿名调用方），有值时渲染 `=`。
        stmt = (
            select(TaskRow)
            .where(TaskRow.key_fingerprint == key_fingerprint)
            .order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._session() as s:
            return list(s.scalars(stmt))

    def count_by_status(self) -> dict[str, int]:
        stmt = select(TaskRow.status, func.count()).group_by(TaskRow.status)
        with self._session() as s:
            return {str(status): int(count) for status, count in s.execute(stmt)}

    def count_in_flight_by_egress(self) -> dict[str, int]:
        """每个出口当前的**在途任务数**（已提交、未终态）。

        出口池靠它保证"一个出口同时只跑一个任务" —— 这是上游的规则
        （同 IP / 同浏览器身份只允许一个在途任务），不是保守参数。
        `egress` 为空的旧任务按 `direct` 计。
        """
        stmt = (
            select(TaskRow.egress, func.count())
            .where(TaskRow.status == IN_PROGRESS)
            .group_by(TaskRow.egress)
        )
        with self._session() as s:
            return {str(label or "direct"): int(count) for label, count in s.execute(stmt)}

    def count_active(self) -> int:
        """在途任务数 —— 节奏闸门用它判断"上游还被占着"。"""
        stmt = select(func.count()).select_from(TaskRow).where(TaskRow.status.in_(ACTIVE_STATUSES))
        with self._session() as s:
            return int(s.scalar(stmt) or 0)

    def count_in_flight(self) -> int:
        """**已提交上游且未终态**的任务数。

        这是闸门判断"还能不能再提交"的依据：上游是在途互斥的，
        所以"我们这边已经有一个上游任务在跑"就足以断定下一个会被拒。
        注意它与 `count_active()` 不同 —— 后者还包含尚未提交的 `queued`。
        """
        stmt = select(func.count()).select_from(TaskRow).where(TaskRow.status == IN_PROGRESS)
        with self._session() as s:
            return int(s.scalar(stmt) or 0)

    def oldest_active_age(self, now: datetime | None = None) -> float | None:
        """最老在途任务的年龄（秒）。没有在途任务时返回 None。"""
        stamp = now or utcnow()
        stmt = select(func.min(TaskRow.created_at)).where(TaskRow.status.in_(ACTIVE_STATUSES))
        with self._session() as s:
            oldest = s.scalar(stmt)
        if oldest is None:
            return None
        return max(0.0, (stamp - oldest).total_seconds())

    # ------------------------------------------------------------------ 租约
    def claim_due_tasks(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> list[TaskRow]:
        """抢占"到点了且没人持有"的任务。

        🔴 只抢 `next_poll_at <= now` 的 —— 这是**轮询退避**与**闸门**共同的落点：
        协调器不需要自己记账"谁该被问"，库里的时间戳就是账本。
        """
        stamp = now or utcnow()
        horizon = stamp + timedelta(seconds=lease_seconds)

        with self._session.begin() as s:
            rows = list(
                s.scalars(
                    select(TaskRow)
                    .where(
                        TaskRow.status.in_(ACTIVE_STATUSES),
                        (TaskRow.next_poll_at.is_(None)) | (TaskRow.next_poll_at <= stamp),
                        (TaskRow.lease_owner.is_(None)) | (TaskRow.lease_expires_at < stamp),
                    )
                    .order_by(TaskRow.next_poll_at.is_(None).desc(), TaskRow.created_at.asc())
                    .limit(max(0, limit))
                )
            )
            for row in rows:
                row.lease_owner = owner
                row.lease_expires_at = horizon
                row.attempts = (row.attempts or 0) + 1
            return rows

    def list_orphan_submissions(self, *, before: datetime) -> list[TaskRow]:
        """找出**在 `before` 之前**进入「提交中」的任务 —— 即**上个进程**的遗留。

        只挑在途任务（终态任务带着残留标记也没意义）。调用方（协调器）在**启动时**
        用 `before=本进程启动时刻` 调一次，把结果判成 `submit_unknown`。

        🔴 **不能**用租约过期来做这件事：租约过期 = 放回池子 = 重提 —— 正是要避免的
        盲目重提（那会双建并白烧一次上游额度）。
        """
        stmt = (
            select(TaskRow)
            .where(
                TaskRow.status.in_(ACTIVE_STATUSES),
                TaskRow.submit_started_at.is_not(None),
                TaskRow.submit_started_at < before,
            )
            .order_by(TaskRow.created_at.asc())
        )
        with self._session() as s:
            return list(s.scalars(stmt))

    def release_expired_leases(self, *, now: datetime | None = None) -> int:
        """把过期租约放回池子（worker 崩了以后的自愈）。"""
        stamp = now or utcnow()
        with self._session.begin() as s:
            result = s.execute(
                update(TaskRow)
                .where(
                    TaskRow.status.in_(ACTIVE_STATUSES),
                    TaskRow.lease_owner.is_not(None),
                    TaskRow.lease_expires_at < stamp,
                )
                .values(lease_owner=None, lease_expires_at=None, next_poll_at=stamp)
            )
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------ 维护
    def purge_terminal_older_than(self, days: int, *, now: datetime | None = None) -> int:
        """清理过期终态任务。日志里说清删了几条，因为"删掉了"本身是事实。"""
        if days <= 0:
            return 0
        stamp = now or utcnow()
        cutoff = stamp - timedelta(days=days)
        with self._session.begin() as s:
            result = s.execute(
                delete(TaskRow).where(
                    TaskRow.status.in_(TERMINAL_STATUSES),
                    TaskRow.finished_at.is_not(None),
                    TaskRow.finished_at < cutoff,
                )
            )
            return int(result.rowcount or 0)

    def all_active_ids(self) -> Iterable[str]:
        with self._session() as s:
            return list(s.scalars(select(TaskRow.id).where(TaskRow.status.in_(ACTIVE_STATUSES))))


__all__ = [
    "ACTIVE_STATUSES",
    "FAILURE",
    "IN_PROGRESS",
    "QUEUED",
    "SUCCESS",
    "TERMINAL_STATUSES",
    "Base",
    "TaskRow",
    "TaskStore",
    "utcnow",
]
