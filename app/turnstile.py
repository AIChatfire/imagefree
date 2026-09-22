#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turnstile token 的供给：**现铸现用**。

站点的**主生成端点**不校验 Turnstile，但**工具端点**（`/api/ai-photo-editor`、
`/api/image-upscaler` 等）强制校验（实测 `400 Human verification failed`，见
`docs/UPSTREAM.md` §10.1）⇒ 图生图/放大**必须**带一个有效 token。

🔴 为什么不能"启动时注入一个静态值"：token **只有几分钟有效期**
（实测：真 Chrome 铸出 816 字符，几分钟后失效）⇒ 静态注入的部署在重启几分钟后就全废。
所以生产形态是**按需铸造**：

    IMAGEFREE_TURNSTILE_MINT_CMD='node scripts/mint_turnstile.cjs --headless --json'

stdout 取 token（支持 JSON 的 `.token` 字段，或整行就是 token）。铸不出来 ⇒ 抛
`TurnstileRequired`（**503**：这是部署问题，不是调用方参数写错 —— 别让它伪装成 4xx）。

⚠️ **TZ 必须与出口 IP 地理一致**（技能档定案的头号坑）：缺 TZ ⇒ Chrome 跑 UTC
⇒ CF 判时区与 IP 不一致 ⇒ 挑战升级为交互式 ⇒ 无头环境铸不出来。Linux 上本服务不做兜底，
由铸造脚本 fail-fast（`docs/DEPLOY.md` §3）。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time

from loguru import logger

from .config import Settings
from .errors import TurnstileMintFailed, TurnstileRequired

#: 铸造命令允许的输出上限（防呆：脚本如果把整页 HTML 打出来，别把内存吃掉）。
_MAX_STDOUT_CHARS = 8192


def parse_mint_output(stdout: str) -> str | None:
    """从铸造命令的 stdout 里取 token。

    只认**最后一行**（脚本可能带诊断输出），支持两种形态：
      · `{"token": "1.abc…"}`（`--json` 模式）
      · 裸 token 一行
    """
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1][:_MAX_STDOUT_CHARS]
    if last.startswith("{"):
        try:
            payload = json.loads(last)
        except json.JSONDecodeError:
            return None
        token = payload.get("token")
        return str(token) if token else None
    return last


def _resolve_argv(cmd: str) -> list[str]:
    argv = shlex.split(cmd)
    if not argv:
        raise TurnstileRequired("IMAGEFREE_TURNSTILE_MINT_CMD 是空的。")
    program = argv[0]
    # 带路径的（./scripts/x、/usr/bin/x）直接判文件；裸名走 PATH 查找。
    exists = os.path.isfile(program) if "/" in program else bool(shutil.which(program))
    if not exists:
        raise TurnstileRequired(
            f"铸造命令不可执行：{program!r} 不存在（或不在 PATH 上）。"
            "Linux 无头部署需要 node + playwright-core + 一个 Chrome/Chromium，见 docs/DEPLOY.md。"
        )
    return argv


def mint_token(settings: Settings) -> str:
    """按需铸一个 token。失败一律抛 `TurnstileRequired`（503 部署问题）。"""
    cmd = (settings.imagefree_turnstile_mint_cmd or "").strip()
    if not cmd:
        raise TurnstileRequired(
            "工具端点（图生图/放大）强制 Turnstile，但没有可用的 token 来源："
            "既没配 `IMAGEFREE_TURNSTILE_MINT_CMD`（按需铸造），也没配 `IMAGEFREE_TURNSTILE_TOKEN`。"
            "推荐配法（无头 Linux）："
            "IMAGEFREE_TURNSTILE_MINT_CMD='node scripts/mint_turnstile.cjs --headless --json'"
        )

    argv = _resolve_argv(cmd)
    timeout = float(settings.imagefree_turnstile_mint_timeout)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TurnstileMintFailed(
            f"铸造 turnstile token 超时（{timeout:.0f}s）。"
            "无头环境常见原因：**TZ 未设或为 UTC** ⇒ 挑战变交互式（见 docs/DEPLOY.md §3）。"
        ) from exc
    except OSError as exc:  # 命令存在但起不来（权限/缺依赖）
        raise TurnstileMintFailed(f"铸造 turnstile token 无法执行：{exc!r}") from exc

    elapsed = time.monotonic() - started
    token = parse_mint_output(proc.stdout or "")
    if proc.returncode != 0 or not token:
        tail = " / ".join((proc.stderr or "").strip().splitlines()[-2:])[:300]
        raise TurnstileMintFailed(
            f"铸造 turnstile token 失败（退出码 {proc.returncode}，{elapsed:.1f}s）。"
            f"铸造脚本的最后输出：{tail or '(空)'}"
        )
    # 🔴 绝不打印 token 本身 —— 只报长度与耗时（日志会被收集/转发）。
    logger.info("已铸造 turnstile token（{} 字符，{:.1f}s）", len(token), elapsed)
    return token


def token_for_tool_submit(settings: Settings) -> str | None:
    """工具端点提交用的 token：**优先现铸**；没配铸造命令时退回静态配置值。"""
    if (settings.imagefree_turnstile_mint_cmd or "").strip():
        return mint_token(settings)
    return settings.imagefree_turnstile_token or None


__all__ = ["mint_token", "parse_mint_output", "token_for_tool_submit"]
