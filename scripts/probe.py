#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游探针：**默认零消耗**，只有显式开闸才会提交真实任务。

用法：

    # 1) 只读（零额度）：查一个已有任务的走查状态（图生图任务加 --tool）
    python scripts/probe.py status <taskId>
    python scripts/probe.py status <taskId> --tool ai-photo-editor

    # 2) 只读（零额度）：上游是否认为我们在中国区（决定横幅，不决定限流）
    python scripts/probe.py geo

    # 3) 只读（零额度）：逐个出口自检 —— 站点是否可达 + 真实出口 IP 是什么
    python scripts/probe.py egress --echo-ip --repeat 3

    # 3.5) 🔴 跑站点另外三个工具（**消耗免费额度**）：上传 → 建任务 → 轮询 → 量产物尺寸
    python scripts/probe.py tool image-upscaler --image bench/live-4x3-toy-boat.png \
        --i-know-this-consumes-quota

    # 4) 🔴 提交真实任务（**消耗上游免费额度**）
    python scripts/probe.py generate --prompt "cat" --i-know-this-consumes-quota

纪律（与 ../hailuo、../jimeng 一致）：
  · 第 4 条**必须**带 `--i-know-this-consumes-quota`，否则脚本拒绝执行；
  · 脚本自身**不带轮询循环** —— 出图与否是协调器的事，探针只回答"上游怎么说"。

出口配置从 `.env` / 环境变量读（`IMAGEFREE_PROXIES`）。
`--echo-ip` 会额外访问 `api.ipify.org` 显示**真实出口 IP** —— 这是验证"代理到底有没有
换 IP"的唯一手段（`/api/geo` 只回 isChina，看不出 IP）。它同样不碰生成链路、不消耗额度。

⚠️ 本机系统代理（macOS `scutil` 那套）会接管直连流量。给了显式代理时本脚本一律
   `trust_env=False`，免得"配的代理"与"系统代理"叠成两层、出口变得不可预期。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import pathlib
import struct
import sys
import time

import httpx

# 让脚本能 import 到 app/（以 `python scripts/probe.py` 运行时 sys.path[0] 是 scripts/）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

DEFAULT_BASE = "https://imagefree.net"
#: 出口 IP 回显服务。**只在 `--echo-ip` 时访问**，用来验证代理真的换了 IP。
IP_ECHO_URL = "https://api.ipify.org?format=json"

_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/json",
    "origin": DEFAULT_BASE,
    "referer": f"{DEFAULT_BASE}/zh",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}


def _client(base_url: str, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=base_url.rstrip("/"), headers=_HEADERS, timeout=timeout)


def cmd_status(args: argparse.Namespace) -> int:
    """查任务状态（**零额度**）。`--tool` 指定工具端点（图生图/放大任务用）。"""
    path = f"/api/{args.tool}/status" if getattr(args, "tool", "") else "/api/generate/status"
    with _client(args.base_url, args.timeout) as client:
        resp = client.get(path, params={"taskId": args.task_id})
    print(f"HTTP {resp.status_code}（{path}）")
    print(resp.text[:2000])
    return 0 if resp.status_code == 200 else 1


def cmd_geo(args: argparse.Namespace) -> int:
    with _client(args.base_url, args.timeout) as client:
        resp = client.get("/api/geo")
    print(f"HTTP {resp.status_code}")
    print(resp.text[:2000])
    return 0 if resp.status_code == 200 else 1


def _echo_exit_ip(proxy: str | None, *, timeout: float) -> str:
    """回显**出口 IP**。显式给代理时关掉环境代理解析（否则本机系统代理会插手）。"""
    with httpx.Client(proxy=proxy, trust_env=proxy is None, timeout=timeout) as client:
        resp = client.get(IP_ECHO_URL)
        resp.raise_for_status()
        return str(resp.json().get("ip"))


def cmd_egress(args: argparse.Namespace) -> int:
    """逐个出口做**零额度**自检：站点可达性（`/api/geo`）+ 出口 IP（`--echo-ip`）。"""
    from app.config import get_settings
    from app.egress import EgressPool

    try:
        pool = EgressPool(get_settings())
    except Exception as exc:  # noqa: BLE001 - CLI 要把配置错原样说清楚，而不是回栈
        print(f"出口配置有问题：{exc}", file=sys.stderr)
        return 2

    print(f"出口数：{len(pool)}；轮换：{'是' if pool.is_rotating else '否（直连）'}")
    failures = 0
    try:
        for egress in pool.egresses:
            client = pool.client(egress.label)
            print(f"\n---- 出口 {egress.label}（{egress.masked() or '直连'}）----")
            seen: list[str] = []
            for round_index in range(1, max(1, args.repeat) + 1):
                try:
                    geo = client.fetch_geo()
                except Exception as exc:  # noqa: BLE001 - 探针就是要把失败原因打出来
                    print(f"  第 {round_index} 轮：站点不可达 —— {exc!r}")
                    failures += 1
                    continue
                line = f"  第 {round_index} 轮：/api/geo → {json.dumps(geo, ensure_ascii=False)}"
                if args.echo_ip:
                    try:
                        ip = _echo_exit_ip(egress.proxy, timeout=args.timeout)
                        seen.append(ip)
                        line += f"；出口 IP = {ip}"
                    except Exception as exc:  # noqa: BLE001 - 回显失败不影响站点可达性结论
                        line += f"；出口 IP 取失败（{exc!r}）"
                print(line)
            if len(set(seen)) > 1:
                print(f"  ⇒ 观察到轮换：{len(set(seen))} 个不同出口 IP / {len(seen)} 次请求")
            elif seen:
                print(f"  ⇒ 出口 IP 恒为 {seen[0]}（该出口不是每次换 IP）")
    finally:
        pool.close()
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 站点的另外三个工具（上传 → 建任务 → 轮询）
# ---------------------------------------------------------------------------

