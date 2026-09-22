#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务层：受理 / 查询 / 列表 / 删除 / 能力清单。

这一层**从不碰上游**（那是 coordinator 的事）。这样做的理由只有一条：
受理请求必须在上游抖动时依然稳定返回 `task_id`，
否则调用方会重试 ⇒ 重复提交 ⇒ 撞上游的**在途互斥**限流。

对外响应的形状全部在 `docs/INTERFACE.md` 冻结；本文件是它的实现。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from . import models
from .config import Settings
from .errors import TaskNotDeletable, TaskNotFoundError
from .store import FAILURE, IN_PROGRESS, QUEUED, SUCCESS, TaskRow, TaskStore

#: Key 指纹的固定盐 —— 静态白名单场景下它只用来"不可逆化"，不是口令哈希。
_FINGERPRINT_SALT = b"imagefree-service/api-key-fingerprint/v1"


def fingerprint_key(key: str) -> str:
    """明文 Key ⇒ 不可逆指纹（**明文永不落库**）。"""
    return hmac.new(_FINGERPRINT_SALT, key.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def new_task_id() -> str:
    """`imagefree_<32hex>` —— 不可猜，因此"id 本身就是凭据"（见 §2.4）。"""
    return f"{models.TASK_PREFIX}{secrets.token_hex(16)}"


def _epoch(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.replace(tzinfo=UTC).timestamp())


# ---------------------------------------------------------------------------
# 对外状态词汇：**对齐 new-api 的 task 状态（全小写）**，见 docs/INTERFACE.md §2.5
# ---------------------------------------------------------------------------

#: 库内状态 → 对外 `status` 值。🔴 **单向映射，只用于出站渲染**。
#: 禁止用它写库 / 写缓存 / 做 SQL 条件 / 与库内状态比较 —— 两套词表混用会**静默**
#: 匹配不到任何行（`"completed"` 与库内的 `"success"` 不相等，且不会报错）。
#: 取值依据 new-api：内部 `SUCCESS`/`FAILURE` 对外渲染为 `completed`/`failed`
#: （`relaykit/dto/openai_video.go` 的 `VideoStatus*`）。
#: ⚠️ 不含 `unknown`：本服务的状态机是**封闭的**（四个态），不存在未知态 ——
#: 加一个永不出现的枚举值等于假能力（与 DELIBERATE_ABSENCES 同一条纪律）。
PUBLIC_STATUS: dict[str, str] = {
    QUEUED: "queued",
    IN_PROGRESS: "in_progress",
    SUCCESS: "completed",
    FAILURE: "failed",
}


def public_status(internal: str) -> str:
    """库内状态 ⇒ 对外 `status` 值（**唯一映射点**）。"""
    try:
        return PUBLIC_STATUS[internal]
    except KeyError:
        # 库内出现词表外的状态 = 代码与数据不一致。绝不允许静默透出。
        raise RuntimeError(
            f"未知的库内状态 {internal!r} —— PUBLIC_STATUS 缺少映射。"
            "（对外词表与库内词表是两套，改任一侧都要同步这里）"
        ) from None


class GenerationService:
    def __init__(self, store: TaskStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    # ------------------------------------------------------------------ 受理
    def accept(self, req: models.GenerationRequest, key_fingerprint: str | None) -> str:
        """归一化 + 落库。**零上游往返** —— 提交交给协调器。"""
        normalized = models.normalize(req)
        task_id = new_task_id()
        self._store.create_task(
            task_id=task_id,
            key_fingerprint=key_fingerprint,
            model=normalized.model,
            prompt=normalized.prompt,
            aspect_ratio=normalized.aspect_ratio,
            degradations=normalized.degradations,
            image_ref=normalized.image_ref,
        )
        return task_id

    # ------------------------------------------------------------------ 查询
    def get(self, task_id: str) -> tuple[int, dict[str, Any]]:
        """返回 `(http_status, body)`。

        刻意**不按 Key 过滤**：`task_id` 不可猜且只在受理时发给带 Key 的调用方，
        id 本身就是凭据（与"没带 Key 也能查"是同一套语义，见 §2.4）。
        """
        row = self._require(task_id)
        if row.status == SUCCESS:
            if not row.image_url:
                # 不可能发生（coordinator 只在成图时收口 success）。写成显式 raise 而不是
                # `assert`，是为了**不被 `-O` 优化掉**：宁可 500，也绝不把「成功但没图」
                # 这个自相矛盾的终态渲染给调用方。
                raise RuntimeError(
                    f"任务 {row.id} 是 success 却没有产物 URL —— 库内数据不一致，拒绝渲染。"
                )
            return 200, {
                # 终态一定带显式 status（不靠 HTTP 码推断；见 INTERFACE.md §2.2/§2.5）。
                "status": public_status(SUCCESS),
                "data": [{"url": row.image_url}],
                "created": _epoch(row.created_at),
                **self._degradations(row),
            }
        if row.status == FAILURE:
            return 200, {
                "task_id": row.id,
                "status": public_status(FAILURE),
                "error": {
                    "message": row.error_message or "任务失败（上游未给出原因）。",
                    "type": _error_type(row.error_code),
                    "code": row.error_code or "upstream_error",
                },
                **self._degradations(row),
            }
        # 非终态：202 = "还没好，继续轮询"。
        # 🔴 这里**不做** `else QUEUED` 之类的兜底 —— 库内出现词表外状态时，
        # `public_status()` 会**响亮失败**（与列表路径同一行为）。静默兜成 `queued`
        # 等于把「未知」伪装成「还没好」，调用方会**永远轮询**下去：
        # 终态绝不能被伪装成非终态 —— 这是终态最不能出的错。
        return 202, {
            "task_id": row.id,
            "status": public_status(row.status),
            **self._degradations(row),
        }

    def list_tasks(self, key_fingerprint: str | None, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        rows = self._store.list_tasks(key_fingerprint, limit=limit, offset=offset)
        data = []
        for row in rows:
            item: dict[str, Any] = {
                "task_id": row.id,
                "status": public_status(row.status),
                "model": row.model,
            }
            if row.image_url:
                item["url"] = row.image_url
            if row.error_code:
                item["error"] = {"code": row.error_code, "message": row.error_message}
            item["created"] = _epoch(row.created_at)
            data.append(item)
        return {"object": "list", "data": data, "limit": limit, "offset": offset}

    # ------------------------------------------------------------------ 删除
    def delete(self, task_id: str, key_fingerprint: str | None) -> dict[str, Any]:
        row = self._require(task_id, key_fingerprint=key_fingerprint)
        if row.status not in (SUCCESS, FAILURE):
            raise TaskNotDeletable(
                f"任务 {task_id} 还是 {row.status} —— 上游**没有取消端点**，"
                "删掉本地记录不会让上游停下来（它的额度还在被占着）。"
                "请等它到终态（成功/失败）之后再删。"
            )
        self._store.delete_task(task_id)
        # 删除回执：**同样小写**。它不是任务状态（new-api 词表里没有它），是操作回执。
        return {"task_id": task_id, "status": "deleted"}

    # ------------------------------------------------------------------ 运维
    def stats(self) -> dict[str, Any]:
        return {
            "tasks": self._store.count_by_status(),
            "active": self._store.count_active(),
            "oldest_active_age_seconds": self._store.oldest_active_age(),
            "capabilities": {
                "model": models.MODEL_T2I,
                "models": [models.MODEL_T2I, models.MODEL_I2I],
                "aspect_ratios": list(models.ASPECT_RATIOS),
                "max_images_per_request": 1,
                "accepts_reference_images": True,
                "max_reference_images": 1,
            },
        }

    # ------------------------------------------------------------------ 内部
    def _require(self, task_id: str, *, key_fingerprint: str | None = None) -> TaskRow:
        row = self._store.get_task(task_id)
        if row is None:
            raise TaskNotFoundError(f"任务 {task_id} 不存在，或不属于当前 API Key。")
        if key_fingerprint is not None and row.key_fingerprint != key_fingerprint:
            # 刻意与"不存在"同一个错误 —— 区分开等于确认 id 存在。
            raise TaskNotFoundError(f"任务 {task_id} 不存在，或不属于当前 API Key。")
        return row

    @staticmethod
    def _degradations(row: TaskRow) -> dict[str, Any]:
        """`degradations` 只在非空时出现。

        它**挂在查询响应上**而不是受理响应上：受理响应被冻结成只有 `task_id`
        （docs/INTERFACE.md §1），而调用方本来就要轮询 ⇒ 这里才是能看见它的地方。
        """
        if not row.degradations:
            return {}
        return {"degradations": list(row.degradations)}


def _error_type(code: str | None) -> str:
    """任务错误的 `type` 与错误类对齐（供调用方按 type 归类）。"""
    if code == "task_timeout":
        return "timeout_error"
    if code in ("content_policy_violation", "invalid_image"):
        return "invalid_request_error"
    if code:
        return "upstream_error"
    return "upstream_error"


__all__ = ["PUBLIC_STATUS", "GenerationService", "fingerprint_key", "new_task_id", "public_status"]
