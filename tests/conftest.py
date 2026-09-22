#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具：**一个字节都不出网**。

三层防线（缺一层都会让"测试全绿"变成假象）：

  1. `block_real_network`（autouse, session）—— 把 httpx 的真实传输层改成"一碰就炸"，
     任何忘记注入假上游的用例都会**响亮失败**，而不是悄悄打真上游（那会消耗免费额度）；
  2. `FakeUpstream` —— 用 `httpx.MockTransport` 顶掉两个端点，行为可脚本化；
  3. 缺依赖就 import 失败 ⇒ pytest 报错。**不许 skip** —— 跳过会让人把"没跑"当成"跑过了"。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.egress import Egress, EgressPool
from app.main import create_app
from app.store import TaskStore
from app.upstream import ImageFreeClient

# ---------------------------------------------------------------------------
# 第 1 层：真实出网 = 立即失败
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def block_real_network() -> Any:
    """把 httpx 的真实传输层钉死。

    `MockTransport` 不经过 `HTTPTransport`，所以假上游照常工作 ——
    只有"真的想连出去"的代码会撞上它。
    """
    original = httpx.HTTPTransport.handle_request

    def _boom(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "测试试图**真实出网**（httpx.HTTPTransport.handle_request）。"
            f"请求：{request.method} {request.url}。"
            "提交任务会消耗上游免费额度 ⇒ 测试必须注入假上游（见 tests/conftest.py）。"
        )

    httpx.HTTPTransport.handle_request = _boom  # type: ignore[method-assign]
    try:
        yield
    finally:
        httpx.HTTPTransport.handle_request = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 假上游
# ---------------------------------------------------------------------------

SUBMITTED_TASK_ID = "adf689ee-0a7b-4418-8567-c1f0c66317b9"
IMAGE_URL = (
    "https://pub-62e693a7058040f98bba94ed1d6f880b.r2.dev/images/"
    "e34fe638-ae05-43c4-bf7a-11f61460afff.png"
)


@dataclass
class FakeUpstream:
    """imagefree.net 的两个端点的替身。

    默认行为：提交 → `{"taskId": ...}`；查询 → `{"status":"pending","progress":0}`。
    想让某个任务走完，用 `script_status(task_id, [...])` 排一串响应。
    """

    #: 提交响应。`None` ⇒ 用默认成功响应；dict ⇒ 原样返回；可调用 ⇒ 收 body 返回 (status, payload)
    submit_override: Any = None
    submit_status_code: int = 200
    #: taskId → 响应队列（弹尽后沿用最后一个）
    status_scripts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: 查询默认响应
    default_status: dict[str, Any] = field(
        default_factory=lambda: {"status": "pending", "progress": 10}
    )
    #: 让提交响应带上 Set-Cookie（模拟"上游下发浏览器身份"，见 docs/UPSTREAM.md §2.3）
    submit_set_cookie: str | None = None
    #: 让提交直接抛网络异常（模拟超时/断连）
    submit_raises: Exception | None = None
    #: 让查询直接抛网络异常
    status_raises: Exception | None = None
    #: 记录所有请求（断言"发了什么"用）
    submit_calls: list[dict[str, Any]] = field(default_factory=list)
    status_calls: list[str] = field(default_factory=list)
    submit_headers: list[dict[str, str]] = field(default_factory=list)
    status_headers: list[dict[str, str]] = field(default_factory=list)
    #: 图生图三步流的记录与剧本（docs/UPSTREAM.md §10）。
    i2i_upload_url_calls: list[dict[str, Any]] = field(default_factory=list)
    i2i_put_calls: list[dict[str, Any]] = field(default_factory=list)
    i2i_create_calls: list[dict[str, Any]] = field(default_factory=list)
    #: upload-url 响应剧本（None ⇒ 默认成功）。dict ⇒ 原样返回；int ⇒ 作状态码回空对象。
    i2i_upload_url_override: Any = None
    #: PUT 直传的状态码（None ⇒ 200）。
    i2i_put_status: int | None = None
    #: 建任务响应剧本（None ⇒ 默认成功 `{"taskId": SUBMITTED_TASK_ID, "status": "pending"}`）。
    i2i_create_override: Any = None

    # ------------------------------------------------------------ 脚本化助手
    def fail_submit(self, error_code: str, message: str = "upstream says no", *, status_code: int = 200) -> None:
        self.submit_override = {"error": message, "errorCode": error_code}
        self.submit_status_code = status_code

    def script_status(self, task_id: str, *responses: dict[str, Any]) -> None:
        self.status_scripts[task_id] = list(responses)

    def complete(self, task_id: str = SUBMITTED_TASK_ID, url: str = IMAGE_URL) -> None:
        self.script_status(task_id, {"status": "completed", "image": url, "progress": 100})

    # ------------------------------------------------------------ 传输层
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/generate" and request.method == "POST":
            return self._handle_submit(request)
        if path == "/api/generate/status":
            return self._handle_status(request)
        if path == "/api/ai-photo-editor/upload-url" and request.method == "POST":
            return self._handle_i2i_upload_url(request)
        if path == "/api/ai-photo-editor" and request.method == "POST":
            return self._handle_i2i_create(request)
        if path.startswith("/upscaler/") or path.startswith("/editor/"):
            return self._handle_i2i_put(request)
        if path == "/api/ai-photo-editor/status":
            return self._handle_status(request)
        return httpx.Response(404, json={"error": f"fake upstream: 未实现的路径 {path}"})

    def _handle_submit(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.submit_calls.append(body)
        self.submit_headers.append(dict(request.headers))
        if self.submit_raises is not None:
            raise self.submit_raises
        payload = self.submit_override
        if payload is None:
            payload = {"taskId": SUBMITTED_TASK_ID}
        if callable(payload):
            payload = payload(body)
        headers = {"set-cookie": self.submit_set_cookie} if self.submit_set_cookie else None
        return httpx.Response(self.submit_status_code, json=payload, headers=headers)

    def _handle_status(self, request: httpx.Request) -> httpx.Response:
        task_id = request.url.params.get("taskId", "")
        self.status_calls.append(task_id)
        self.status_headers.append(dict(request.headers))
        if self.status_raises is not None:
            raise self.status_raises
        queue = self.status_scripts.get(task_id)
        if queue:
            payload = queue[0]
            if len(queue) > 1:
                queue.pop(0)
        else:
            payload = dict(self.default_status)
        return httpx.Response(200, json=payload)

    # ---------------------------------------------- 图生图三步流（§10）
    _UPLOAD_URL = "https://fake-r2.r2.cloudflarestorage.com/editor/fake.png"
    _PUBLIC_URL = "https://fake-public.r2.dev/images/fake.png"

    def _handle_i2i_upload_url(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.i2i_upload_url_calls.append(body)
        if self.i2i_upload_url_override is None:
            payload = {"uploadUrl": self._UPLOAD_URL, "publicUrl": self._PUBLIC_URL}
            return httpx.Response(200, json=payload)
        if isinstance(self.i2i_upload_url_override, int):
            return httpx.Response(self.i2i_upload_url_override, json={"error": "boom"})
        return httpx.Response(200, json=self.i2i_upload_url_override)

    def _handle_i2i_put(self, request: httpx.Request) -> httpx.Response:
        self.i2i_put_calls.append(
            {"path": request.url.path, "bytes": len(request.content),
             "content_type": request.headers.get("content-type", "")}
        )
        return httpx.Response(self.i2i_put_status if self.i2i_put_status is not None else 200)

    def _handle_i2i_create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.i2i_create_calls.append(body)
        payload = self.i2i_create_override
        if payload is None:
            payload = {"taskId": SUBMITTED_TASK_ID, "status": "pending"}
        if callable(payload):
            payload = payload(body)
        if isinstance(payload, int):
            return httpx.Response(
                payload,
                json={"error": "Human verification failed. Please complete the challenge and try again."},
            )
        return httpx.Response(200, json=payload)

    # ------------------------------------------------------------ 挂到 httpx
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---------------------------------------------------------------------------
# 配置 / 库 / 客户端
# ---------------------------------------------------------------------------


@pytest.fixture
def make_settings(tmp_path: Any) -> Callable[..., Settings]:
    """构造隔离的 Settings（**不读项目 .env**，避免本机环境串进来）。"""

    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "task_db": f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}",
            "api_keys": "",
            "coordinator_enabled": 0,
            "logfire_token": "",
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)  # type: ignore[call-arg]

    return _make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def fake() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def store(settings: Settings) -> Any:
    s = TaskStore(settings.task_db)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(settings: Settings, fake: FakeUpstream) -> Any:
    c = ImageFreeClient(settings, transport=fake.transport())
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def app(settings: Settings, store: TaskStore, client: ImageFreeClient) -> Any:
    """注入假上游与临时库。**协调器不启动**（tick 由用例显式驱动）。"""
    return create_app(
        settings=settings, store=store, client=client, start_coordinator=False
    )


