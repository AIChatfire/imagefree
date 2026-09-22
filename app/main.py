#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 层：对外契约的**唯一出口**（docs/INTERFACE.md）。

三件必须做对的事：

  1. **受理只回 `task_id`** —— 多一个键就是契约变更；提交由协调器在后台做。
  2. **失败也回 200**（任务跑完了，只是结果是失败）—— 回 4xx 会误触发调用方的重试。
  3. **鉴权语义分三种**（见 §2.4）：查询单任务不强制鉴权（id 即凭据），
     列表/删除**必须**鉴权（否则可枚举/删除别人的任务），
     带了错 Key 一律 401（不许静默吞掉调用方的配置错误）。

⚠️ `gunicorn` 目标必须是**工厂**：`app.main:create_app()`（见 gunicorn_conf.py）。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import ValidationError

from . import models, observability
from .config import Settings, get_settings
from .coordinator import Coordinator
from .egress import EgressPool
from .errors import AdapterError, AuthError, InvalidParameterError
from .service import GenerationService, fingerprint_key
from .store import TaskStore
from .upstream import ImageFreeClient


def _setup_logging(settings: Settings) -> None:
    """loguru 直接接管：stdout + 单行格式。`LOG_LEVEL` 是唯一旋钮。"""
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization")
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _configured_keys(settings: Settings) -> frozenset[str]:
    """解析 `API_KEYS` 白名单（去空、去重）。**明文永不落库**。"""
    return frozenset(k.strip() for k in settings.api_keys.split(",") if k.strip())


def _resolve_key(request: Request, settings: Settings) -> str | None:
    """把请求头里的 Key 换成指纹。**返回 None = 匿名**；无效 Key ⇒ 401。"""
    keys = _configured_keys(settings)
    token = _bearer(request)
    if not keys:
        # 鉴权整体关闭（启动已打 WARNING）。带了 Key 也不校验 —— 但别静默吞掉拼写错误：
        # 关闭状态下没有"正确的 Key"可对照，校验只会变成猜谜。
        return None
    if token is None:
        return None
    if token not in keys:
        raise AuthError("Authorization 的 Bearer Key 不在白名单里。")
    return fingerprint_key(token)


def _require_key(request: Request, settings: Settings) -> str | None:
    """列表 / 删除 / 受理用：配了 Key 就必须带对的。"""
    keys = _configured_keys(settings)
    if not keys:
        return None
    token = _bearer(request)
    if token is None:
        raise AuthError("缺少 Authorization: Bearer <key>（本服务已启用鉴权）。")
    if token not in keys:
        raise AuthError("Authorization 的 Bearer Key 不在白名单里。")
    return fingerprint_key(token)


