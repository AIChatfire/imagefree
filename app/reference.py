#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图生图的「代取参考图」：调用方给 URL / data URI，本模块负责取回字节。

🔴 纪律（docs/UPSTREAM.md §10 末尾引用的 user-controlled-egress-guard）：
调用方给的地址是**用户可控出站目标**，三条硬规则：

  1. **拒绝内网/保留地址**（loopback / private / link-local / reserved）——
     否则本服务会被当成内网跳板（SSRF）；
  2. **体积上限**（默认 10MB，与上游 `/api/ai-photo-editor` 的上传限额一致）：
     Content-Length 超限直接拒，流式读取超限中途掐断；
  3. **超时收紧**（默认 30s）+ **手动跟 ≤3 跳重定向**，每一跳都重新过一遍规则 1。

data URI 不出网：`data:image/…;base64,` 直接解码，同样受限额约束。

失败一律抛 `InvalidImageError`（任务终态 failure）—— 这是**调用方数据问题**，
换出口/重试都无意义。
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import cv2
import httpx
import numpy as np

from .errors import InvalidImageError

#: 允许的协议。file:/ftp: 之类一律拒绝。
_ALLOWED_SCHEMES = ("http", "https")
#: 重定向跟随上限。每一跳的目标都会重新过一遍内网检查。
_MAX_REDIRECTS = 3
#: 按 Content-Type 推断扩展名（够用即可，上游只看 filename 的可读性）。
_EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@dataclass(frozen=True)
class ReferenceImage:
    """取回的参考图。`filename`/`content_type` 用于 upload-url 请求体。"""

    data: bytes
    filename: str
    content_type: str


def _reject(reason: str) -> InvalidImageError:
    return InvalidImageError(f"参考图不合格：{reason}")


def _validate_decodable(data: bytes) -> None:
    """字节必须是**可解码的位图**（PNG/JPEG/WebP/GIF…）。

    0 字节、截断文件、伪图片（魔数对但内容坏）都在这一层拦下 ——
    与其把废图传完三步流再被上游拒，不如在代取层就省掉那次往返。
    SVG 等矢量格式 `imdecode` 解不出 ⇒ 一并拒绝（上游编辑器也不收）。
    """
    if len(data) == 0:
        raise _reject("空文件（0 字节）")
    if cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) is None:
        raise _reject("无法解码为位图图像（支持 PNG/JPEG/WebP；GIF/SVG 校验不了，不往上游送）")


def _check_host_public(url: str, *, resolve: bool = True) -> None:
    """校验 URL 主机不在内网/保留段。

    ``resolve=False``（测试注入了假传输层）时跳过 DNS 解析，只查**字面 IP**——
    防护语义在真实部署（resolve=True）下完整；测试环境没有 DNS，也不能让它真解析。
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise _reject("URL 没有主机名")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise _reject(f"拒绝指向内网/保留地址的图片 URL（{host}）")
        return
    if not resolve:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise _reject(f"主机 {host} 无法解析（{exc!r}）") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise _reject(f"拒绝指向内网/保留地址的图片 URL（{host} → {ip}）")


def _from_data_uri(ref: str, max_bytes: int) -> ReferenceImage:
    head, sep, payload = ref.partition(",")
    if not sep or not head.startswith("data:image/") or "base64" not in head:
        raise _reject("data URI 只支持 `data:image/<type>;base64,<payload>` 形态")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _reject(f"data URI base64 解码失败（{exc!r}）") from exc
    if len(data) > max_bytes:
        raise _reject(f"{len(data)} 字节超过上限 {max_bytes}")
    _validate_decodable(data)
    subtype = head[len("data:image/"):].split(";", 1)[0] or "png"
    content_type = f"image/{subtype}"
    return ReferenceImage(
        data=data,
        filename=f"reference.{_EXT_BY_MIME.get(content_type, subtype or 'bin')}",
        content_type=content_type,
    )


def _from_http(
    ref: str,
    *,
    max_bytes: int,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> ReferenceImage:
    url = ref
    resolve = transport is None  # 注入了假传输层（测试）⇒ 无法也不必做真 DNS。
    # 手动跟跳：每一跳都重过内网检查（follow_redirects=True 会跳过这层防护）。
    for _hop in range(_MAX_REDIRECTS + 1):
        scheme = urlparse(url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise _reject(f"只允许 http(s) URL（收到 {scheme or '空'} 协议）")
        _check_host_public(url, resolve=resolve)
        # 流式读取 + 途中限额：不把"声称 1 字节、实发 1GB"的服务器整个吞进内存。
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            try:
                with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise _reject("重定向响应缺 Location 头")
                        url = str(resp.next_request.url) if resp.next_request else location
                        continue
                    if resp.status_code != 200:
                        raise _reject(f"下载返回 HTTP {resp.status_code}")
                    content_type = (resp.headers.get("content-type") or "").split(";", 1)[
                        0
                    ].strip().lower()
                    if not content_type.startswith("image/"):
                        raise _reject(f"Content-Type 是 {content_type or '空'}，不是 image/*")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise _reject(f"Content-Length {declared} 字节超过上限 {max_bytes}")
                    chunks: list[bytes] = []
                    received = 0
                    try:
                        for chunk in resp.iter_raw():
                            received += len(chunk)
                            if received > max_bytes:
                                raise _reject(f"下载到 {received} 字节时超过上限 {max_bytes}")
                            chunks.append(chunk)
                    except httpx.StreamConsumed:
                        # MockTransport（测试）的响应体构造时已在内存，iter_raw 不可复用；
                        # 真实网络响应不会走这条回退。整读后仍要过限额。
                        chunks = [resp.read()]
                    data = b"".join(chunks)
                    if len(data) > max_bytes:
                        raise _reject(f"{len(data)} 字节超过上限 {max_bytes}")
                    _validate_decodable(data)
            except httpx.TimeoutException as exc:
                raise _reject(f"下载超时（>{timeout}s）") from exc
            except httpx.HTTPError as exc:
                raise _reject(f"下载失败（{exc!r}）") from exc
        path = urlparse(url).path
        stem = path.rsplit("/", 1)[-1] if "/" in path else ""
        ext = _EXT_BY_MIME.get(content_type, "bin")
        filename = stem if ("." in stem and not stem.startswith(".")) else f"reference.{ext}"
        return ReferenceImage(data=data, filename=filename, content_type=content_type)
    else:
        raise _reject(f"重定向超过 {_MAX_REDIRECTS} 跳")


def load_reference(
    ref: str,
    *,
    max_bytes: int,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> ReferenceImage:
    """参考图入口：data URI 走本地解码，http(s) 走带防护的代取。"""
    limit = int(max_bytes)
    if limit <= 0:
        raise _reject(f"体积上限配置非法（{max_bytes}MB）")
    if ref.startswith("data:"):
        return _from_data_uri(ref, limit)
    return _from_http(ref, max_bytes=limit, timeout=timeout, transport=transport)


__all__ = ["ReferenceImage", "load_reference"]
