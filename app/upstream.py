#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游客户端：imagefree.net 的两个端点。

    POST /api/generate              → {"taskId": "..."}      （提交，消耗免费额度）
    GET  /api/generate/status?taskId=…  → {"status","image","progress"}（查询，零额度）

形态与请求头**照抄浏览器抓包**（`docs/UPSTREAM.md` §2/§3）：
上游是前端同源接口，带 `origin`/`referer` 是让它把我们当成自家页面
（实测不带也通，但没理由去掉——这是它的正常调用姿势）。

错误映射是本文件的核心价值：上游把限流语义塞在 `errorCode` 里，
我们的对外信封要把它们翻成**不同的下一步动作**（见 app/errors.py）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from . import models
from .config import Settings
from .errors import (
    AdapterError,
    ContentPolicyError,
    EgressUnavailable,
    TurnstileRequired,
    UpstreamBrowserTaskActive,
    UpstreamError,
    UpstreamIpTaskActive,
    UpstreamRateLimited,
    UpstreamTimeout,
)
from .observability import upstream_span
from .turnstile import token_for_tool_submit

#: 上游 `errorCode` → 我们的异常类。**语义映射，不是字符串透传**。
_ERROR_CODE_MAP: dict[str, type[AdapterError]] = {
    "FREE_GENERATION_ACTIVE": UpstreamBrowserTaskActive,
    "FREE_TASK_BROWSER_ACTIVE": UpstreamBrowserTaskActive,
    "FREE_TASK_IP_ACTIVE": UpstreamIpTaskActive,
}

#: 上游可能用来表达"内容不合规"的字样（**未取证**，故按关键词兜底识别）。
_POLICY_HINTS = ("policy", "prohibited", "violat", "safety", "moderation", "blocked")

#: 可能表达"要过 Turnstile"的字样。
_TURNSTILE_HINTS = ("turnstile", "challenge", "captcha", "verify")

_BROWSER_HEADERS: dict[str, str] = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/json",
    "origin": "https://imagefree.net",
    "referer": "https://imagefree.net/zh",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}

#: 上游浏览器身份 cookie 名（`docs/UPSTREAM.md` §2.3）。
BROWSER_ID_COOKIE = "imagefree_free_generation_id"


@dataclass
class UpstreamStatus:
    """上游查询的解析结果。"""

    #: 上游原始 `status` 字符串（`completed` / `failed` / 其它=非终态）。
    raw_status: str
    image: str | None = None
    progress: int | None = None
    message: str | None = None

    @property
    def is_success(self) -> bool:
        """**必须同时有 `status=completed` 与 `image`** —— 与前端判据逐字一致。"""
        return self.raw_status == "completed" and bool(self.image)

    @property
    def is_failed(self) -> bool:
        return self.raw_status == "failed"