def create_app(
    *,
    settings: Settings | None = None,
    store: TaskStore | None = None,
    client: ImageFreeClient | None = None,
    pool: EgressPool | None = None,
    start_coordinator: bool | None = None,
) -> FastAPI:
    """应用工厂。

    测试注入 `store` + `pool`（每个出口一个假上游）即可做到**零出网**；
    只给一个出口时用 `client=`（会被包成单出口池）。
    """
    cfg = settings or get_settings()
    _setup_logging(cfg)
    obs = observability.configure(cfg)

    owns_store = store is None
    task_store = store or TaskStore(cfg.task_db)
    # 🔴 出口池**在这里就构造**：IMAGEFREE_PROXIES 写错必须表现为**启动失败**，
    #    而不是跑起来之后静默地全走直连（那等于配了个寂寞）。
    owns_pool = pool is None and client is None
    egress_pool = pool or (
        EgressPool.for_single_client(cfg, client) if client is not None else EgressPool(cfg)
    )
    service = GenerationService(task_store, cfg)
    coordinator = Coordinator(task_store, egress_pool, cfg)
    if egress_pool.is_rotating:
        logger.info(
            "出口轮换已启用：{} 个出口（{}）。",
            len(egress_pool),
            ", ".join(str(e.masked()) for e in egress_pool.egresses),
        )
    if egress_pool.is_rotating and cfg.imagefree_free_generation_id:
        logger.warning(
            "同时配了多个出口与固定的 IMAGEFREE_FREE_GENERATION_ID：该值只会成为各出口的**初始** cookie。"
            "上游限流按 **IP** 记账（单 IP 3 个在途任务，见 docs/UPSTREAM.md §9），"
            "浏览器身份那一层从未触发过 ⇒ 固定它既无益也无害，建议留空以免误解。"
        )

    should_start = bool(cfg.coordinator_enabled) if start_coordinator is None else start_coordinator
    if not cfg.coordinator_enabled:
        logger.warning(
            "协调器被禁用（COORDINATOR_ENABLED=0）⇒ 任务会被受理但**永不提交上游**。"
            "冒烟/自检时这是刻意的；生产跑成这样就是故障。"
        )
    if not _configured_keys(cfg):
        logger.warning("API_KEYS 为空 ⇒ 鉴权已整体关闭。**仅限内网**，生产必须配。")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if should_start:
            task = asyncio.create_task(coordinator.run_forever())
        try:
            yield
        finally:
            coordinator.stop()
            if task is not None:
                try:
                    # 停止信号会**拦在提交动作之前**，所以这里等的通常只是"一个已经在飞的
                    # HTTP 请求"（查询或提交）。预算与 gunicorn 的 graceful_timeout 对齐。
                    await asyncio.wait_for(task, timeout=cfg.coordinator_stop_grace)
                except (TimeoutError, asyncio.CancelledError):
                    # 强杀：那一刻若正好有提交在飞，重启后会被判 `submit_unknown`
                    # （不会静默双建）。这条日志是为了让排障的人知道去库里找它。
                    logger.warning(
                        "协调器在 {:.0f}s 内没停下来（线程里的 HTTP 调用取消不掉）⇒ 强制取消；"
                        "若当时有提交在飞，重启后该任务会被判 `submit_unknown`。",
                        cfg.coordinator_stop_grace,
                    )
                    task.cancel()
            if owns_pool:
                egress_pool.close()
            if owns_store:
                task_store.close()

    app = FastAPI(
        title="imagefree-service",
        version="0.1.0",
        description=(
            "imagefree.net 的异步图片生成出口。受理 → 轮询两段式；"
            "契约见 docs/INTERFACE.md，上游取证见 docs/UPSTREAM.md。"
        ),
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.store = task_store
    app.state.service = service
    app.state.egress_pool = egress_pool
    app.state.coordinator = coordinator
    app.state.observability = obs

    # ---------------------------------------------------------------- 错误信封
    @app.exception_handler(AdapterError)
    async def _adapter_error_handler(_: Request, exc: AdapterError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(status_code=exc.status_code, content=exc.to_error(), headers=headers)

    # ---------------------------------------------------------------- 对外契约
    @app.post("/async/v1/images/generations", status_code=202)
    async def create_generation(
        request: Request,
        payload: Any = Body(default=None),
    ) -> JSONResponse:
        cfg_now: Settings = app.state.settings
        fingerprint = _require_key(request, cfg_now)
        if not isinstance(payload, dict):
            raise InvalidParameterError(
                "请求体必须是 JSON 对象（含 prompt 字段）。", param="body"
            )
        try:
            req = models.GenerationRequest.model_validate(payload)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first.get("loc", ())) or None
            raise InvalidParameterError(
                f"请求字段校验失败：{first.get('msg')}（字段 {loc or 'body'}）。", param=loc
            ) from exc

        task_id = service.accept(req, fingerprint)
        logger.info("已受理：{}（model={}）", task_id, req.model)
        return JSONResponse(
            status_code=202,
            content={"task_id": task_id},
            headers={"Location": f"/async/v1/images/generations/{task_id}"},
        )

    @app.get("/async/v1/images/generations/{task_id}")
    async def get_generation(request: Request, task_id: str) -> JSONResponse:
        # 带了 Key 就校验（错的必须 401），没带则放行 —— id 本身即凭据。
        _resolve_key(request, app.state.settings)
        status_code, body = service.get(task_id)
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/async/v1/images/generations")
    async def list_generations(
        request: Request, limit: int = 50, offset: int = 0
    ) -> JSONResponse:
        fingerprint = _require_key(request, app.state.settings)
        if limit < 1 or limit > 200:
            raise InvalidParameterError("limit 必须在 1..200 之间。", param="limit")
        if offset < 0:
            raise InvalidParameterError("offset 不能为负。", param="offset")
        return JSONResponse(content=service.list_tasks(fingerprint, limit=limit, offset=offset))

    @app.delete("/async/v1/images/generations/{task_id}")
    async def delete_generation(request: Request, task_id: str) -> JSONResponse:
        fingerprint = _require_key(request, app.state.settings)
        return JSONResponse(content=service.delete(task_id, fingerprint))

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        """模型清单（**OpenAI 兼容形态**，本服务唯一的 `models` 端点）。

        给 OpenAI 生态的客户端/网关做自动探测用（`openai` SDK 的 `models.list()`、
        one-api / new-api 的"拉取模型列表"）。只列**可调用**的模型 —— 四字段最小形状。

        🔴 2026-09-22 起：`/async/v1/models`（旧的富形态）**已移除，不做别名兼容**
        （裁决见 docs/INTERFACE.md §0.1）。全集 / 未启用能力 / 比例档改由
        `GET /capabilities`（运维端点）提供。
        """
        return JSONResponse(content=models.openai_models_payload())

    # ---------------------------------------------------------------- 运维端点
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """**零依赖**：不碰库、不碰上游。容器探活用。"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """会真查一次库（廉价），并报告上游配置是否齐备。**不打上游网络**。"""
        db_ok = True
        db_error: str | None = None
        try:
            task_store.count_by_status()
        except Exception as exc:  # noqa: BLE001 - 探活要把原因说出来，而不是 500
            db_ok = False
            db_error = repr(exc)
        cfg_now: Settings = app.state.settings
        ready = db_ok
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "degraded",
                "db": {"ok": db_ok, "error": db_error},
                "upstream": {
                    "base_url": cfg_now.imagefree_base_url,
                    "turnstile_token_configured": bool(cfg_now.imagefree_turnstile_token),
                    "pinned_browser_id": bool(cfg_now.imagefree_free_generation_id),
                    "egress": {
                        "count": len(egress_pool),
                        "rotating": egress_pool.is_rotating,
                        "labels": list(egress_pool.labels),
                        # 每个出口是否已拿到自己的浏览器身份（cookie 由上游下发，
                        # 所以第一次提交之前这里全是 false）。
                        "identity_acquired": {
                            label: value is not None
                            for label, value in egress_pool.browser_ids().items()
                        },
                    },
                },
                "coordinator_enabled": bool(app.state.settings.coordinator_enabled),
                "auth_enabled": bool(_configured_keys(cfg_now)),
            },
        )

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        return {
            **service.stats(),
            "gate": {
                "if_concurrency": app.state.settings.if_concurrency,
                # 有效容量 = min(IF_CONCURRENCY, 出口数)：两个值都报出来，
                # 免得有人看着 if_concurrency=5 以为真的会并行 5 个。
                "effective_capacity": app.state.coordinator.capacity,
                "if_min_interval": app.state.settings.if_min_interval,
                "if_per_minute": app.state.settings.if_per_minute,
            },
            "egress": egress_pool.state(
                in_flight_by_label=task_store.count_in_flight_by_egress()
            ),
        }

    @app.get("/capabilities")
    async def capabilities() -> dict[str, Any]:
        cfg_now: Settings = app.state.settings
        return {
            "service": cfg_now.otel_service_name,
            "capability": models.capability_payload(),
            "upstream": {
                "site": "imagefree.net",
                "auth_required": False,
                "aspect_ratios": list(models.ASPECT_RATIOS),
                "default_aspect_ratio": models.DEFAULT_ASPECT_RATIO,
                "max_images_per_request": 1,
                "accepts_reference_images": False,
                "cost_model": "free quota with in-flight mutex (browser id + ip)",
            },
            "deliberate_absences": list(models.DELIBERATE_ABSENCES),
            "degradable_fields": sorted(models.DEGRADABLE_FIELDS),
            "config": cfg_now.documented_defaults(),
            "egress": {
                "count": len(egress_pool),
                "rotating": egress_pool.is_rotating,
                "cooldown_seconds": cfg_now.imagefree_proxy_cooldown,
                # 🔴 只报**打码后**的代理地址（地址里可能带凭据）。
                "endpoints": [
                    {"label": e.label, "proxy": e.masked()} for e in egress_pool.egresses
                ],
            },
            "observability": obs,
        }

    return app


if __name__ == "__main__":  # pragma: no cover - 本地手跑用
    import uvicorn

    _cfg = get_settings()
    uvicorn.run(create_app(settings=_cfg), host=_cfg.host, port=_cfg.port)