#: 支持的工具路由（与站点 nav 里的名字一致）。
TOOLS = ("image-upscaler", "ai-photo-editor", "background-remover")


def _probe_client(args: argparse.Namespace) -> httpx.Client:
    return _client(args.base_url, args.timeout)


def _upload_image(client: httpx.Client, tool: str, image_path: str, timeout: float) -> tuple[str, dict]:
    """取上传地址 → `PUT` 直传 → 返回 (publicUrl, 原始 upload-url 响应)。"""
    file_path = pathlib.Path(image_path)
    data = file_path.read_bytes()
    content_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
    print(f"  上传：{file_path.name}（{content_type}，{len(data) / 1024:.0f} KB）")
    resp = client.post(
        f"/api/{tool}/upload-url",
        json={"filename": file_path.name, "content_type": content_type},
    )
    payload = resp.json()
    print(f"  upload-url → HTTP {resp.status_code}：{json.dumps(payload, ensure_ascii=False)[:300]}")
    if payload.get("error") or not payload.get("uploadUrl"):
        raise RuntimeError(f"取上传地址失败（{payload.get('errorCode') or resp.status_code}）")
    # PUT 到**存储域名**（不是本站），所以另起一个客户端，别把本站 base_url 带过去。
    with httpx.Client(trust_env=True, timeout=timeout) as put_client:
        put = put_client.put(
            payload["uploadUrl"], content=data, headers={"Content-Type": content_type}
        )
    print(f"  PUT 直传 → HTTP {put.status_code}")
    if put.status_code >= 400:
        raise RuntimeError(f"直传失败：HTTP {put.status_code} {put.text[:200]}")
    return str(payload["publicUrl"]), payload