@pytest.fixture
def api(app: Any) -> Any:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def coord(app: Any) -> Any:
    """app.state 上的协调器（tick 由用例显式调用，等价于"手动推进时间"）。"""
    return app.state.coordinator


# ---------------------------------------------------------------------------
# 多出口（加代理）夹具
# ---------------------------------------------------------------------------


@dataclass
class EgressFixture:
    """一组假出口：每个出口一个假上游 + 一个独立 cookie jar。"""

    settings: Settings
    fakes: dict[str, FakeUpstream]
    pool: EgressPool

    def fake(self, label: str) -> FakeUpstream:
        return self.fakes[label]


@pytest.fixture
def make_pool(make_settings: Callable[..., Settings]) -> Any:
    """造一个多出口池。

    每个出口都注入**自己的** `MockTransport` ⇒ 零出网；
    代理地址是真的写进配置的（`http://10.0.0.N:8080`），但**永远不会被拨号**
    —— 这正是要验的：出口选择逻辑只看配置与在途状态。
    """
    created: list[EgressPool] = []

    def _make(labels: tuple[str, ...] = ("proxy1", "proxy2"), **overrides: Any) -> EgressFixture:
        proxies = [f"http://10.0.0.{idx}:8080" for idx in range(1, len(labels) + 1)]
        settings = make_settings(imagefree_proxies=",".join(proxies), **overrides)
        fakes: dict[str, FakeUpstream] = {}
        clients: dict[str, ImageFreeClient] = {}
        egresses: list[Egress] = []
        for label, proxy in zip(labels, proxies, strict=True):
            fake = FakeUpstream()
            fakes[label] = fake
            # 🔴 **刻意不给客户端传 proxy**：httpx 在同时收到 `transport=` 与 `proxy=` 时
            # 会走**代理传输层**，把假传输整个绕开 —— 实测就是这条让"零出网"守卫响了
            # （只探测 `client._transport` 属性会被骗过去，它看着是 MockTransport）。
            # 代理地址仍然记在 `Egress.proxy` 上（打码/观测/真实部署都要用），
            # 只是测试里的出口**不需要**它：反正请求都被假上游接住了。
            clients[label] = ImageFreeClient(settings, label=label, transport=fake.transport())
            egresses.append(Egress(label=label, proxy=proxy))
        pool = EgressPool(settings, clients=clients, egresses=tuple(egresses))
        created.append(pool)
        return EgressFixture(settings=settings, fakes=fakes, pool=pool)

    try:
        yield _make
    finally:
        for pool in created:
            pool.close()
