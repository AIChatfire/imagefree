#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图生图（image-i2i → /api/ai-photo-editor）链路的**离线**验证。

覆盖四段：参考图代取（`app/reference.py`，含 SSRF/限额防护）、
三步流客户端（`app/upstream.py::submit_i2i`）、协调器分支（取图/提交/轮询/独立预算）、
受理校验的完整矩阵。全部零出网、零上游额度。
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.errors import InvalidImageError, TurnstileRequired, UpstreamError
from app.models import MODEL_I2I, MODEL_T2I, GenerationRequest
from app.reference import ReferenceImage, load_reference
from app.service import GenerationService
from app.store import FAILURE, IN_PROGRESS, SUCCESS, TaskStore
from app.upstream import ImageFreeClient
from tests.conftest import IMAGE_URL, SUBMITTED_TASK_ID, FakeUpstream

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _data_uri(mime: str = "image/png", payload: bytes | None = None, *, good: bool = True) -> str:
    """默认 payload 是**真 PNG**（8×8 黑图）—— 必须能过代取层的位图解码校验。"""
    if payload is None:
        payload = _real_png_bytes()
    raw = base64.b64encode(payload).decode() if good else "!!!not-base64!!!"
    return f"data:{mime};base64,{raw}"


def _real_png_bytes() -> bytes:
    import cv2 as _cv2
    import numpy as _np

    ok, buf = _cv2.imencode(".png", _np.zeros((8, 8, 3), dtype=_np.uint8))
    assert ok
    return buf.tobytes()


def _png_bytes(size: int = 64) -> bytes:
    """**可解码**的真 PNG（旧版假字节会被代取层的解码校验正确拒绝）。"""
    return _real_png_bytes()


def _http_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _i2i_settings(make_settings: Any, **extra: Any) -> Settings:
    return make_settings(imagefree_turnstile_token="tok-test", **extra)


def _accept_i2i(service: GenerationService, ref: str, prompt: str = "把背景换成雪山") -> str:
    return service.accept(
        GenerationRequest(prompt=prompt, model=MODEL_I2I, image=[ref]), key_fingerprint=None
    )


# ---------------------------------------------------------------------------
# 参考图代取（app/reference.py）
# ---------------------------------------------------------------------------


class TestReferenceDataUri:
    def test_valid_data_uri_round_trips(self) -> None:
        payload = _png_bytes()
        ref = load_reference(_data_uri(payload=payload), max_bytes=10 * 1024 * 1024, timeout=5)
        assert isinstance(ref, ReferenceImage)
        assert ref.data == payload
        assert ref.content_type == "image/png"
        assert ref.filename.endswith(".png")

    def test_oversize_rejected(self) -> None:
        with pytest.raises(InvalidImageError, match="超过上限"):
            load_reference(_data_uri(payload=b"x" * 1024), max_bytes=1, timeout=5)

    def test_bad_base64_rejected(self) -> None:
        with pytest.raises(InvalidImageError, match="base64"):
            load_reference(_data_uri(good=False), max_bytes=1024, timeout=5)

    def test_non_image_mime_rejected(self) -> None:
        uri = "data:text/html;base64," + base64.b64encode(b"<h1>").decode()
        with pytest.raises(InvalidImageError, match="data:image"):
            load_reference(uri, max_bytes=1024, timeout=5)


