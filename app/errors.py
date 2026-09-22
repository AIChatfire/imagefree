#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外错误分类体系。

一个 `AdapterError` 子类 = 一个 `code` = 一个 HTTP 状态码 = **一句"下一步该做什么"**。

分类沿用 ../jimeng → ../hailuo 的结论（因为它是对的）：
  · **区分"上游没有"与"你写错了"** —— 前者进 `degradations`，后者才 4xx；
  · **部署问题不是调用方的问题** —— 故 Turnstile 未配 token 回 503，不回 401；
  · **`Retry-After` 只在真知道时给** —— 编一个数字等于伪造事实；
  · **失败也回 200**（任务完成了，只是结果是失败），否则会误触发调用方的重试。

imagefree 特有的两个：上游的限流是**在途互斥**（同浏览器 / 同 IP 只允许一个任务），
所以给它们单独的状态码 —— "等它结束"与"退避重试"是完全不同的动作。
"""
from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """所有对外错误的基类。HTTP 状态码**来自错误类本身**，不来自调用点。"""

    status_code: int = 502
    code: str = "upstream_error"
    error_type: str = "upstream_error"
    #: 该错误是否**值得重试**（默认否：多数错误重试只是白撞）。
    #: 协调器只对 `retryable=True` 的错误退避重试。
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        retry_after: float | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.retry_after = retry_after
        self.detail = detail

    def to_error(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code,
        }
        if self.param:
            err["param"] = self.param
        if self.retry_after is not None:
            err["retry_after"] = self.retry_after
        if self.detail is not None:
            err["detail"] = self.detail
        return {"error": err}

    def to_task_error(self) -> dict[str, Any]:
        """任务终态 failure 用的瘦身版（§2.3）—— 只保留 `message/type/code`。"""
        return {"message": self.message, "type": self.error_type, "code": self.code}


# ---------------------------------------------------------------------------
# 4xx —— 调用方的问题
# ---------------------------------------------------------------------------


class InvalidParameterError(AdapterError):
    """请求写错了。`param` 指出是哪个字段 —— 这是调用方唯一需要的线索。"""

    status_code = 400
    code = "invalid_parameter"
    error_type = "invalid_request_error"


class ContentPolicyError(AdapterError):
    """上游拦截（送审/版权）。**换 prompt**，重试无效。"""

    status_code = 400
    code = "content_policy_violation"
    error_type = "invalid_request_error"


class AuthError(AdapterError):
    """调用方的 Key 不对。"""

    status_code = 401
    code = "invalid_api_key"
    error_type = "invalid_request_error"


class TaskNotFoundError(AdapterError):
    """任务不存在，或不属于当前 Key（**刻意不区分** —— 区分开等于确认 id 存在）。"""

    status_code = 404
    code = "task_not_found"
    error_type = "invalid_request_error"


class TaskNotDeletable(AdapterError):
    """未终态的任务不能删 —— 上游**没有取消端点**，本地置删不会让上游停下来。"""

    status_code = 400
    code = "task_not_deletable"
    error_type = "invalid_request_error"


# ---------------------------------------------------------------------------
# 429 —— 能不能重试，分得很清
# ---------------------------------------------------------------------------


class UpstreamBrowserTaskActive(AdapterError):
    """上游：**同一浏览器身份**已有在途任务（`FREE_GENERATION_ACTIVE` /
    `FREE_TASK_BROWSER_ACTIVE`）。**重试无效** —— 要等那个任务结束。"""

    status_code = 429
    code = "upstream_browser_task_active"
    error_type = "rate_limit_error"


class UpstreamIpTaskActive(AdapterError):
    """上游：**同一出口 IP** 已有在途任务（`FREE_TASK_IP_ACTIVE`）。
    **重试无效** —— 要等那个任务结束，或换出口 IP。"""

    status_code = 429
    code = "upstream_ip_task_active"
    error_type = "rate_limit_error"


class UpstreamRateLimited(AdapterError):
    """上游通用限流。**可退避重试**。"""

    status_code = 429
    code = "upstream_rate_limited"
    error_type = "rate_limit_error"


# ---------------------------------------------------------------------------
# 5xx —— 我们这边的问题
# ---------------------------------------------------------------------------


class UpstreamError(AdapterError):
    """上游 5xx / 非 JSON / WAF 页。"""

    status_code = 502
    code = "upstream_error"
    error_type = "upstream_error"


class EgressUnavailable(AdapterError):
    """某个出口**连不上**：代理拒绝认证 / DNS 失败 / 连接被拒或被重置。

    语义是「这条通道坏了」，而不是「上游拒绝了请求」：
      · **多出口** ⇒ 换一条路就能解决（换出口 + 冷置坏的那个）；
      · **单出口** ⇒ 没得换，退避重试。

    与 `upstream_error` 分开，是为了不把「代理/部署问题」混进「上游 5xx」——
    两者的下一步动作完全不同。
    """

    status_code = 502
    code = "upstream_egress_unavailable"
    error_type = "upstream_error"


class UpstreamTimeout(AdapterError):
    status_code = 504
    code = "upstream_timeout"
    error_type = "upstream_error"


class CapabilityUnavailable(AdapterError):
    """能力当前不可用。"""

    status_code = 503
    code = "capability_unavailable"
    error_type = "service_unavailable"


class TurnstileRequired(AdapterError):
    """上游把 Turnstile 开关打开了，而本服务没有可用的 token —— **部署问题**。

    这是**永久**配置错误（没配来源）⇒ 任务直接终态，重试不会变好。
    运行期铸 token **失败**请用 :class:`TurnstileMintFailed`（可能是瞬时的）。
    """

    status_code = 503
    code = "upstream_turnstile_required"
    error_type = "service_unavailable"


class TurnstileMintFailed(TurnstileRequired):
    """铸 Turnstile token **失败**（脚本非零退出/超时/输出为空）。

    与父类的区别只有一个：**它是可能瞬时的**（CF 风险分抖动、浏览器慢、出口一时被风控），
    ⇒ 值得**退避重试几次**，而不是一次就把调用方的任务判死。
    对外语义不变（仍是 503 + `upstream_turnstile_required`）。
    """

    retryable = True


# ---------------------------------------------------------------------------
# 任务级失败（**不是** HTTP 错误，走 §2.3 的 200 failure 信封）
# ---------------------------------------------------------------------------


class TaskTimeout(AdapterError):
    """等待超过 `TASK_TIMEOUT`。上游任务**可能仍在跑并占用额度** —— 未取证。"""

    status_code = 200
    code = "task_timeout"
    error_type = "timeout_error"


class TaskFailed(AdapterError):
    """上游明确回了 `failed`。"""

    status_code = 200
    code = "task_failed"
    error_type = "upstream_error"


class SubmitUnknown(AdapterError):
    """**提交窗口内重启**：上游可能已建任务（额度可能已扣），但本地没有锚点可续跟。

    这是刻意的**保守**取舍：不自动重提（重提会双建并白烧一次额度），把「不确定」
    如实告诉调用方，由调用方决定是否重新提交。见 docs/INTERFACE.md §6。
    """

    status_code = 200
    code = "submit_unknown"
    error_type = "upstream_error"


class InvalidImageError(AdapterError):
    """图生图的参考图不合格（下载失败 / 超 10MB / 非 image/* / 指向内网地址）。

    **调用方数据问题** ⇒ 任务终态 failure（200 信封），重试无意义。
    """

    status_code = 200
    code = "invalid_image"
    error_type = "invalid_request_error"


__all__ = [
    "AdapterError",
    "AuthError",
    "CapabilityUnavailable",
    "ContentPolicyError",
    "EgressUnavailable",
    "InvalidImageError",
    "InvalidParameterError",
    "SubmitUnknown",
    "TaskFailed",
    "TaskNotDeletable",
    "TaskNotFoundError",
    "TaskTimeout",
    "TurnstileRequired",
    "UpstreamBrowserTaskActive",
    "UpstreamError",
    "UpstreamIpTaskActive",
    "UpstreamRateLimited",
    "UpstreamTimeout",
]
