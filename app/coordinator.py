#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协调器：**唯一会碰上游的组件**。

它做四件事，顺序固定：

  1. **自愈**：释放过期租约（worker 崩了以后任务能回到池子）；
  2. **收口超时**：超过 `TASK_TIMEOUT` 的任务判 failure；
  3. **查询**在途任务（零额度的只读动作，随时可做）；
  4. **提交**新任务（**消耗免费额度**的动作，受节奏闸门约束）。

## 闸门与容量

上游的限流不是"每秒多少次"，而是**在途互斥**，而且是**两层**：
同浏览器身份、同 IP 各只允许一个任务在跑。所以容量由两件事共同决定：

   有效容量 = min(IF_CONCURRENCY, 出口数)

  · 出口数 = 1（没配代理）⇒ 容量 1。**这时把 IF_CONCURRENCY 调大只会抬高失败率**，
    所以 `min()` 会把它压回去，并在启动日志里说明（不静默）。
  · 出口数 = N（配了 N 个代理，每个自带独立浏览器身份）⇒ 最多 N 个任务并行，
    且**一个出口同时只跑一个任务**。

## 撞墙就换出口（这是"加代理"真正生效的地方）

`FREE_TASK_IP_ACTIVE` / `FREE_GENERATION_ACTIVE` / `FREE_TASK_BROWSER_ACTIVE` 的含义是
**上游什么都没创建** ⇒ 换一个出口重试**不消耗任何额度**。所以一轮 tick 内会按顺序
试遍所有空闲出口，并把撞墙的那个**冷置一段时间**（`IMAGEFREE_PROXY_COOLDOWN`），
让后来的任务优先换别的出口。

🔴 **超时不换出口。** 超时的语义是"可能已经建成了"，换个出口重发等于**真的建出两条任务**。

## 重试纪律（哪些错误值得重试）