class TestReferenceHttp:
    def test_happy_path_downloads_image(self) -> None:
        png = _png_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "cdn.example.com"
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})

        ref = load_reference(
            "https://cdn.example.com/ref.png",
            max_bytes=1024 * 1024,
            timeout=5,
            transport=_http_transport(handler),
        )
        assert ref.data == png
        assert ref.filename == "ref.png"

    def test_content_length_over_limit_rejected_early(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"x", headers={"content-type": "image/png", "content-length": "999"}
            )

        with pytest.raises(InvalidImageError, match="Content-Length"):
            load_reference(
                "https://cdn.example.com/a.png",
                max_bytes=16,
                timeout=5,
                transport=_http_transport(handler),
            )

    def test_non_image_content_type_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})

        with pytest.raises(InvalidImageError, match="image/"):
            load_reference(
                "https://cdn.example.com/a.html",
                max_bytes=1024,
                timeout=5,
                transport=_http_transport(handler),
            )

    def test_http_error_status_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with pytest.raises(InvalidImageError, match="404"):
            load_reference(
                "https://cdn.example.com/gone.png",
                max_bytes=1024,
                timeout=5,
                transport=_http_transport(handler),
            )

    @pytest.mark.parametrize("url", ["http://127.0.0.1/x.png", "http://10.0.0.1/x.png",
                                     "http://169.254.169.254/latest/meta-data", "ftp://x/y.png"])
    def test_ssrf_and_scheme_guards(self, url: str) -> None:
        with pytest.raises(InvalidImageError):
            load_reference(url, max_bytes=1024, timeout=5)

    def test_redirect_to_private_host_rejected(self) -> None:
        """外层公网、重定向进内网 —— 每一跳都要重新过内网检查。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": "http://10.0.0.1/secret.png"})
            return httpx.Response(200, content=b"x")

        with pytest.raises(InvalidImageError, match="内网"):
            load_reference(
                "https://public.example.com/step.png",
                max_bytes=1024,
                timeout=5,
                transport=_http_transport(handler),
            )


# ---------------------------------------------------------------------------
# 三步流客户端（app/upstream.py::submit_i2i）
# ---------------------------------------------------------------------------


def _reference() -> ReferenceImage:
    return ReferenceImage(data=_png_bytes(), filename="ref.png", content_type="image/png")


class TestSubmitI2i:
    def test_happy_path_three_steps_in_order(
        self, make_settings: Any, fake: FakeUpstream
    ) -> None:
        settings = _i2i_settings(make_settings)
        client = ImageFreeClient(settings, transport=fake.transport())
        task_id = client.submit_i2i("把背景换成雪山", _reference())
        assert task_id == SUBMITTED_TASK_ID
        # 顺序：upload-url → PUT → create
        assert len(fake.i2i_upload_url_calls) == 1
        assert fake.i2i_upload_url_calls[0] == {"filename": "ref.png", "content_type": "image/png"}
        assert fake.i2i_put_calls[0]["bytes"] == len(_png_bytes())
        create = fake.i2i_create_calls[0]
        assert create["image_url"].endswith(".png") and "r2" in create["image_url"]
        assert create["prompt"] == "把背景换成雪山"
        assert create["turnstile_token"] == "tok-test"

    def test_missing_token_fails_before_any_request(self, make_settings: Any, fake: FakeUpstream) -> None:
        client = ImageFreeClient(make_settings(), transport=fake.transport())
        with pytest.raises(TurnstileRequired, match="IMAGEFREE_TURNSTILE_TOKEN"):
            client.submit_i2i("x", _reference())
        assert fake.i2i_upload_url_calls == [], "🔴 没 token 就一个请求都不许发"

    def test_verification_failure_maps_to_turnstile(self, make_settings: Any, fake: FakeUpstream) -> None:
        fake.i2i_create_override = 400  # 模拟 "Human verification failed"
        client = ImageFreeClient(_i2i_settings(make_settings), transport=fake.transport())
        with pytest.raises(TurnstileRequired, match="Turnstile"):
            client.submit_i2i("x", _reference())

    def test_upload_url_error_maps_to_upstream_error(self, make_settings: Any, fake: FakeUpstream) -> None:
        fake.i2i_upload_url_override = 500
        client = ImageFreeClient(_i2i_settings(make_settings), transport=fake.transport())
        with pytest.raises(UpstreamError):
            client.submit_i2i("x", _reference())
        assert fake.i2i_create_calls == [], "上传段失败 ⇒ 建任务（消耗额度的一步）不许发生"

    def test_put_failure_maps_to_upstream_error(self, make_settings: Any, fake: FakeUpstream) -> None:
        fake.i2i_put_status = 403
        client = ImageFreeClient(_i2i_settings(make_settings), transport=fake.transport())
        with pytest.raises(UpstreamError, match="直传失败"):
            client.submit_i2i("x", _reference())
        assert fake.i2i_create_calls == []

    def test_tool_status_uses_editor_endpoint(self, make_settings: Any, fake: FakeUpstream) -> None:
        client = ImageFreeClient(_i2i_settings(make_settings), transport=fake.transport())
        fake.script_status(SUBMITTED_TASK_ID, {"status": "completed", "image": IMAGE_URL})
        status = client.fetch_status(SUBMITTED_TASK_ID, tool="ai-photo-editor")
        assert status.is_success and status.image == IMAGE_URL
        assert fake.status_calls == [SUBMITTED_TASK_ID]


# ---------------------------------------------------------------------------
# 协调器分支（取参考图 → 三步流提交 → 工具端点轮询 → 独立超时预算）
# ---------------------------------------------------------------------------


class TestCoordinatorI2i:
    def test_happy_path_end_to_end(self, store: TaskStore, make_settings: Any, fake: FakeUpstream) -> None:
        from datetime import timedelta

        from app.coordinator import Coordinator
        from app.store import utcnow

        settings = _i2i_settings(make_settings)
        service = GenerationService(store, settings)
        task_id = _accept_i2i(service, _data_uri())
        row = store.get_task(task_id)
        assert row is not None and row.image_ref and row.model == MODEL_I2I

        client = ImageFreeClient(settings, transport=fake.transport())
        from app.egress import EgressPool

        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        base = utcnow()
        coord.tick(now=base)  # 提交（三步流）
        row = store.get_task(task_id)
        assert row is not None and row.status == IN_PROGRESS
        assert row.upstream_task_id == SUBMITTED_TASK_ID
        assert len(fake.i2i_create_calls) == 1 and fake.submit_calls == [], "t2i 端点不许被碰"

        fake.script_status(SUBMITTED_TASK_ID, {"status": "completed", "image": IMAGE_URL})
        coord.tick(now=base + timedelta(seconds=6))  # 工具端点轮询 → completed
        row = store.get_task(task_id)
        assert row is not None and row.status == SUCCESS and row.image_url == IMAGE_URL

    def test_missing_token_fails_task_as_deployment_error(
        self, store: TaskStore, make_settings: Any, fake: FakeUpstream
    ) -> None:
        from app.coordinator import Coordinator
        from app.egress import EgressPool

        settings = make_settings(imagefree_turnstile_token="")
        task_id = _accept_i2i(GenerationService(store, settings), _data_uri())
        client = ImageFreeClient(settings, transport=fake.transport())
        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        coord.tick()
        row = store.get_task(task_id)
        assert row is not None and row.status == FAILURE
        assert row.error_code == "upstream_turnstile_required"
        assert fake.i2i_upload_url_calls == []

    def test_invalid_reference_fails_task_not_retry(
        self, store: TaskStore, make_settings: Any, fake: FakeUpstream
    ) -> None:
        from app.coordinator import Coordinator
        from app.egress import EgressPool

        settings = _i2i_settings(make_settings)
        task_id = _accept_i2i(GenerationService(store, settings), _data_uri(good=False))
        client = ImageFreeClient(settings, transport=fake.transport())
        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        coord.tick()
        row = store.get_task(task_id)
        assert row is not None and row.status == FAILURE
        assert row.error_code == "invalid_image"
        assert fake.i2i_upload_url_calls == [], "参考图不合格 ⇒ 不许发起任何上游动作"

    def test_i2i_has_its_own_timeout_budget(
        self, store: TaskStore, make_settings: Any, fake: FakeUpstream
    ) -> None:
        """同一年龄：t2i 已超时收口，i2i 还在预算内继续跑（编辑器 pending 10min+ 是真实形态）。"""
        from datetime import timedelta

        from app.coordinator import Coordinator
        from app.egress import EgressPool
        from app.store import utcnow

        settings = _i2i_settings(make_settings, task_timeout=100.0, task_timeout_i2i=3600.0,
                                 if_concurrency=2)
        service = GenerationService(store, settings)
        t2i_id = service.accept(GenerationRequest(prompt="cat"), key_fingerprint=None)
        i2i_id = _accept_i2i(service, _data_uri())

        client = ImageFreeClient(settings, transport=fake.transport())
        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        base = utcnow()
        coord.tick(now=base)  # 两个任务都提交
        assert store.get_task(t2i_id).status == IN_PROGRESS  # type: ignore[union-attr]
        assert store.get_task(i2i_id).status == IN_PROGRESS  # type: ignore[union-attr]

        coord.tick(now=base + timedelta(seconds=120))  # > t2i 预算，< i2i 预算
        assert store.get_task(t2i_id).status == FAILURE  # type: ignore[union-attr]
        assert store.get_task(i2i_id).status == IN_PROGRESS  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 受理校验矩阵（模型 × image 形态）
# ---------------------------------------------------------------------------


class TestAcceptValidation:
    def test_i2i_accepts_url_and_stores_ref(self, store: TaskStore, settings: Settings) -> None:
        task_id = _accept_i2i(GenerationService(store, settings), "https://cdn.example.com/a.png")
        row = store.get_task(task_id)
        assert row is not None
        assert row.model == MODEL_I2I and row.image_ref == "https://cdn.example.com/a.png"

    def test_i2i_requires_exactly_one_image(self, store: TaskStore, settings: Settings) -> None:
        from app.errors import InvalidParameterError
        from app.models import GenerationRequest as Req

        service = GenerationService(store, settings)
        with pytest.raises(InvalidParameterError, match="恰好 1 条"):
            service.accept(Req(prompt="x", model=MODEL_I2I), key_fingerprint=None)
        with pytest.raises(InvalidParameterError, match="只接受 1 张"):
            service.accept(
                Req(prompt="x", model=MODEL_I2I, image=["https://a.png", "https://b.png"]),
                key_fingerprint=None,
            )

    def test_i2i_rejects_non_url_entry(self, store: TaskStore, settings: Settings) -> None:
        service = GenerationService(store, settings)

        with pytest.raises(Exception, match="http|data:image"):
            _accept_i2i(service, RefShim())

    def test_t2i_with_image_still_400_and_mentions_i2i(
        self, store: TaskStore, settings: Settings
    ) -> None:
        from app.errors import InvalidParameterError
        from app.models import GenerationRequest as Req

        service = GenerationService(store, settings)
        with pytest.raises(InvalidParameterError, match="image-i2i"):
            service.accept(
                Req(prompt="cat", model=MODEL_T2I, image=["https://a.png"]), key_fingerprint=None
            )


class RefShim:
    """非字符串条目（触发类型拒绝）。"""

    def __str__(self) -> str:
        return "not-a-url"


# ---------------------------------------------------------------------------
# 多形态矩阵一：参考图形态（data URI / http 的内容多样性）
# ---------------------------------------------------------------------------


class TestReferenceVariants:
    def test_jpeg_webp_mime_maps_filename_ext_and_gif_rejected(self) -> None:
        import cv2 as _cv2
        import numpy as _np

        canvas = _np.zeros((8, 8, 3), dtype=_np.uint8)
        # 可校验的位图：JPEG / WebP —— 通过并按 Content-Type 映射扩展名
        for mime, ext in [("image/jpeg", "jpg"), ("image/webp", "webp")]:
            ok, buf = _cv2.imencode("." + ext, canvas)
            assert ok, ext
            ref = load_reference(
                _data_uri(mime=mime, payload=buf.tobytes()), max_bytes=1024 * 1024, timeout=5
            )
            assert ref.content_type == mime
            assert ref.filename.endswith(f".{ext}"), (mime, ref.filename)
        # GIF：cv2 解不了 ⇒ 代取层拒绝（诚实：校验不了的格式不往上游送）
        gif_uri = "data:image/gif;base64," + base64.b64encode(b"GIF89axxxx").decode()
        with pytest.raises(InvalidImageError, match="无法解码"):
            load_reference(gif_uri, max_bytes=1024, timeout=5)

    def test_real_png_passes_decode_validation(self) -> None:
        ref = load_reference(_data_uri(), max_bytes=1024 * 1024, timeout=5)
        assert len(ref.data) > 0

    def test_empty_payload_rejected(self) -> None:
        with pytest.raises(InvalidImageError, match="空文件"):
            load_reference(_data_uri(payload=b""), max_bytes=1024, timeout=5)

    def test_corrupted_png_body_rejected(self) -> None:
        """魔数对但内容坏 —— 代取层就拦，省一次上游往返。"""
        corrupt = b"\x89PNG\r\n\x1a\n" + b"\xff" * 64
        with pytest.raises(InvalidImageError, match="无法解码"):
            load_reference(_data_uri(payload=corrupt), max_bytes=1024, timeout=5)

    def test_svg_rejected_as_non_bitmap(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
        with pytest.raises(InvalidImageError, match="SVG"):
            load_reference(uri, max_bytes=1024, timeout=5)

    def test_http_corrupted_body_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"\xff" * 64, headers={"content-type": "image/png"})

        with pytest.raises(InvalidImageError, match="无法解码"):
            load_reference(
                "https://cdn.example.com/broken.png",
                max_bytes=1024,
                timeout=5,
                transport=_http_transport(handler),
            )

    def test_url_with_query_and_no_extension(self) -> None:
        png = _png_canvas_jpeg()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=png, headers={"content-type": "image/jpeg"})

        ref = load_reference(
            "https://cdn.example.com/images/serve?id=42",
            max_bytes=1024 * 1024,
            timeout=5,
            transport=_http_transport(handler),
        )
        assert ref.filename == "reference.jpg", "无扩展名路径 ⇒ 按 Content-Type 命名"


def _png_canvas() -> Any:
    import numpy as _np

    canvas = _np.zeros((8, 8, 3), dtype=_np.uint8)
    return canvas


def _png_canvas_jpeg() -> Any:
    import cv2 as _cv2
    import numpy as _np

    canvas = _np.zeros((8, 8, 3), dtype=_np.uint8)
    ok, buf = _cv2.imencode(".jpg", canvas)
    assert ok
    return buf.tobytes()


# ---------------------------------------------------------------------------
# 多形态矩阵二：请求形态（受理端）
# ---------------------------------------------------------------------------


class TestRequestVariants:
    def _svc(self, store: TaskStore, settings: Settings) -> GenerationService:
        return GenerationService(store, settings)

    @pytest.mark.parametrize("alias", ["image-i2i", "i2i", "image-to-image", "edit", "图生图", "编辑"])
    def test_all_i2i_aliases_accepted(self, store: TaskStore, settings: Settings, alias: str) -> None:
        task_id = self._svc(store, settings).accept(
            GenerationRequest(prompt="edit it", model=alias, image=["https://a.png"]),
            key_fingerprint=None,
        )
        row = store.get_task(task_id)
        assert row is not None and row.model == MODEL_I2I and row.image_ref == "https://a.png"

    def test_image_as_bare_string_rejected(self, store: TaskStore, settings: Settings) -> None:
        from app.errors import InvalidParameterError
        from app.models import GenerationRequest as Req

        with pytest.raises(InvalidParameterError, match="数组"):
            self._svc(store, settings).accept(
                Req(prompt="x", model=MODEL_I2I, image="https://a.png"), key_fingerprint=None
            )

    def test_image_empty_list_with_i2i_rejected(self, store: TaskStore, settings: Settings) -> None:
        from app.errors import InvalidParameterError
        from app.models import GenerationRequest as Req

        with pytest.raises(InvalidParameterError, match="恰好 1 条"):
            self._svc(store, settings).accept(
                Req(prompt="x", model=MODEL_I2I, image=[]), key_fingerprint=None
            )

    def test_image_empty_string_entry_rejected(self, store: TaskStore, settings: Settings) -> None:
        from app.errors import InvalidParameterError
        from app.models import GenerationRequest as Req

        with pytest.raises(InvalidParameterError, match="非空字符串"):
            self._svc(store, settings).accept(
                Req(prompt="x", model=MODEL_I2I, image=["   "]), key_fingerprint=None
            )

    def test_data_uri_must_be_base64_form(self) -> None:
        """非 base64 形态的 data URI 在**代取层**被拒（受理只查前缀，避免受理期解码）。"""
        with pytest.raises(InvalidImageError, match="data:image/"):
            load_reference("data:image/png,raw-percent", max_bytes=1024, timeout=5)

    def test_i2i_ignores_ratio_with_trace(self, store: TaskStore, settings: Settings) -> None:
        task_id = self._svc(store, settings).accept(
            GenerationRequest(
                prompt="x", model=MODEL_I2I, image=["https://a.png"], aspect_ratio="16:9", size="2048x2048"
            ),
            key_fingerprint=None,
        )
        _, body = self._svc(store, settings).get(task_id)
        traces = "\n".join(body["degradations"])
        assert "没有比例字段" in traces and "16:9" in traces
        assert "以 aspect_ratio 为准" in traces  # size 让位给 aspect_ratio 的既有留痕也在

    def test_i2i_with_n1_accepted(self, store: TaskStore, settings: Settings) -> None:
        from app.models import GenerationRequest as Req

        task_id = self._svc(store, settings).accept(
            Req(prompt="x", model=MODEL_I2I, image=["https://a.png"], n=1), key_fingerprint=None
        )
        assert store.get_task(task_id) is not None


# ---------------------------------------------------------------------------
# 多形态矩阵三：上游响应形态（三步流的每一步都可能"形变"）
# ---------------------------------------------------------------------------


class TestUpstreamResponseShapes:
    def _client(self, make_settings: Any, fake: FakeUpstream, **kw: Any) -> ImageFreeClient:
        return ImageFreeClient(_i2i_settings(make_settings, **kw), transport=fake.transport())

    def test_upload_url_429_wall_is_retryable(self, make_settings: Any, fake: FakeUpstream) -> None:
        fake.i2i_upload_url_override = {
            "error": "IP task active", "errorCode": "FREE_TASK_IP_ACTIVE"}
        client = self._client(make_settings, fake)
        from app.errors import UpstreamIpTaskActive

        with pytest.raises(UpstreamIpTaskActive):
            client.submit_i2i("x", _reference())

    def test_upload_url_non_json_is_upstream_error(self, make_settings: Any, fake: FakeUpstream) -> None:
        fake.i2i_upload_url_override = {"unexpected": "shape"}
        client = self._client(make_settings, fake)
        with pytest.raises(UpstreamError, match="uploadUrl"):
            client.submit_i2i("x", _reference())

    def test_create_missing_taskid_is_upstream_error(self, make_settings: Any, fake: FakeUpstream) -> None:
        fake.i2i_create_override = {"status": "pending"}
        client = self._client(make_settings, fake)
        with pytest.raises(UpstreamError, match="taskId"):
            client.submit_i2i("x", _reference())

    def test_editor_status_failed_fails_task(
        self, store: TaskStore, make_settings: Any, fake: FakeUpstream
    ) -> None:
        from datetime import timedelta

        from app.coordinator import Coordinator
        from app.egress import EgressPool
        from app.store import utcnow

        settings = _i2i_settings(make_settings)
        task_id = _accept_i2i(GenerationService(store, settings), _data_uri())
        client = self._client(make_settings, fake)
        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        base = utcnow()
        coord.tick(now=base)
        fake.script_status(SUBMITTED_TASK_ID, {"status": "failed", "progress": 0, "message": "bad input"})
        coord.tick(now=base + timedelta(seconds=6))
        row = store.get_task(task_id)
        assert row is not None and row.status == FAILURE and row.error_code == "task_failed"
        assert row.error_message and "bad input" in row.error_message

    def test_editor_completed_without_image_keeps_polling(
        self, store: TaskStore, make_settings: Any, fake: FakeUpstream
    ) -> None:
        """`completed` 但没有 image ⇒ 判据不满足（与前端逐字一致），继续轮询而非假成功。"""
        from datetime import timedelta

        from app.coordinator import Coordinator
        from app.egress import EgressPool
        from app.store import IN_PROGRESS, utcnow

        settings = _i2i_settings(make_settings)
        task_id = _accept_i2i(GenerationService(store, settings), _data_uri())
        client = self._client(make_settings, fake)
        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        base = utcnow()
        coord.tick(now=base)
        fake.script_status(SUBMITTED_TASK_ID, {"status": "completed", "progress": 99})
        coord.tick(now=base + timedelta(seconds=6))
        row = store.get_task(task_id)
        assert row is not None and row.status == IN_PROGRESS, "缺 image ≠ 成功"

    def test_coordinator_wall_on_i2i_schedules_retry(
        self, store: TaskStore, make_settings: Any, fake: FakeUpstream
    ) -> None:
        """直连出口撞 i2i 的在途互斥 ⇒ 原样放回池子退避（不换出口白撞、不判死）。"""
        from app.coordinator import Coordinator
        from app.egress import EgressPool
        from app.store import QUEUED

        settings = _i2i_settings(make_settings)
        task_id = _accept_i2i(GenerationService(store, settings), _data_uri())
        client = self._client(make_settings, fake)
        fake.i2i_upload_url_override = {
            "error": "IP task active", "errorCode": "FREE_TASK_IP_ACTIVE"}
        coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
        summary = coord.tick()
        assert summary["wall_blocked"] == 1
        row = store.get_task(task_id)
        assert row is not None and row.status == QUEUED
        assert row.error_code == "upstream_ip_task_active"


# ---------------------------------------------------------------------------
# 多形态矩阵四：t2i / i2i 共存过闸门
# ---------------------------------------------------------------------------


def test_gate_routes_both_kinds_in_one_tick(
    store: TaskStore, make_settings: Any, fake: FakeUpstream
) -> None:
    """同一 tick：t2i 走 /api/generate，i2i 走三步流 —— 互不串线。"""
    from app.coordinator import Coordinator
    from app.egress import EgressPool

    settings = _i2i_settings(make_settings, if_concurrency=2)
    service = GenerationService(store, settings)
    t2i_id = service.accept(GenerationRequest(prompt="cat"), key_fingerprint=None)
    i2i_id = _accept_i2i(service, _data_uri())

    client = ImageFreeClient(settings, transport=fake.transport())
    coord = Coordinator(store, EgressPool.for_single_client(settings, client), settings)
    summary = coord.tick()
    assert summary["submitted"] == 2
    assert len(fake.submit_calls) == 1, "t2i 恰好打生成端点一次"
    assert len(fake.i2i_create_calls) == 1, "i2i 恰好打编辑器端点一次"
    assert store.get_task(t2i_id).upstream_task_id == SUBMITTED_TASK_ID  # type: ignore[union-attr]
    assert store.get_task(i2i_id).upstream_task_id == SUBMITTED_TASK_ID  # type: ignore[union-attr]
