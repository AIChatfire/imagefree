#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交窗口（submit window）：**重启不丢额度**的最后一道保险。

缺口：`client.submit()` 成功 → 写回 `upstream_task_id` 之间崩溃 ⇒ 上游可能已经建了
任务（额度已扣），而本地没有任何锚点 ⇒ 重启后重提 = **双建 + 白烧一次额度**，
第一条的产物永远无人认领。轻量手段**无法消除**这个窗口（上游没有幂等键），
只能**留痕 + 保守处理**。

判据：`submit_started_at` 有值且**早于本进程启动时刻** ⇒ 上个进程的遗留。

所有用例零出网（假上游）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from app.coordinator import Coordinator
from app.store import TaskStore, utcnow


def _seed(store: TaskStore, task_id: str, *, now: datetime) -> None:
    store.create_task(
        task_id=task_id,
        key_fingerprint=None,
        model="image-t2i",
        prompt="a cat on a windowsill",
        aspect_ratio="1:1",
        degradations=None,
        now=now,
    )


def test_unknown_outcome_keeps_the_marker(store: TaskStore, make_pool: Any) -> None:
    """提交结果**未知**（超时）⇒ 标记**保留** —— 它是重启后判「可能已建」的唯一凭据。"""
    fix = make_pool(("proxy1",))
    fix.fakes["proxy1"].submit_raises = httpx.ReadTimeout("boom")
    _seed(store, "imagefree_unknown", now=utcnow())

    Coordinator(store, fix.pool, fix.settings).tick()

    row = store.get_task("imagefree_unknown")
    assert row is not None
    assert row.status == "queued", "未知结果 ⇒ 退回队列等退避（进程内仍会重试）"
    assert row.submit_started_at is not None, (
        "🔴 结果未知时必须保留标记 —— 少了它，重启后就会盲目重提（双建）"
    )


def test_wall_clears_the_marker(store: TaskStore, make_pool: Any) -> None:
    """撞墙（`FREE_TASK_*`，已取证 = 零创建）⇒ 标记**清空** ⇒ 重提是零损失的。"""
    fix = make_pool(("proxy1",))
    fix.fakes["proxy1"].fail_submit("FREE_TASK_IP_ACTIVE", "已有在途任务")
    _seed(store, "imagefree_wall", now=utcnow())

    Coordinator(store, fix.pool, fix.settings).tick()

    row = store.get_task("imagefree_wall")
    assert row is not None and row.status == "queued"
    assert row.submit_started_at is None, "撞墙=零创建 ⇒ 关闭提交窗口（重提安全）"


def test_orphan_submission_is_reaped_and_never_retried(store: TaskStore, make_pool: Any) -> None:
    """🔴 **缺口本体**：上个进程死在提交窗口 ⇒ 判 `submit_unknown`，且**不再碰上游**。"""
    fix = make_pool(("proxy1",))
    now = utcnow()
    _seed(store, "imagefree_orphan", now=now)
    # 模拟上个进程：进入提交窗口后崩了（标记留着、没有 upstream_task_id）
    store.update_task("imagefree_orphan", submit_started_at=now)

    coord = Coordinator(store, fix.pool, fix.settings, boot_at=now + timedelta(seconds=10))
    assert coord._reap_orphan_submissions() == 1

    row = store.get_task("imagefree_orphan")
    assert row is not None
    assert row.status == "failure" and row.error_code == "submit_unknown"
    assert "不会自动重提" in (row.error_message or ""), "错误信息必须说清为什么没有自动重试"
    assert fix.fakes["proxy1"].submit_calls == [], "🔴 绝不能重提（那会双建 + 白烧额度）"

    # 再推几轮 tick：也不许把它捡起来重提
    coord.tick(now=now + timedelta(minutes=5))
    assert fix.fakes["proxy1"].submit_calls == []

    # 对外呈现（§2.3 的失败信封）：200 + status=failed + error.code=submit_unknown
    from app.service import GenerationService

    http_status, body = GenerationService(store, fix.settings).get("imagefree_orphan")
    assert http_status == 200
    assert body["status"] == "failed"
    assert body["error"]["code"] == "submit_unknown"
    assert body["error"]["type"] == "upstream_error"


