#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游客户端的**离线**验证：请求形态 + 错误码映射。

映射表是这一层唯一容易出错、且出错代价最大的地方：
把"上游忙"翻成"你写错了"，调用方会去改 prompt；
把"要过人机校验"翻成"限流"，调用方会一直重试并加深风控。
"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.errors import (
    ContentPolicyError,
    TurnstileRequired,
    UpstreamBrowserTaskActive,
    UpstreamError,
    UpstreamIpTaskActive,
    UpstreamRateLimited,
    UpstreamTimeout,
)
from app.upstream import BROWSER_ID_COOKIE, ImageFreeClient
from tests.conftest import IMAGE_URL, SUBMITTED_TASK_ID, FakeUpstream


def _client(settings: Settings, handler: object) -> ImageFreeClient:
    assert callable(handler)
    return ImageFreeClient(settings, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 提交：请求形态
# ---------------------------------------------------------------------------


def test_submit_body_has_exactly_the_three_upstream_fields(settings: Settings, fake: FakeUpstream) -> None:
    client = ImageFreeClient(settings, transport=fake.transport())
    assert client.submit("cat", "16:9") == SUBMITTED_TASK_ID
    assert fake.submit_calls == [{"prompt": "cat", "aspect_ratio": "16:9", "turnstile_token": None}]


def test_submit_looks_like_the_sites_own_frontend(settings: Settings, fake: FakeUpstream) -> None:
    """带 origin/referer 是**上游的正常调用姿势**（它是前端同源接口）。"""
    client = ImageFreeClient(settings, transport=fake.transport())
    client.submit("cat", "1:1")
    headers = fake.submit_headers[0]
    assert headers["origin"] == "https://imagefree.net"
    assert headers["referer"] == "https://imagefree.net/zh"
    assert "Chrome" in headers["user-agent"]


def test_submit_sends_null_token_by_default(make_settings: object, fake: FakeUpstream) -> None:
    """上游把 Turnstile 硬编码成关闭（docs/UPSTREAM.md §5）⇒ 默认就是 null。"""
    settings = make_settings()  # type: ignore[operator]
    client = ImageFreeClient(settings, transport=fake.transport())
    client.submit("cat", "1:1")
    assert fake.submit_calls[0]["turnstile_token"] is None


def test_submit_sends_configured_turnstile_token(make_settings: object, fake: FakeUpstream) -> None:
    """开关被打开后，配置项是唯一的出口。"""
    settings = make_settings(imagefree_turnstile_token="tok-123")  # type: ignore[operator]
    client = ImageFreeClient(settings, transport=fake.transport())
    client.submit("cat", "1:1")
    assert fake.submit_calls[0]["turnstile_token"] == "tok-123"


def test_missing_task_id_is_an_error_even_with_http_200(settings: Settings, fake: FakeUpstream) -> None:
    """上游回 200 但没有 taskId ⇒ 必须失败（前端同样以此判失败）。"""
    fake.submit_override = {"ok": True}
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(UpstreamError) as excinfo:
        client.submit("cat", "1:1")
    assert "taskId" in excinfo.value.message


# ---------------------------------------------------------------------------
# 提交：错误码映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["FREE_GENERATION_ACTIVE", "FREE_TASK_BROWSER_ACTIVE"])
def test_browser_level_mutex_maps_to_its_own_code(
    settings: Settings, fake: FakeUpstream, code: str
) -> None:
    fake.fail_submit(code, "another generation is running")
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(UpstreamBrowserTaskActive) as excinfo:
        client.submit("cat", "1:1")
    assert excinfo.value.status_code == 429
    assert excinfo.value.code == "upstream_browser_task_active"
    # 「在途互斥」的等待时长取决于别人 ⇒ **不许**编 Retry-After
    assert excinfo.value.retry_after is None


def test_ip_level_mutex_maps_to_its_own_code(settings: Settings, fake: FakeUpstream) -> None:
    fake.fail_submit("FREE_TASK_IP_ACTIVE", "network task limit reached")
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(UpstreamIpTaskActive) as excinfo:
        client.submit("cat", "1:1")
    assert excinfo.value.status_code == 429
    assert excinfo.value.code == "upstream_ip_task_active"


def test_turnstile_hint_maps_to_deployment_problem(settings: Settings, fake: FakeUpstream) -> None:
    fake.fail_submit("TURNSTILE_REQUIRED", "please verify you are human (turnstile)")
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(TurnstileRequired) as excinfo:
        client.submit("cat", "1:1")
    assert excinfo.value.status_code == 503  # 部署问题 ≠ 调用方的问题
    assert "IMAGEFREE_TURNSTILE_TOKEN" in excinfo.value.message


def test_policy_hint_maps_to_content_policy(settings: Settings, fake: FakeUpstream) -> None:
    fake.fail_submit("CONTENT_BLOCKED", "prompt violates the safety policy")
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(ContentPolicyError):
        client.submit("cat", "1:1")


def test_unknown_error_code_with_429_maps_to_rate_limited(settings: Settings, fake: FakeUpstream) -> None:
    fake.submit_override = {"error": "too many requests"}
    fake.submit_status_code = 429
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(UpstreamRateLimited):
        client.submit("cat", "1:1")


def test_retry_after_is_reported_only_when_the_upstream_actually_sent_it(
    settings: Settings,
) -> None:
    def with_header(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": "slow down"}, headers={"Retry-After": "42"}
        )

    client = _client(settings, with_header)
    with pytest.raises(UpstreamRateLimited) as excinfo:
        client.submit("cat", "1:1")
    assert excinfo.value.retry_after == 42.0

    def without_header(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    client2 = _client(settings, without_header)
    with pytest.raises(UpstreamRateLimited) as excinfo2:
        client2.submit("cat", "1:1")
    assert excinfo2.value.retry_after is None


# ---------------------------------------------------------------------------
# 提交：传输层异常
# ---------------------------------------------------------------------------


def test_non_json_body_is_upstream_error(settings: Settings) -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html><body>Just a moment...</body></html>")

    client = _client(settings, html)
    with pytest.raises(UpstreamError) as excinfo:
        client.submit("cat", "1:1")
    assert "不是 JSON" in excinfo.value.message
    assert "Cloudflare" in excinfo.value.message


def test_http_500_is_upstream_error(settings: Settings) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client = _client(settings, boom)
    with pytest.raises(UpstreamError) as excinfo:
        client.submit("cat", "1:1")
    assert excinfo.value.status_code == 502


def test_timeout_is_upstream_timeout(settings: Settings, fake: FakeUpstream) -> None:
    fake.submit_raises = httpx.ConnectTimeout("timed out")
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(UpstreamTimeout) as excinfo:
        client.submit("cat", "1:1")
    assert excinfo.value.status_code == 504
    # 超时后**不能**断言任务没建成功 —— 只能说"我们没拿到 taskId"
    assert "可能" in excinfo.value.message


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_status_success_requires_image(settings: Settings, fake: FakeUpstream) -> None:
    fake.script_status("t1", {"status": "completed", "image": IMAGE_URL, "progress": 100})
    client = ImageFreeClient(settings, transport=fake.transport())
    status = client.fetch_status("t1")
    assert status.is_success and status.image == IMAGE_URL and status.progress == 100


def test_completed_without_image_is_not_success(settings: Settings, fake: FakeUpstream) -> None:
    """与前端判据逐字一致：`completed && image` 才算成图。"""
    fake.script_status("t1", {"status": "completed", "progress": 100})
    client = ImageFreeClient(settings, transport=fake.transport())
    status = client.fetch_status("t1")
    assert not status.is_success and not status.is_failed


def test_pending_is_neither_success_nor_failure(settings: Settings, fake: FakeUpstream) -> None:
    fake.script_status("t1", {"status": "processing", "progress": 40})
    client = ImageFreeClient(settings, transport=fake.transport())
    status = client.fetch_status("t1")
    assert not status.is_success and not status.is_failed
    assert status.raw_status == "processing"


def test_status_failed_is_terminal_failure(settings: Settings, fake: FakeUpstream) -> None:
    fake.script_status("t1", {"status": "failed"})
    client = ImageFreeClient(settings, transport=fake.transport())
    assert client.fetch_status("t1").is_failed


def test_status_error_payload_is_mapped_like_submit(settings: Settings, fake: FakeUpstream) -> None:
    fake.script_status("t1", {"error": "ip busy", "errorCode": "FREE_TASK_IP_ACTIVE"})
    client = ImageFreeClient(settings, transport=fake.transport())
    with pytest.raises(UpstreamIpTaskActive):
        client.fetch_status("t1")


def test_status_is_queried_by_task_id_query_param(settings: Settings, fake: FakeUpstream) -> None:
    client = ImageFreeClient(settings, transport=fake.transport())
    client.fetch_status("abc-123")
    assert fake.status_calls == ["abc-123"]


# ---------------------------------------------------------------------------
# 浏览器身份 cookie
# ---------------------------------------------------------------------------


def test_pinned_browser_id_is_sent_when_configured(make_settings: object, fake: FakeUpstream) -> None:
    settings = make_settings(imagefree_free_generation_id="pinned-id")  # type: ignore[operator]
    client = ImageFreeClient(settings, transport=fake.transport())
    client.fetch_status("t1")
    assert f"{BROWSER_ID_COOKIE}=pinned-id" in fake.status_headers[0]["cookie"]


def test_no_cookie_header_when_not_configured(settings: Settings, fake: FakeUpstream) -> None:
    client = ImageFreeClient(settings, transport=fake.transport())
    client.fetch_status("t1")
    assert "cookie" not in {k.lower() for k in fake.status_headers[0]}


def test_upstream_set_cookie_is_learned_and_reused(settings: Settings) -> None:
    """§2.3 的未取证假设：上游在首次提交时下发浏览器身份 ⇒ cookie jar 自动持有。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie", ""))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"taskId": "t1"},
                headers={"set-cookie": f"{BROWSER_ID_COOKIE}=issued-by-upstream; Path=/"},
            )
        return httpx.Response(200, json={"status": "pending"})

    client = ImageFreeClient(settings, transport=httpx.MockTransport(handler))
    client.submit("cat", "1:1")
    assert client.browser_id == "issued-by-upstream"
    client.fetch_status("t1")
    assert seen[0] == ""  # 第一次提交时还没有身份
    assert f"{BROWSER_ID_COOKIE}=issued-by-upstream" in seen[1]  # 之后自动带上
