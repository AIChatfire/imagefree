#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧库升级路径的**离线**验证。

`create_all` 只建表不改表 ⇒ 给"接入 egress / 接入图生图之前"的旧库补列
必须靠 `TaskStore` 里的幂等迁移。今天线上库里有真实数据，
这条路径坏了的表现是"服务能起、任务全卡"—— 必须钉死。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from app.models import MODEL_I2I
from app.store import TaskStore

_OLD_CREATE = """
CREATE TABLE tasks (
    id VARCHAR(64) PRIMARY KEY,
    key_fingerprint VARCHAR(64),
    model VARCHAR(64),
    prompt TEXT,
    aspect_ratio VARCHAR(8),
    degradations JSON,
    status VARCHAR(16),
    upstream_task_id VARCHAR(64),
    image_url TEXT,
    progress INTEGER,
    error_code VARCHAR(64),
    error_message TEXT,
    attempts INTEGER,
    next_poll_at TIMESTAMP,
    lease_owner VARCHAR(64),
    lease_expires_at TIMESTAMP,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    finished_at TIMESTAMP
)
"""


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_OLD_CREATE)
        # 一条"接入代理/图生图之前"的历史任务
        conn.execute(
            "INSERT INTO tasks (id, model, prompt, aspect_ratio, status) VALUES (?,?,?,?,?)",
            ("imagefree_oldrow", "image-t2i", "cat", "1:1", "success"),
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    finally:
        conn.close()


def test_old_schema_gains_egress_and_image_ref_in_place(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _make_old_db(db)

    store = TaskStore(f"sqlite+pysqlite:///{db}")
    try:
        cols = _columns(db)
        assert {"egress", "image_ref", "submit_started_at"} <= cols, (
            "三个后加列都必须被幂等补上"
        )

        # 旧行可读，新列按空处理
        row = store.get_task("imagefree_oldrow")
        assert row is not None
        assert row.egress is None and row.image_ref is None and row.submit_started_at is None

        # 升级后的库必须能承载新的 i2i 任务（image_ref 落库、可读回）
        store.create_task(
            task_id="imagefree_newi2i",
            key_fingerprint=None,
            model=MODEL_I2I,
            prompt="把背景换成雪山",
            aspect_ratio="1:1",
            degradations=None,
            image_ref="https://cdn.example.com/ref.png",
        )
        new_row = store.get_task("imagefree_newi2i")
        assert new_row is not None
        assert new_row.image_ref == "https://cdn.example.com/ref.png"
        assert new_row.model == MODEL_I2I
    finally:
        store.close()


def test_old_schema_gains_submit_started_at_and_the_window_works(tmp_path: Path) -> None:
    """旧库补上 `submit_started_at` 之后，「提交窗口」整条链路可用（打标记 / 查孤儿）。"""
    from app.store import utcnow

    db = tmp_path / "window.db"
    _make_old_db(db)
    store = TaskStore(f"sqlite+pysqlite:///{db}")
    try:
        assert "submit_started_at" in _columns(db)

        now = utcnow()
        store.create_task(
            task_id="imagefree_newwin",
            key_fingerprint=None,
            model="image-t2i",
            prompt="cat",
            aspect_ratio="1:1",
            degradations=None,
            now=now,
        )
        store.update_task("imagefree_newwin", submit_started_at=now)
        orphans = store.list_orphan_submissions(before=now + timedelta(seconds=10))
        assert [o.id for o in orphans] == ["imagefree_newwin"], "升级后的库必须能查「孤儿提交」"
        # 判据是**严格早于**：同一时刻不算孤儿（本进程刚写的标记不能被误杀）
        assert store.list_orphan_submissions(before=now) == []
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """同一个库开两次 TaskStore ⇒ 第二次补列必须是空操作（不报错、不加重复列）。"""
    db = tmp_path / "again.db"
    _make_old_db(db)
    first = TaskStore(f"sqlite+pysqlite:///{db}")
    first.close()
    second = TaskStore(f"sqlite+pysqlite:///{db}")
    try:
        assert "image_ref" in _columns(db)
    finally:
        second.close()