def test_inflight_submission_of_this_process_is_never_reaped(
    store: TaskStore, make_pool: Any
) -> None:
    """本进程**正在提交**（标记 ≥ `boot_at`）的任务绝不能被误判为孤儿 —— 那才是真在飞的。"""
    fix = make_pool(("proxy1",))
    boot = utcnow()
    _seed(store, "imagefree_live", now=boot)
    store.update_task("imagefree_live", submit_started_at=boot + timedelta(seconds=5))

    coord = Coordinator(store, fix.pool, fix.settings, boot_at=boot)
    assert coord._reap_orphan_submissions() == 0

    row = store.get_task("imagefree_live")
    assert row is not None and row.status == "queued", "原样留在池子里，等着被正常处理"


def test_run_forever_runs_the_reap_on_boot(store: TaskStore, make_pool: Any) -> None:
    """自检必须挂在**启动路径**上 —— 「方法存在但没人调」是典型缺陷，这条专门钉它。"""
    import asyncio

    fix = make_pool(("proxy1",))
    now = utcnow()
    _seed(store, "imagefree_boot", now=now)
    store.update_task("imagefree_boot", submit_started_at=now)

    coord = Coordinator(store, fix.pool, fix.settings, boot_at=now + timedelta(seconds=10))

    async def _boot_and_stop() -> None:
        task = asyncio.create_task(coord.run_forever())
        await asyncio.sleep(0.2)  # 够跑到自检（它在第一个 tick 之前）
        coord.stop()
        await task

    asyncio.run(_boot_and_stop())

    row = store.get_task("imagefree_boot")
    assert row is not None
    assert row.error_code == "submit_unknown", "协调器一启动就必须已经收口孤儿"
    assert fix.fakes["proxy1"].submit_calls == [], "收口走的是终态，不是重提"


# ---------------------------------------------------------------------------
# 优雅关闭：「窗口」不会再多一个
# ---------------------------------------------------------------------------


def test_stop_before_the_tick_loop_releases_everything(store: TaskStore, make_pool: Any) -> None:
    """关闭信号一到就**连任务都不碰**：原样放回（不写标记、不等退避），下个进程立刻接手。"""
    fix = make_pool(("proxy1",))
    now = utcnow()
    _seed(store, "imagefree_stopping", now=now)

    coord = Coordinator(store, fix.pool, fix.settings)
    coord.stop()  # 模拟 SIGTERM 已经来了
    summary = coord.tick(now=now)

    assert summary["released_for_shutdown"] == 1
    assert fix.fakes["proxy1"].submit_calls == [], "🔴 停止后绝不再碰上游"
    row = store.get_task("imagefree_stopping")
    assert row is not None
    assert row.status == "queued" and row.submit_started_at is None, "没碰过就不该有提交标记"
    assert row.lease_owner is None, "租约要放开 ⇒ 下一个进程立刻能接手"
    assert row.next_poll_at == now, "不等退避：关机的任务是「没处理过」，不是「失败了」"


def test_stop_mid_flight_prevents_further_attempts(store: TaskStore, make_pool: Any) -> None:
    """已经发出的请求会跑完，但**后续尝试**（换出口 / 原地重试）一律不再发起。"""
    fix = make_pool(("proxy1", "proxy2"))
    now = utcnow()
    _seed(store, "imagefree_midstop", now=now)
    coord = Coordinator(store, fix.pool, fix.settings)

    def _stop_then_wall(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        coord.stop()  # 提交发出的**那一瞬间**收到 SIGTERM
        return 200, {"error": "已有在途任务", "errorCode": "FREE_TASK_IP_ACTIVE"}

    fix.fakes["proxy1"].submit_override = _stop_then_wall
    coord.tick(now=now)

    assert len(fix.fakes["proxy1"].submit_calls) == 1, "第一个请求已经发出（让它跑完）"
    assert fix.fakes["proxy2"].submit_calls == [], "🔴 停止后不许再去试别的出口"