class ImageFreeClient:
    """上游两个端点的薄封装。**无凭据**：上游不需要登录。"""

    def __init__(
        self,
        settings: Settings,
        *,
        proxy: str | None = None,
        label: str = "direct",
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        #: 出口标签（诊断用：日志里能看出是哪个出口在说话）。
        self._label = label
        self._proxy = proxy
        if client is not None:
            self._client = client
        else:
            cookies: dict[str, str] = {}
            if settings.imagefree_free_generation_id:
                cookies[BROWSER_ID_COOKIE] = settings.imagefree_free_generation_id
            self._client = httpx.Client(
                base_url=settings.imagefree_base_url.rstrip("/"),
                headers=dict(_BROWSER_HEADERS),
                cookies=cookies,
                timeout=settings.upstream_timeout,
                follow_redirects=True,
                transport=transport,
                proxy=proxy,
                # 配了显式代理就不让环境变量/系统代理插手：否则本机的系统代理
                # （macOS `scutil` 那套）会把"出口 A"的流量再代理一次，
                # 表现为"配了 N 个出口、实际全从同一个地方出去"。
                # 没配代理时保持 httpx 默认（走环境代理），与加代理之前一致。
                trust_env=proxy is None,
            )

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ImageFreeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def label(self) -> str:
        """出口标签（`direct` / `proxy1` / 自定义）。"""
        return self._label

    @property
    def proxy(self) -> str | None:
        """该出口用的代理地址（**可能含凭据** —— 对外只报 `Egress.masked()`）。"""
        return self._proxy

    @property
    def browser_id(self) -> str | None:
        """当前持有的上游浏览器身份（诊断用；不含其他 cookie）。"""
        return self._client.cookies.get(BROWSER_ID_COOKIE)

    # ------------------------------------------------------------------ 提交
    def submit(self, prompt: str, aspect_ratio: str) -> str:
        """提交生成任务，返回上游 `taskId`。

        ⚠️ 这是**消耗免费额度**的动作 —— 只允许协调器在闸门内调用。
        """
        body = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            # 上游当前把 Turnstile 硬编码关闭，正常就是 null（docs/UPSTREAM.md §5）。
            "turnstile_token": self._settings.imagefree_turnstile_token or None,
        }
        with upstream_span("upstream.submit", {"request": body}):
            try:
                resp = self._client.post("/api/generate", json=body)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(
                    f"提交上游超时（>{self._settings.upstream_timeout}s）。"
                    "任务**可能**已在跑，但我们没拿到 taskId。"
                ) from exc
            except httpx.HTTPError as exc:
                # 传输层失败 = **这条出口坏了**（代理认证失败/DNS/连接被拒），
                # 不是上游拒绝 ⇒ 让协调器去换出口，别混进 upstream_error。
                raise EgressUnavailable(f"提交上游网络错误（出口 {self._label}）：{exc!r}") from exc

            payload = self._decode(resp, stage="提交")
            # 上游可能用 **HTTP 200 + {error, errorCode}** 表达拒绝
            # （HTTP 码未取证，见 docs/UPSTREAM.md §7）⇒ 先看 error 信封，
            # 再看状态码。只看状态码会让整套错误码映射变成死代码。
            if payload.get("error"):
                raise self._from_error_payload(payload, resp)
            if resp.status_code >= 400:
                raise self._from_error_payload(payload, resp)

            task_id = payload.get("taskId")
            if not task_id:
                raise UpstreamError(
                    "上游没有回 taskId（前端同样以此判失败）。",
                    detail={"status_code": resp.status_code, "payload": payload},
                )
            return str(task_id)

    # ------------------------------------------------------------ 图生图提交
    #: 图生图走的上游工具端点（docs/UPSTREAM.md §10；三步流：上传地址 → 直传 → 建任务）。
    TOOL_EDITOR = "ai-photo-editor"

    def submit_i2i(self, prompt: str, reference: Any) -> str:
        """图生图提交：upload-url → PUT 直传 → 建任务，返回上游 `taskId`。

        与 :meth:`submit` 同级：**消耗免费额度**（额度只在第 3 步建任务成功时消耗；
        前两步失败都是零额度损失）。工具端点**强制 Turnstile**
        （docs/UPSTREAM.md §10.1）⇒ 没配 token 时在**发起任何请求之前**就失败。

        ``reference`` 是 `app.reference.ReferenceImage`（用 Any 注解避免循环导入）。
        """
        # 🔴 token **现铸现用**：静态注入的值几分钟就过期（见 app/turnstile.py）。
        # 没配铸造命令时退回静态值；两者都没有 ⇒ 在**发起任何请求之前**失败（零额度损失）。
        token = token_for_tool_submit(self._settings)
        if not token:
            raise TurnstileRequired(
                "图生图走的工具端点强制 Turnstile（与文生图的死开关不同，"
                "docs/UPSTREAM.md §10.1），而 IMAGEFREE_TURNSTILE_TOKEN 未配置 —— 部署问题。"
                "铸 token 配方：scripts/mint_turnstile.cjs（真 Chrome，token 有效期仅几分钟）。"
            )

        base = f"/api/{self.TOOL_EDITOR}"
        with upstream_span("upstream.i2i.upload_url", {"tool": self.TOOL_EDITOR}):
            try:
                resp = self._client.post(
                    f"{base}/upload-url",
                    json={"filename": reference.filename, "content_type": reference.content_type},
                )
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout("申请上传地址超时。") from exc
            except httpx.HTTPError as exc:
                raise EgressUnavailable(f"申请上传地址网络错误（出口 {self._label}）：{exc!r}") from exc
            payload = self._decode(resp, stage="申请上传地址")
            if payload.get("error"):
                raise self._from_error_payload(payload, resp)
            if resp.status_code >= 400:
                raise self._from_error_payload(payload, resp)
            upload_url = payload.get("uploadUrl")
            public_url = payload.get("publicUrl")
            if not upload_url or not public_url:
                raise UpstreamError(
                    "上传地址响应缺 uploadUrl/publicUrl。",
                    detail={"status_code": resp.status_code, "payload": payload},
                )

        with upstream_span("upstream.i2i.put", {"bytes": len(reference.data)}):
            try:
                put_resp = self._client.put(
                    str(upload_url),
                    content=reference.data,
                    headers={"content-type": reference.content_type},
                )
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout("直传参考图到上游存储超时。") from exc
            except httpx.HTTPError as exc:
                raise EgressUnavailable(f"直传参考图网络错误（出口 {self._label}）：{exc!r}") from exc
            if put_resp.status_code >= 400:
                # 直传的是 R2 预签名地址：没有 error 信封，状态码即事实。
                # 额度未消耗（建任务还没发生）⇒ 重试整段三步流是安全的。
                raise UpstreamError(
                    f"参考图直传失败：R2 返回 {put_resp.status_code}。",
                    detail={"status_code": put_resp.status_code},
                )

        with upstream_span("upstream.i2i.create", {"public_url": public_url}):
            try:
                resp = self._client.post(
                    base,
                    json={
                        "image_url": str(public_url),
                        "prompt": prompt,
                        "turnstile_token": token,
                    },
                )
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(
                    "图生图建任务超时。任务**可能**已在跑，但我们没拿到 taskId。"
                ) from exc
            except httpx.HTTPError as exc:
                raise EgressUnavailable(f"图生图建任务网络错误（出口 {self._label}）：{exc!r}") from exc
            payload = self._decode(resp, stage="图生图建任务")
            if payload.get("error"):
                raise self._from_error_payload(payload, resp)
            if resp.status_code >= 400:
                raise self._from_error_payload(payload, resp)
            task_id = payload.get("taskId")
            if not task_id:
                raise UpstreamError(
                    "图生图建任务没有回 taskId。",
                    detail={"status_code": resp.status_code, "payload": payload},
                )
            return str(task_id)

    # ------------------------------------------------------------------ 查询
    def fetch_status(self, task_id: str, *, tool: str | None = None) -> UpstreamStatus:
        """查询任务状态（**零额度**的只读动作）。

        ``tool`` 传工具名（如 ``"ai-photo-editor"``）⇒ 查 `/api/<tool>/status`；
        缺省查生成链路的 `/api/generate/status`。两边的响应同形
        （`{status, progress, image}`，判据同样是 `completed && image`，§10）。
        """
        path = f"/api/{tool}/status" if tool else "/api/generate/status"
        with upstream_span("upstream.status", {"task_id": task_id, "tool": tool or "generate"}):
            try:
                resp = self._client.get(path, params={"taskId": task_id})
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(f"查询上游超时（>{self._settings.upstream_timeout}s）。") from exc
            except httpx.HTTPError as exc:
                raise EgressUnavailable(f"查询上游网络错误（出口 {self._label}）：{exc!r}") from exc

            payload = self._decode(resp, stage="查询")
            if resp.status_code >= 400:
                raise self._from_error_payload(payload, resp)

            if payload.get("error"):
                raise self._from_error_payload(payload, resp)

            return UpstreamStatus(
                raw_status=str(payload.get("status") or "unknown"),
                image=payload.get("image") or None,
                progress=payload.get("progress"),
                message=payload.get("message") or None,
            )

    # ------------------------------------------------------------------ 诊断
    def fetch_geo(self) -> dict[str, Any]:
        """`GET /api/geo` —— **零额度**的只读端点，用来验证出口是否打得通。

        上游用它决定是否显示中国区横幅（见 `docs/UPSTREAM.md` §4），
        与生成链路无关；本服务只用它做**出口连通性自检**
        （`scripts/probe.py egress`），因此不做错误映射。
        """
        with upstream_span("upstream.geo", {"egress": self._label}):
            resp = self._client.get("/api/geo")
            resp.raise_for_status()
            return dict(resp.json())

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _decode(resp: httpx.Response, *, stage: str) -> dict[str, Any]:
        """把响应体解成 dict。非 JSON（WAF/HTML/空）⇒ `upstream_error`。"""
        text = resp.text or ""
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            hint = "（疑似 Cloudflare 拦截页）" if "<html" in text.lower() else ""
            raise UpstreamError(
                f"上游{stage}返回的不是 JSON{hint}。",
                detail={"status_code": resp.status_code, "body_head": text[:400]},
            ) from None
        if not isinstance(data, dict):
            raise UpstreamError(
                f"上游{stage}返回的 JSON 不是对象（收到 {type(data).__name__}）。",
                detail={"status_code": resp.status_code, "payload": data},
            )
        return data

    def _from_error_payload(self, payload: dict[str, Any], resp: httpx.Response) -> AdapterError:
        """把上游的错误信封翻成我们的异常。这是本文件的**唯一**错误出口。"""
        code = str(payload.get("errorCode") or "").upper()
        message = str(payload.get("error") or payload.get("message") or "上游拒绝了请求")
        blob = f"{code} {message}".lower()

        cls = _ERROR_CODE_MAP.get(code)
        if cls is not None:
            # 🔴 上游在 429 上给 `Retry-After: 7200`，但**实测不符**：
            # 在途任务一结束名额立刻恢复（见 docs/UPSTREAM.md §2.2.2）
            # ⇒ 如实上报这个值，但内部退避策略**不**据此长等。
            return cls(
                message,
                retry_after=self._retry_after(resp),
                detail={"upstream_error_code": code},
            )

        if any(hint in blob for hint in _TURNSTILE_HINTS):
            return TurnstileRequired(
                f"上游要求通过人机校验（Turnstile）：{message}。"
                "这是**部署问题** —— 请配置 IMAGEFREE_TURNSTILE_TOKEN。",
                detail={"upstream_error_code": code},
            )
        if any(hint in blob for hint in _POLICY_HINTS):
            return ContentPolicyError(message, detail={"upstream_error_code": code})

        if resp.status_code == 429:
            retry_after = self._retry_after(resp)
            return UpstreamRateLimited(message, retry_after=retry_after)
        if resp.status_code >= 500:
            return UpstreamError(message, detail={"status_code": resp.status_code})
        return UpstreamError(
            f"上游返回 {resp.status_code}：{message}",
            detail={"status_code": resp.status_code, "upstream_error_code": code or None},
        )

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        """**只在响应头真的给了**的时候返回 —— 编一个数字等于伪造事实。"""
        raw = resp.headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None


def describe_capability() -> dict[str, Any]:
    """给运维端点用的上游能力摘要（不含请求）。"""
    return {
        "aspect_ratios": list(models.ASPECT_RATIOS),
        "default_aspect_ratio": models.DEFAULT_ASPECT_RATIO,
        "max_images_per_request": 1,
        "accepts_reference_images": True,
    }


__all__ = [
    "BROWSER_ID_COOKIE",
    "ImageFreeClient",
    "UpstreamStatus",
    "describe_capability",
]