def _image_size(data: bytes) -> str:
    """从字节里读出像素（PNG / JPEG），不依赖 Pillow。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return f"{w}×{h} PNG"
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return f"{w}×{h} JPEG"
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg
        return "JPEG（未能解析尺寸）"
    return f"未知格式（前 8 字节：{data[:8]!r}）"


def cmd_tool(args: argparse.Namespace) -> int:
    """跑通站点另外三个工具的完整流程，并把产物像素量出来。"""
    if not args.i_know_this_consumes_quota:
        print(
            f"拒绝执行：`/api/{args.tool}` 建任务会**消耗免费额度**。\n"
            f"确需执行请显式追加：python scripts/probe.py tool {args.tool} "
            "--image <本地图片路径> --i-know-this-consumes-quota",
            file=sys.stderr,
        )
        return 2
    if not pathlib.Path(args.image).is_file():
        print(f"找不到图片：{args.image}", file=sys.stderr)
        return 2

    with _probe_client(args) as client:
        try:
            public_url, _ = _upload_image(client, args.tool, args.image, args.timeout)
        except Exception as exc:  # noqa: BLE001 - CLI 要把原因说清楚
            print(f"上传阶段失败：{exc}", file=sys.stderr)
            return 1

        body: dict = {"image_url": public_url, "turnstile_token": args.turnstile_token or None}
        if args.tool == "ai-photo-editor":
            body["prompt"] = args.prompt
        started = time.monotonic()
        resp = client.post(f"/api/{args.tool}", json=body)
        print(f"  建任务 → HTTP {resp.status_code}：{resp.text[:300]}")
        payload = resp.json()
        if payload.get("error") or not payload.get("taskId"):
            print(f"建任务被拒（errorCode={payload.get('errorCode')}）", file=sys.stderr)
            return 1
        task_id = str(payload["taskId"])
        print(f"  taskId = {task_id}")

        # 轮询（站点前端对放大给的是 30 轮 × 5s = 150s 预算，这里放宽到 --watch-timeout）
        deadline = time.monotonic() + args.watch_timeout
        image_url: str | None = None
        while time.monotonic() < deadline:
            time.sleep(args.poll_interval)
            status_resp = client.get(f"/api/{args.tool}/status", params={"taskId": task_id})
            status = status_resp.json()
            elapsed = round(time.monotonic() - started, 1)
            print(f"    [+{elapsed}s] {json.dumps(status, ensure_ascii=False)[:200]}")
            if status.get("error"):
                print("  上游报错，终止。", file=sys.stderr)
                return 1
            if status.get("status") == "completed" and status.get("image"):
                image_url = str(status["image"])
                break
            if status.get("status") == "failed":
                print("  上游判定失败。", file=sys.stderr)
                return 1
        if not image_url:
            print("  超时未出结果。", file=sys.stderr)
            return 1

        print(f"\n  出结果用时 {round(time.monotonic() - started, 1)}s")
        print(f"  产物 URL：{image_url}")
        with httpx.Client(trust_env=True, timeout=args.timeout, follow_redirects=True) as dl:
            got = dl.get(image_url)
        print(f"  下载 → HTTP {got.status_code}，{len(got.content) / 1024:.0f} KB")
        print(f"  🔴 产物真实尺寸：{_image_size(got.content)}")
        out = pathlib.Path(args.out) if args.out else pathlib.Path("/tmp") / f"tool_{args.tool}_{task_id[:8]}.png"
        out.write_bytes(got.content)
        print(f"  已存：{out}")
    return 0

def cmd_generate(args: argparse.Namespace) -> int:
    if not args.i_know_this_consumes_quota:
        print(
            "拒绝执行：提交会**消耗上游免费额度**，并占用该出口 IP 的在途名额。\n"
            "确需执行请显式追加：python scripts/probe.py generate --prompt 'cat' "
            "--i-know-this-consumes-quota",
            file=sys.stderr,
        )
        return 2
    body = {
        "prompt": args.prompt,
        "aspect_ratio": args.aspect_ratio,
        "turnstile_token": args.turnstile_token or None,
    }
    proxy = args.proxy or None
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=_HEADERS,
        timeout=args.timeout,
        proxy=proxy,
        trust_env=proxy is None,
    ) as client:
        resp = client.post("/api/generate", json=body)
        cookies = dict(client.cookies)
    print(f"HTTP {resp.status_code}")
    print(resp.text[:2000])
    if cookies:
        # 把身份 cookie 打出来 —— 这就是 §2.3 里"来源未取证"那一步的直接证据。
        print("---- Set-Cookie 后客户端持有的 cookie ----")
        print(json.dumps(cookies, ensure_ascii=False, indent=2))
    return 0 if resp.status_code < 400 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="imagefree.net 上游探针（默认零消耗）")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="上游基址")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时（秒）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="只读：查任务状态（零额度）")
    p_status.add_argument("task_id")
    p_status.add_argument(
        "--tool",
        default="",
        choices=["", "ai-photo-editor", "image-upscaler", "background-remover"],
        help="工具端点名（查图生图/放大任务时带上；缺省 = 生成链路）",
    )
    p_status.set_defaults(func=cmd_status)

    p_geo = sub.add_parser("geo", help="只读：查地区判定（零额度）")
    p_geo.set_defaults(func=cmd_geo)

    p_egress = sub.add_parser("egress", help="只读：逐个出口自检（零额度）")
    p_egress.add_argument(
        "--echo-ip", action="store_true", help="额外回显真实出口 IP（会访问 api.ipify.org）"
    )
    p_egress.add_argument("--repeat", type=int, default=1, help="每个出口测几轮（轮换池建议 3）")
    p_egress.set_defaults(func=cmd_egress)

    p_tool = sub.add_parser("tool", help="🔴 跑站点另外三个工具（消耗免费额度）")
    p_tool.add_argument("tool", choices=TOOLS)
    p_tool.add_argument("--image", required=True, help="本地图片路径（放大工具限 1MB，编辑器限 10MB）")
    p_tool.add_argument("--prompt", default="make it look like a watercolor painting", help="仅 ai-photo-editor 需要")
    p_tool.add_argument(
        "--turnstile-token",
        default="",
        help="🔴 这三个工具**强制** Turnstile（实测 400）—— 用 /tmp/mint_turnstile.cjs 铸一个带上",
    )
    p_tool.add_argument("--poll-interval", type=float, default=5.0)
    p_tool.add_argument(
        "--watch-timeout", type=float, default=300.0,
        help="轮询预算（秒）。⚠️ 编辑器（ai-photo-editor）实测 pending 10 分钟+ ⇒ 用 3600",
    )
    p_tool.add_argument("--out", default="", help="产物落盘路径（默认 /tmp/tool_<tool>_<id>.png）")
    p_tool.add_argument(
        "--i-know-this-consumes-quota",
        action="store_true",
        help="确认这会**消耗免费额度**（不加则拒绝执行）",
    )
    p_tool.set_defaults(func=cmd_tool)

    p_gen = sub.add_parser("generate", help="🔴 提交真实任务（消耗免费额度）")
    p_gen.add_argument("--prompt", default="cat")
    p_gen.add_argument("--aspect-ratio", default="1:1", choices=["1:1", "3:4", "4:3", "9:16", "16:9"])
    p_gen.add_argument("--turnstile-token", default="", help="上游开关被打开时才需要")
    p_gen.add_argument("--proxy", default="", help="走指定代理提交（默认直连）")
    p_gen.add_argument(
        "--i-know-this-consumes-quota",
        action="store_true",
        help="确认这是**要花额度**的真实提交（不加则拒绝执行）",
    )
    p_gen.set_defaults(func=cmd_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
