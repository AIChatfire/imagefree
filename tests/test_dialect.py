#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方言可移植性：把"只在本地 SQLite 上跑得通"的 SQL 钉死在**编译期**。

背景（2026-09-22 真机部署实测）：`GET /async/v1/images/generations`（任务列表）在
**PostgreSQL** 上直接 500，异常原文：

    sqlalchemy.exc.ProgrammingError: (psycopg2.errors.SyntaxError)
    syntax error at or near "'2b894492f13dbd340787e47f7c9b8e50'"
    LINE 3: WHERE tasks.key_fingerprint IS '2b894492f13dbd340787e47f7c9b...

根因：`Column.is_(<字符串>)`。SQLite 的 `IS` 是**广义相等**（合法），
PostgreSQL 的 `IS` 只接受 `NULL / TRUE / FALSE / UNKNOWN` ⇒ 值比较必须写成 `=`。

🔴 为什么套件当初没抓到：全部用例跑在 SQLite 上
（`tests/test_api.py::test_task_list_is_scoped_to_the_key` 一直是绿的），
而该缺陷**只在鉴权开启**（即生产形态：`API_KEYS` 非空 ⇒ 指纹是字符串）时才触发 ——
鉴权关闭时指纹是 `None`，渲染成 `IS NULL`，在两边都合法。

所以这里**不连库**：把 `list_tasks` 真实发出的语句对象按两个方言各编译一次。
这样"上线才炸"的方言缺陷在本地就能被钉住，也不需要在 CI 里起一个 PG。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.store import TaskStore

#: 目标方言：SQLite（本地/测试）与 PostgreSQL（生产）。加新方言 = 往这里加一条。
DIALECTS: dict[str, Any] = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.dialect(),
}


@pytest.fixture
def capture_statement(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], Any]:
    """捕获 `list_tasks` **真实**发出的语句对象。

    刻意**不**在测试里重建那条 select —— 重建等于把被测代码抄一遍，
    `store.py` 哪天改回 `.is_()` 测试也照样绿（这正是要避免的"假绿"）。
    """
    box: dict[str, Any] = {}
    original = Session.scalars

    def spy(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        box["stmt"] = statement
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalars", spy)

    def _capture(fingerprint: str | None) -> Any:
        store.list_tasks(fingerprint, limit=5, offset=0)
        assert "stmt" in box, "list_tasks 没有走 Session.scalars —— 夹具的前提变了，先看它的实现。"
        return box["stmt"]

    return _capture


@pytest.mark.parametrize("name", sorted(DIALECTS))
def test_list_by_fingerprint_uses_equality_on_every_dialect(
    name: str, capture_statement: Callable[[str | None], Any]
) -> None:
    """有指纹（鉴权开启）⇒ 必须是值比较 `=`，**不能**是 `IS '<字符串>'`。"""
    sql = str(
        capture_statement("fp-abc").compile(
            dialect=DIALECTS[name], compile_kwargs={"literal_binds": True}
        )
    )
    assert "key_fingerprint = 'fp-abc'" in sql, f"{name}：期望值比较，实际 SQL：{sql}"
    assert "IS 'fp-abc'" not in sql, f"{name}：值比较被渲染成了 IS（PG 上会语法错误）：{sql}"


@pytest.mark.parametrize("name", sorted(DIALECTS))
def test_anonymous_list_is_still_null_safe(
    name: str, capture_statement: Callable[[str | None], Any]
) -> None:
    """鉴权关闭时指纹是 None ⇒ 必须仍是 `IS NULL`（这种 IS 在两个方言上都合法）。"""
    sql = str(
        capture_statement(None).compile(
            dialect=DIALECTS[name], compile_kwargs={"literal_binds": True}
        )
    )
    assert "key_fingerprint IS NULL" in sql, f"{name}：匿名列表必须按 IS NULL 过滤，实际 SQL：{sql}"