| 情形 | 动作 | 理由 |
|---|---|---|
| `FREE_*_TASK_ACTIVE`（在途互斥） | **换出口重试**，并冷置撞墙的出口 | 被拒 = 没建任务 ⇒ 零额度损失 |
| 提交超时 / 5xx / 非 JSON | **原出口退避重试**（不换） | 可能已建成；若真在跑，重试会被 `FREE_TASK_*` 挡回来（自证） |
| 查询超时 / 5xx | **退避重试** | 查询零成本 |
| 内容不合规 | **立即终态 failure** | 换 prompt 才有用，重试是白费 |
| 需要 Turnstile | **立即终态 failure**（部署问题） | 重试只会加深风控标记 |
"""
from __future__ import annotations

import asyncio
import socket
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from .config import Settings
from .egress import Egress, EgressPool
from .errors import (
    AdapterError,
    ContentPolicyError,
    EgressUnavailable,
    InvalidImageError,
    SubmitUnknown,
    TaskFailed,
    TaskTimeout,
    TurnstileRequired,
    UpstreamBrowserTaskActive,
    UpstreamIpTaskActive,
)
from .models import MODEL_I2I
from .reference import ReferenceImage, load_reference
from .store import FAILURE, IN_PROGRESS, QUEUED, SUCCESS, TaskRow, TaskStore, utcnow
from .upstream import ImageFreeClient

#: 上游忙 / 网络抖时的退避序列（秒）。取整不取巧：让人一眼能预期下一次尝试。
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)

#: 清库间隔（秒）。终态任务不需要实时清理。
PURGE_EVERY_SECONDS = 3600.0

#: 铸 token 失败时**最多试几次**（含首次）。铸失败常常是瞬时的，
#: 一次就把调用方的任务判死太苛刻；三次之后仍失败才终态（配置真的错了）。
MINT_RETRY_ATTEMPTS = 3

#: 在途互斥类的错误 `code`（撞上它们 ⇒ 值得换个出口再来一次）。
_WALL_CODES: frozenset[str] = frozenset(
    {"upstream_ip_task_active", "upstream_browser_task_active"}
)


class SubmitGate:
    """节奏闸门：**在途互斥 + 最小间隔 + 每分钟上限**。

    ⚠️ `if_min_interval` / `if_per_minute` 的账本是**进程内**状态，
    所以副本数 N 等于把这两项放宽 N 倍 —— 这是 `gunicorn_conf.py` 默认
    `workers=1` 的原因，不是保守参数。
    """

    def __init__(self, settings: Settings, *, capacity: int | None = None) -> None:
        self._min_interval = settings.if_min_interval
        self._per_minute = settings.if_per_minute
        #: 容量 = min(IF_CONCURRENCY, 出口数)。由协调器算好传进来。
        self._max_in_flight = max(1, capacity if capacity is not None else settings.if_concurrency)
        self._last_submit: datetime | None = None
        self._stamps: deque[datetime] = deque()

    @property
    def capacity(self) -> int:
        return self._max_in_flight

    def can_submit(self, *, in_flight: int, now: datetime | None = None) -> tuple[bool, str | None]:
        stamp = now or utcnow()
        if in_flight >= self._max_in_flight:
            return False, f"上游已有 {in_flight} 个在途任务（容量 {self._max_in_flight}）"
        if self._min_interval > 0 and self._last_submit is not None:
            elapsed = (stamp - self._last_submit).total_seconds()
            if elapsed < self._min_interval:
                return False, f"距上次提交仅 {elapsed:.1f}s（下限 {self._min_interval}s）"
        if self._per_minute > 0:
            cutoff = stamp - timedelta(seconds=60)
            while self._stamps and self._stamps[0] < cutoff:
                self._stamps.popleft()
            if len(self._stamps) >= self._per_minute:
                return False, f"最近 60s 已提交 {len(self._stamps)} 次（上限 {self._per_minute}）"
        return True, None

    def note_submit(self, now: datetime | None = None) -> None:
        stamp = now or utcnow()
        self._last_submit = stamp
        self._stamps.append(stamp)


class Coordinator:
    """后台跟进链。一个 tick = 一轮自愈 + 收口 + 查询 + 可能的提交。"""

    def __init__(
        self,
        store: TaskStore,
        egresses: EgressPool,
        settings: Settings,
        *,
        owner: str | None = None,
        boot_at: datetime | None = None,
    ) -> None:
        self._store = store
        self._pool = egresses
        self._settings = settings
        self._owner = owner or f"{settings.otel_service_name}@{socket.gethostname()}:{id(self)}"
        #: 本进程的启动时刻。用于区分「本进程正在提交」与「上个进程死在提交窗口里」——
        #: 判据是 `submit_started_at < boot_at`（见 `_reap_orphan_submissions`）。
        #: 可注入（测试用它模拟"换个进程重启"）。
        self._boot_at = boot_at or utcnow()
        self._stop = asyncio.Event()
        self._last_purge: datetime | None = None
        self._last_block_reason: str | None = None

        #: 有效容量 = min(IF_CONCURRENCY, 出口数 × 单 IP 在途上限)。
        #: 单 IP 上限实测为 3（docs/UPSTREAM.md §2.2.2）⇒ **加出口是真的能扩容量**。
        physical = len(egresses) * max(1, settings.imagefree_ip_concurrency)
        capacity = max(1, min(settings.if_concurrency, physical))
        self._gate = SubmitGate(settings, capacity=capacity)
        logger.info(
            "容量：有效 {}（IF_CONCURRENCY={}；物理上限 {} = {} 个出口 × 单 IP {}）。",
            capacity,
            settings.if_concurrency,
            physical,
            len(egresses),
            settings.imagefree_ip_concurrency,
        )
        self._pool_has_pinned_identity = bool(settings.imagefree_free_generation_id)

    # ------------------------------------------------------------------ 生命周期
    async def run_forever(self) -> None:
        logger.info(
            "协调器启动（owner={}，出口={}，容量={}）",
            self._owner,
            ",".join(self._pool.labels),
            self._gate.capacity,
        )
        # 🔴 **启动自检**：上个进程若死在「提交窗口」里，上游可能已经建了任务。
        # 必须在跑第一个 tick **之前**处理掉 —— 否则它们会被当成普通任务重新提交
        # （双建 + 白烧一次额度，且第一条的产物永远无人认领）。
        try:
            self._reap_orphan_submissions()
        except Exception as exc:  # noqa: BLE001 - 自检失败不许挡住协调器启动
            logger.exception("提交窗口自检失败（已吞，协调器继续）：{!r}", exc)
        while not self._stop.is_set():
            try:
                # tick 里有同步 HTTP ⇒ 丢进线程，别堵住事件循环（探活端点要活着）。
                summary = await asyncio.to_thread(self.tick)
                if summary["submitted"] or summary["polled"] or summary["finalized"]:
                    logger.debug("tick: {}", summary)
            except Exception as exc:  # noqa: BLE001 - 任何 tick 异常都不许终止循环
                logger.exception("tick 异常（已吞，循环继续）：{!r}", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._settings.coordinator_tick)
            except TimeoutError:
                continue
        logger.info("协调器已停止（owner={}）", self._owner)

    def stop(self) -> None:
        self._stop.set()

    @property
    def capacity(self) -> int:
        """有效容量 = min(IF_CONCURRENCY, 出口数)。`/stats` 会把两个值都报出来。"""
        return self._gate.capacity

    @property
    def is_stopping(self) -> bool:
        """关闭信号是否已发出。用于**拦在提交动作之前**（见 `_try_egresses`）。"""
        return self._stop.is_set()

    def _reap_orphan_submissions(self) -> int:
        """把**上个进程**遗留的「提交中」任务判为终态 —— 保守，**不自动重提**。

        🔴 缺口背景：`client.submit()` 成功 → 写回 `upstream_task_id` 之间崩溃 ⇒
        上游可能已经建了任务（额度已扣），而本地没有任何锚点 ⇒ 重启后重提 = **双建 +
        白烧一次额度**，且第一条的产物永远无人认领。轻量手段**无法消除**这个窗口
        （上游没有幂等键），只能**留痕 + 保守处理**。

        判据：`submit_started_at` 有值且**早于本进程启动时刻** ⇒ 那是上个进程写的。
        本进程正在提交的任务，标记必然 ≥ `boot_at`，**绝不会**被碰。

        ⚠️ **不能**靠租约过期处理：租约过期 = 放回池子 = 重提 —— 正是要避免的盲目重提。
        """
        orphans = self._store.list_orphan_submissions(before=self._boot_at)
        for row in orphans:
            self._finalize(
                row,
                SubmitUnknown(
                    "服务在「提交上游」这一步的窗口内重启：上游**可能已创建了任务**"
                    "（免费额度可能已扣），但本地没有任务 ID 可续跟。为不白烧额度，"
                    "服务**不会自动重提** —— 需要重试请重新提交一次（会消耗新的额度）。"
                ),
            )
        if orphans:
            logger.warning(
                "提交窗口自检：{} 条任务在上一个进程的提交窗口内失去联系 ⇒ 已按 "
                "`submit_unknown` 收口（**不自动重提**，避免双建）。",
                len(orphans),
            )
        return len(orphans)

    # ------------------------------------------------------------------ 一轮
    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        stamp = now or utcnow()
        summary: dict[str, Any] = {
            "released_leases": 0,
            "purged": 0,
            "claimed": 0,
            "submitted": 0,
            "polled": 0,
            "finalized": 0,
            "gate_blocked": 0,
            "retry_scheduled": 0,
            "wall_blocked": 0,
            "stopped": 0,
            "released_for_shutdown": 0,
        }

        summary["released_leases"] = self._store.release_expired_leases(now=stamp)
        summary["purged"] = self._maybe_purge(stamp)

        rows = self._store.claim_due_tasks(
            owner=self._owner,
            limit=self._gate.capacity,
            lease_seconds=self._settings.coordinator_lease,
            now=stamp,
        )
        summary["claimed"] = len(rows)

        for index, row in enumerate(rows):
            if self._stop.is_set():
                # 🔴 关闭途中：**不再处理**剩下的（含当前这条）。它们还没产生任何副作用，
                # 原样放回池子让下一个进程**立刻**接手（不写标记、不等退避）。
                for pending in rows[index:]:
                    self._release_untouched(pending, now=stamp)
                summary["released_for_shutdown"] = len(rows) - index
                break
            outcome = self._process(row, now=stamp)
            summary[outcome] = summary.get(outcome, 0) + 1
            if outcome == "gate_blocked":
                summary["gate_blocked_reason"] = self._last_block_reason
        return summary

    def _process(self, row: TaskRow, *, now: datetime) -> str:
        """处理一个被抢到的任务。

        返回 `submitted` / `polled` / `finalized` / `gate_blocked`（闸门挡住）
        / `retry_scheduled`（上游异常，已排退避） / `wall_blocked`（所有出口都撞墙）。
        """
        # --- 超时收口优先于一切：任务已经等了太久，别再往上加动作。
        # 图生图（编辑器）有**独立的、大得多**的预算 —— 实测 pending 10 分钟+ 是真实形态
        # （docs/UPSTREAM.md §10.4），生成链路的 780s 对它完全不适用。
        budget = (
            self._settings.task_timeout_i2i
            if row.model == MODEL_I2I
            else self._settings.task_timeout
        )
        age = (now - row.created_at).total_seconds()
        if age >= budget:
            self._finalize(
                row,
                TaskTimeout(
                    f"任务等待超过 {budget:.0f}s 仍未出图"
                    f"（{'图生图' if row.model == MODEL_I2I else '文生图'}预算）。"
                    "上游任务**可能仍在跑并占用额度** —— 这一点未取证，故不做断言。"
                ),
            )
            return "finalized"

        if row.upstream_task_id:
            return self._poll(row, now=now)
        return self._submit(row, now=now)

    # ------------------------------------------------------------------ 提交
    def _submit(self, row: TaskRow, *, now: datetime) -> str:
        allowed, reason = self._gate.can_submit(in_flight=self._store.count_in_flight(), now=now)
        if not allowed:
            self._last_block_reason = reason
            # 闸门关着：原样放回池子，稍后再看（**不消耗任何额度**）。
            self._release_later(row, now=now)
            return "gate_blocked"

        candidates = self._pool.available_egresses(
            in_flight_by_label=self._store.count_in_flight_by_egress(),
            per_egress_limit=self._settings.imagefree_ip_concurrency,
            now=now,
        )
        if not candidates:
            self._last_block_reason = (
                "所有出口的在途任务都满了（每个 IP 上限 "
                f"{self._settings.imagefree_ip_concurrency} 个）或正在冷却中"
            )
            self._release_later(row, now=now)
            return "gate_blocked"

        # 图生图：先代取参考图（调用方 URL/data URI → 字节）。
        # 放在闸门**之后**：闸门关着时连参考图都不该下载（白费流量）。
        # 下载失败/超限/内网地址 ⇒ 调用方数据问题，任务直接终态 failure。
        reference: ReferenceImage | None = None
        if row.model == MODEL_I2I:
            try:
                reference = load_reference(
                    row.image_ref or "",
                    max_bytes=self._settings.i2i_upload_limit_mb * 1024 * 1024,
                    timeout=self._settings.i2i_fetch_timeout,
                )
            except InvalidImageError as exc:
                self._finalize(row, exc)
                return "finalized"

        # 🔴 **提交窗口标记**：从这一刻起，本任务「可能已经在上游建了」。
        # 在此之前崩了都安全（重提零损失）；此后崩了只能靠它保守处理
        # （见 `_reap_orphan_submissions`）。标记必须写在**发 HTTP 之前** ——
        # 写在后面就等于虚报，那正是这个窗口的全部代价。
        self._store.update_task(row.id, submit_started_at=now)
        return self._try_egresses(row, candidates, now=now, reference=reference)

    def _try_egresses(
        self,
        row: TaskRow,
        candidates: list[Egress],
        *,
        now: datetime,
        reference: ReferenceImage | None = None,
    ) -> str:
        """按顺序试出口；每个出口撞墙后先**原地重试**几次，再换下一个。

        为什么值得原地重试：轮换型代理是**每 TCP 连接一个出口 IP**
        （见 skills/per-request-egress-rotation），撞墙后开新连接很可能就换了 IP。
        而被拒意味着**上游什么都没创建** ⇒ 重试不消耗额度。
        """
        wall: AdapterError | None = None
        tried = 0
        stop_all = False
        for egress in candidates:
            client: ImageFreeClient = self._pool.client(egress.label)
            # 撞墙后在**同一个出口**上当场再试几次：轮换型代理（每连接一个出口 IP）的
            # 下一连接很可能就是新 IP，比等十几秒退避划算得多。
            # 直连出口的 IP 不会变 ⇒ 不做原地重试（那只是白撞）。
            attempts = 1 + (0 if egress.is_direct else max(0, self._settings.imagefree_proxy_retries))
            unavailable: EgressUnavailable | None = None
            for attempt in range(attempts):
                if self._stop.is_set():
                    # 🔴 **拦在提交动作之前**：关闭信号已发出 ⇒ 绝不发起新的提交。
                    # 已经发出的那个请求会跑完（`to_thread` 里的线程取消不掉），
                    # 但提交窗口**不会再多一个** —— 这正是关闭途中最要紧的一条。
                    logger.info("收到停止信号 ⇒ 放弃提交、任务原样放回池子：{}", row.id)
                    self._release_untouched(row, now=now)
                    return "stopped"
                tried += 1
                try:
                    if reference is not None:
                        upstream_id = client.submit_i2i(row.prompt, reference)
                    else:
                        upstream_id = client.submit(row.prompt, row.aspect_ratio)
                except (UpstreamIpTaskActive, UpstreamBrowserTaskActive) as exc:
                    wall = exc
                    logger.warning(
                        "出口 {} 第 {}/{} 次撞到在途互斥（{}）—— 本次没有创建任何上游任务。",
                        egress.label,
                        attempt + 1,
                        attempts,
                        exc.code,
                    )
                    if isinstance(exc, UpstreamBrowserTaskActive) and self._pool_has_pinned_identity:
                        # 浏览器维度的墙**从未实测触发过**（docs/UPSTREAM.md §2.2.1）；
                        # 这里取保守行为：身份被固定时不逐个出口白撞。
                        logger.warning(
                            "IMAGEFREE_FREE_GENERATION_ID 是固定的 ⇒ 各出口的初始 cookie 相同。"
                            "浏览器维度的墙从未实测过，这里保守地停止在多个出口间白撞。"
                        )
                        stop_all = True
                        break
                    continue
                except (TurnstileRequired, ContentPolicyError) as exc:
                    # 两类"换出口也没用"的错误：
                    #   · 内容不合规 / 没配 token 来源 ⇒ prompt 或配置的事，重试不会变好 ⇒ 直接终态；
                    #   · 铸 token **失败**（`retryable`）⇒ 可能是瞬时的（CF 风险分抖动/浏览器慢）
                    #     ⇒ 退避重试几次，别一次就把调用方的任务判死。
                    if getattr(exc, "retryable", False) and (row.attempts or 1) <= MINT_RETRY_ATTEMPTS:
                        logger.warning(
                            "提交前置步骤失败（可重试，第 {}/{} 次）：{} — {}",
                            row.attempts or 1,
                            MINT_RETRY_ATTEMPTS,
                            row.id,
                            exc.message,
                        )
                        self._store.update_task(
                            row.id,
                            status=QUEUED,
                            error_code=exc.code,
                            error_message=exc.message,
                            next_poll_at=now + timedelta(seconds=self.retry_delay(row)),
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                        return "retry_scheduled"
                    self._finalize(row, exc)
                    return "finalized"
                except EgressUnavailable as exc:
                    # 🔴 出口**连不上**（代理拒绝认证 / DNS / 连接被拒）。
                    # 这类失败**不原地重试**：同一条坏路再撞只是白费 ——
                    # 直接跳出内层循环，由下面的统一分支去换出口（或退避）。
                    unavailable = exc
                    break
                except AdapterError as exc:
                    # 上游忙 / 网络抖 —— "还没建成功"但不一定是墙，**不换出口**，退避重试。
                    logger.warning("提交失败（将退避重试，不换出口）：{} — {}", row.id, exc.message)
                    self._store.update_task(
                        row.id,
                        status=QUEUED,
                        error_code=exc.code,
                        error_message=exc.message,
                        next_poll_at=now + timedelta(seconds=self.retry_delay(row)),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    return "retry_scheduled"

                # 成功：把出口**焊在任务上** —— 之后的轮询必须走同一个出口，
                # 因为上游下发的浏览器身份就住在那个出口的 cookie jar 里。
                self._gate.note_submit(now)
                self._store.update_task(
                    row.id,
                    status=IN_PROGRESS,
                    upstream_task_id=upstream_id,
                    egress=egress.label,
                    next_poll_at=now + timedelta(seconds=self._settings.poll_grace),
                    error_code=None,
                    error_message=None,
                    # 拿到明确响应（有 taskId）⇒ 关闭提交窗口。
                    submit_started_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
                logger.info(
                "已提交上游：{} → {}（出口 {}）",
                row.id,
                upstream_id,
                egress.label,
            )
                return "submitted"

            if unavailable is not None:
                # 出口连不上：先看还有没有别的路可换，没有再退避（别把任务判死）。
                if len(candidates) <= 1:
                    logger.warning(
                        "出口 {} 连不上，且这是唯一可用的出口（{}）⇒ 退避重试。",
                        egress.label,
                        unavailable.message,
                    )
                    self._store.update_task(
                        row.id,
                        status=QUEUED,
                        error_code=unavailable.code,
                        error_message=unavailable.message,
                        next_poll_at=now + timedelta(seconds=self.retry_delay(row)),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    return "retry_scheduled"
                wall = unavailable
                self._pool.note_unavailable(egress.label, now=now)
                logger.warning(
                    "出口 {} 连不上（{}）⇒ 冷置它并换下一个出口。",
                    egress.label,
                    unavailable.message,
                )
                continue

            # 这个出口试完了都没成 ⇒ 冷置它（多出口时才有意义），然后换下一个出口。
            self._pool.note_wall(egress.label, now=now)
            if stop_all:
                break

        # 所有候选出口（含每个出口的原地重试）都撞墙
        assert wall is not None  # 走到这里必然有墙
        logger.warning("共 {} 次提交尝试都没能成功（最后一次：{}）⇒ 退避后再试。", tried, wall.code)
        self._store.update_task(
            row.id,
            status=QUEUED,
            error_code=wall.code,
            error_message=wall.message,
            next_poll_at=now + timedelta(seconds=self.retry_delay(row)),
            # 撞墙（FREE_TASK_*）与出口连不上，都已取证为「上游什么都没创建」
            # ⇒ 重提是**零损失**的，关闭提交窗口。
            # ⚠️ 与之相对：超时 / 5xx 的分支**保留**标记（可能已建，不许盲目重提）。
            submit_started_at=None,
            lease_owner=None,
            lease_expires_at=None,
        )
        return "wall_blocked"

    # ------------------------------------------------------------------ 查询
    def _poll(self, row: TaskRow, *, now: datetime) -> str:
        # 走**任务自己那个出口**（浏览器身份在它的 cookie jar 里）。
        client = self._pool.client(row.egress or "direct")
        # 图生图任务查工具端点的 status（响应与生成链路同形，判据相同）。
        tool = ImageFreeClient.TOOL_EDITOR if row.model == MODEL_I2I else None
        try:
            status = client.fetch_status(row.upstream_task_id or "", tool=tool)
        except AdapterError as exc:
            logger.warning("查询失败（将退避重试）：{} — {}", row.id, exc.message)
            self._store.update_task(
                row.id,
                status=IN_PROGRESS,
                error_code=exc.code,
                error_message=exc.message,
                next_poll_at=now + timedelta(seconds=self.retry_delay(row)),
                lease_owner=None,
                lease_expires_at=None,
            )
            return "retry_scheduled"

        if status.is_success:
            wrote = self._store.mark_terminal(
                row.id, status=SUCCESS, image_url=status.image, now=now
            )
            if wrote:
                logger.info("任务完成：{} → {}（出口 {}）", row.id, status.image, row.egress)
            else:
                # 终态**不可覆盖**：对方已是终态（迟到结果 / 并发副本 / 人工改库）。
                logger.warning(
                    "收口被忽略（任务已是终态）：{} —— 迟到的成功结果不会翻面已 announced 的终态。",
                    row.id,
                )
            return "finalized"

        if status.is_failed:
            self._finalize(
                row,
                TaskFailed(
                    "上游判定生成失败（status=failed）。"
                    f"{('上游信息：' + status.message) if status.message else ''}".strip()
                ),
            )
            return "finalized"

        self._store.update_task(
            row.id,
            status=IN_PROGRESS,
            progress=status.progress,
            next_poll_at=now + timedelta(seconds=self._settings.poll_interval),
            lease_owner=None,
            lease_expires_at=None,
        )
        return "polled"

    # ------------------------------------------------------------------ 内部
    def _release_untouched(self, row: TaskRow, *, now: datetime) -> None:
        """把**还没碰过**的任务原样放回池子（关闭途中用）：

        不写提交标记、不改状态、**不等退避** —— 它应该立刻能被下一个进程接手。
        """
        self._store.update_task(
            row.id,
            next_poll_at=now,
            lease_owner=None,
            lease_expires_at=None,
        )

    def _release_later(self, row: TaskRow, *, now: datetime) -> None:
        """把任务放回池子，稍后再看。**不消耗额度**。"""
        self._store.update_task(
            row.id,
            next_poll_at=now + timedelta(seconds=self.retry_delay(row)),
            lease_owner=None,
            lease_expires_at=None,
        )

    def retry_delay(self, row: TaskRow) -> float:
        """按已尝试次数取退避值（越试越慢，且**不消耗额度**）。"""
        idx = max(0, min(len(RETRY_BACKOFF_SECONDS) - 1, (row.attempts or 1) - 1))
        return RETRY_BACKOFF_SECONDS[idx]

    def _finalize(self, row: TaskRow, exc: AdapterError) -> None:
        error = exc.to_task_error()
        wrote = self._store.mark_terminal(
            row.id,
            status=FAILURE,
            error_code=str(error["code"]),
            error_message=str(error["message"]),
        )
        if not wrote:
            # 终态**不可覆盖**：对方已是终态（迟到结果 / 并发副本 / 人工改库）。
            logger.warning(
                "收口被忽略（任务已是终态）：{} —— 迟到的失败结果不会覆盖已 announced 的终态。",
                row.id,
            )
            return
        logger.warning("任务终态 failure：{} — {} ({})", row.id, error["message"], error["code"])

    def _maybe_purge(self, now: datetime) -> int:
        if self._last_purge is not None and (now - self._last_purge).total_seconds() < PURGE_EVERY_SECONDS:
            return 0
        self._last_purge = now
        deleted = self._store.purge_terminal_older_than(self._settings.task_retention_days, now=now)
        if deleted:
            logger.info("清理过期终态任务 {} 条（保留 {} 天）", deleted, self._settings.task_retention_days)
        return deleted


__all__ = [
    "PURGE_EVERY_SECONDS",
    "RETRY_BACKOFF_SECONDS",
    "Coordinator",
    "SubmitGate",
]
