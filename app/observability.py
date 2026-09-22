#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可观测性：日志（loguru）+ 可选的上游 span（logfire）。

两条刻意的取舍（沿用 ../hailuo 的结论）：

  · **留空 token 就是不上报** —— 只打一行说明原因的 WARNING，绝不静默；
  · **观测面刻意不脱敏**（`OTEL_CAPTURE_UPSTREAM=1` 时绑上游原始报文）：
    事后脱敏会改掉上游的实际字段名，让人对着面板排查一个不存在的字段。

`logfire` 是**可选项**：装不上/配置失败 ⇒ 降级为"只有日志"，服务照常跑。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from loguru import logger

from .config import Settings

try:  # pragma: no cover - 环境相关
    import logfire as _logfire
except Exception:  # noqa: BLE001 - 任何导入失败都要降级而不是炸服务
    _logfire = None  # type: ignore[assignment]


class _State:
    """进程级观测状态。**只由 `configure()` 写**。"""

    enabled: bool = False
    capture_upstream: bool = False
    scrub: bool = False
    reason: str = "尚未初始化"


_STATE = _State()


def configure(settings: Settings) -> dict[str, Any]:
    """按配置初始化观测面。返回一段可放进 `/capabilities` 的状态摘要。"""
    _STATE.capture_upstream = bool(settings.otel_capture_upstream)
    _STATE.scrub = bool(settings.otel_scrubbing)

    if not settings.logfire_token:
        _STATE.enabled = False
        _STATE.reason = "LOGFIRE_TOKEN 为空 ⇒ 只在本地留日志，不上报。"
        logger.warning(_STATE.reason)
        return _summary()

    if _logfire is None:
        _STATE.enabled = False
        _STATE.reason = "logfire 未安装 ⇒ 降级为只有日志（服务不受影响）。"
        logger.warning(_STATE.reason)
        return _summary()

    try:
        _logfire.configure(
            token=settings.logfire_token,
            service_name=settings.otel_service_name,
            environment=settings.logfire_environment or None,
        )
    except Exception as exc:  # noqa: BLE001 - 配置失败不许炸服务
        _STATE.enabled = False
        _STATE.reason = f"logfire 配置失败 ⇒ 降级为只有日志：{exc!r}"
        logger.warning(_STATE.reason)
        return _summary()

    _STATE.enabled = True
    _STATE.reason = f"已启用（service_name={settings.otel_service_name}）。"
    logger.info("观测面已启用：{}", _STATE.reason)
    return _summary()


def _summary() -> dict[str, Any]:
    return {
        "enabled": _STATE.enabled,
        "capture_upstream": _STATE.capture_upstream,
        "scrubbing": _STATE.scrub,
        "note": _STATE.reason,
    }


def _bindable(payload: dict[str, Any]) -> dict[str, str]:
    """把上游报文压成 span 属性。

    `OTEL_SCRUBBING=0`（默认）⇒ 原样保留。真开脱敏时**只**打掉 cookie/凭据键，
    不碰任何业务字段名（否则会让人排查一个不存在的字段）。
    """
    if not _STATE.scrub:
        return {f"upstream.{k}": json.dumps(v, ensure_ascii=False)[:2000] for k, v in payload.items()}
    sensitive = ("cookie", "token", "authorization", "credential", "secret")
    out: dict[str, str] = {}
    for k, v in payload.items():
        if any(s in k.lower() for s in sensitive):
            out[f"upstream.{k}"] = "[Scrubbed]"
        else:
            out[f"upstream.{k}"] = json.dumps(v, ensure_ascii=False)[:2000]
    return out


@contextmanager
def upstream_span(name: str, payload: dict[str, Any]) -> Iterator[None]:
    """一次上游往返的 span。未启用观测时是**零开销空操作**。

    ⚠️ 只把「创建 span」放进 try —— **业务异常必须原样穿透**。
    """
    if not _STATE.enabled or _logfire is None:
        yield
        return

    attrs = _bindable(payload) if _STATE.capture_upstream else {}
    try:
        cm: Any = _logfire.span(name, **attrs)
    except Exception as exc:  # noqa: BLE001 - 观测层永远不许影响业务
        logger.debug("创建 span 失败，降级为空操作：{!r}", exc)
        cm = nullcontext()
    with cm:
        yield


def log_upstream(name: str, payload: dict[str, Any]) -> None:
    """把上游报文落到本地日志（抓不到观测面时的兜底证据）。"""
    logger.debug("{} :: {}", name, json.dumps(payload, ensure_ascii=False)[:2000])


__all__ = ["configure", "log_upstream", "upstream_span"]
