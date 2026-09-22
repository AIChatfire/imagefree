#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出口池：**每个出口一套独立身份**。

## 为什么 imagefree 必须"一个出口一整套身份"，而不是"换个 IP 就行"

上游的限流是**双层**的（`docs/UPSTREAM.md` §2.2）：

| 维度 | 错误码 | 换 IP 有用吗 |
|---|---|---|
| IP | `FREE_TASK_IP_ACTIVE` | ✅ 正是它 |
| 浏览器身份（cookie） | `FREE_GENERATION_ACTIVE` / `FREE_TASK_BROWSER_ACTIVE` | ❌ **没用** |

所以"只加代理"是不够的：如果所有出口共用一个 `imagefree_free_generation_id`，
浏览器那一层的墙照样挡住你。本模块的做法是**每个出口一个 `httpx.Client`**：

  · 每个 client 有**自己的 cookie jar** ⇒ 上游给每个出口单独下发一个浏览器身份；
  · 每个 client 走**自己的代理** ⇒ 出口 IP 独立。

⇒ 出口池的规模 = **上限**（IP 维度与浏览器维度同时被绕过），
但真要让它们并行，得显式把 `IF_CONCURRENCY` 提到不超过池子的大小 ——
见 `app/coordinator.py` 里"容量 = min(IF_CONCURRENCY, 池子大小)"。

## 三条纪律

1. **一个任务固定一个出口**：任务提交时选定出口并**落库**，之后所有轮询都走同一个出口
   —— 因为那个浏览器身份就住在那个 client 的 cookie jar 里。
2. **墙上撞了就换出口，但只在"没建成任务"时换**：`FREE_*_TASK_ACTIVE` 意味着上游
   什么都没创建 ⇒ 换一个出口重试**不消耗任何额度**。而**超时**是"可能已经建了"
   ⇒ 绝不换出口重发（会真的建出两条任务）。
3. **凭据在观测面上要打码**：`/stats` 与日志里只出现 `***`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from .config import Settings
from .store import utcnow
from .upstream import ImageFreeClient

#: httpx 支持的代理 scheme（`socks*` 需要 `httpx[socks]`，已钉在 requirements.txt）。
SUPPORTED_SCHEMES: tuple[str, ...] = ("http", "https", "socks5", "socks5h")

#: 直连出口的标签（池子为空时唯一存在的那一个）。
DIRECT_LABEL = "direct"

#: 池子扇出的上限：每个出口要一个**独立客户端**（独立连接池 + cookie jar）。
#: 不设上限的话一行配置就能造出几百个客户端；而 16 个出口 × 3 已远超单进程的处理能力。
MAX_POOL_FANOUT = 16

_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class EgressConfigError(ValueError):
    """代理配置写错了 —— **启动期**响亮失败，绝不静默忽略。

    静默忽略的后果是"看着配了却什么都没发生"：调用方以为在轮换出口，
    其实全部流量还在同一个 IP 上撞墙。
    """


@dataclass(frozen=True)
class Egress:
    """一个出口 = 一个标签 + 一个代理（`None` 表示直连）。"""

    label: str
    #: 代理 URL，或 None（直连）。**可能含凭据** ⇒ 对外只用 `masked()`。
    proxy: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.proxy is None

    def masked(self) -> str | None:
        """给观测面用的代理地址：**凭据打码**。"""
        if self.proxy is None:
            return None
        split = urlsplit(self.proxy)
        if not split.username and not split.password:
            return self.proxy
        host = split.hostname or ""
        if split.port:
            host = f"{host}:{split.port}"
        return urlunsplit((split.scheme, f"***:***@{host}", split.path, "", ""))


def parse_egress_specs(raw: str) -> tuple[Egress, ...]:
    """把 `IMAGEFREE_PROXIES` 解析成出口清单。

    支持的写法（逗号分隔）：

        http://127.0.0.1:7890
        cn=http://user:pass@10.0.0.1:8080,us=socks5://10.0.0.2:1080

    留空 ⇒ 只有直连出口（`direct`）—— 与加代理之前的行为**逐字一致**。
    """
    entries = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not entries:
        return (Egress(label=DIRECT_LABEL, proxy=None),)

    egresses: list[Egress] = []
    seen_labels: set[str] = set()
    seen_proxies: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        label, proxy = _split_label(entry, index)
        if label in seen_labels:
            raise EgressConfigError(f"出口标签重复：'{label}'（标签必须唯一，否则绑定时会串）")
        scheme = urlsplit(proxy).scheme.lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise EgressConfigError(
                f"出口 '{label}' 的代理 scheme 不支持：{scheme!r}。"
                f"支持：{', '.join(SUPPORTED_SCHEMES)}（socks* 需要 httpx[socks]）。"
            )
        if not urlsplit(proxy).hostname:
            raise EgressConfigError(f"出口 '{label}' 的代理地址缺少主机名：{proxy!r}")
        if proxy in seen_proxies:
            # 同一个代理写两遍 = 以为有两个出口、其实只有一个 ⇒ 必须说出来。
            raise EgressConfigError(
                f"出口 '{label}' 与前面的出口指向同一个代理地址（{Egress(label, proxy).masked()}）。"
                "同一个出口写两遍不会变成两个 IP。"
            )
        seen_labels.add(label)
        seen_proxies.add(proxy)
        egresses.append(Egress(label=label, proxy=proxy))
    return tuple(egresses)


def fanout_specs(specs: tuple[Egress, ...], times: int) -> tuple[Egress, ...]:
    """把每个**代理**出口复制成 `times` 份（`IMAGEFREE_POOL_FANOUT`）—— **扩容量**。

    🔴 与"同一个出口写两遍"（`parse_egress_specs` 拒绝的配置错误）**不是一回事**：
    轮换型池子每 TCP 连接换一个出口 IP ⇒ 复制出来的每个出口都会真的拿到不同的 IP，
    因此能**并发**跑（上游按 IP 记在途名额，单 IP 上限 3，见 docs/UPSTREAM.md §9）。
    而"同一个固定 IP 的代理写两遍"是纯重复计数 —— 那种写法仍然被拒绝。

    直连**不参与**复制：它只有一个 IP，复制只会让同一个名额被重复计数。
    `times <= 1` 时原样返回（标签都不动）—— 默认路径与加这个能力之前**逐字一致**。
    """
    if times <= 1:
        return specs
    if times > MAX_POOL_FANOUT:
        raise EgressConfigError(
            f"IMAGEFREE_POOL_FANOUT={times} 超过上限 {MAX_POOL_FANOUT}。"
            "每个出口要一个独立客户端（独立连接池 + cookie jar）—— 出口开到几十个已经不是"
            "「加容量」，而是把同一批出口 IP 重复计数。想再提吞吐请加真实出口，或换上游。"
        )
    expanded: list[Egress] = []
    for egress in specs:
        if egress.is_direct:
            expanded.append(egress)
            continue
        expanded.extend(
            Egress(label=f"{egress.label}-{index}", proxy=egress.proxy)
            for index in range(1, times + 1)
        )
    return tuple(expanded)


def _split_label(entry: str, index: int) -> tuple[str, str]:
    """`name=http://…` → (name, url)；没有 `name=` 前缀就自动编号。"""
    if "=" in entry and "://" not in entry.split("=", 1)[0]:
        label, _, proxy = entry.partition("=")
        label, proxy = label.strip(), proxy.strip()
        if not _LABEL_RE.match(label):
            raise EgressConfigError(
                f"出口标签不合法：{label!r}（只允许字母/数字/下划线/点/连字符，最长 32）"
            )
        if not proxy:
            raise EgressConfigError(f"出口 '{label}' 只写了标签、没写代理地址")
        return label, proxy
    if not entry:
        raise EgressConfigError("空的代理条目")
    return f"proxy{index}", entry


class EgressPool:
    """一组出口，每个出口一个独立客户端（独立 cookie jar ⇒ 独立浏览器身份）。

    ⚠️ 进程内持有，生命周期与 app 一致；`close()` 必须被调用（否则连接不释放）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transports: dict[str, Any] | None = None,
        clients: dict[str, ImageFreeClient] | None = None,
        egresses: tuple[Egress, ...] | None = None,
    ) -> None:
        self._settings = settings
        self._cooldown_seconds = float(settings.imagefree_proxy_cooldown)
        if egresses is not None:
            # 显式给出口清单（注入假上游的测试、或宿主自己拼的池）⇒ 不再解析配置。
            self._egresses = tuple(egresses)
        else:
            specs = parse_egress_specs(settings.imagefree_proxies)
            if settings.imagefree_use_direct and not any(e.is_direct for e in specs):
                # 显式开了直连才把它排到**最前面**（默认不开：上游按 IP 记账，直连烧的
                # 是本机出口 IP 的额度）。顺序即优先级，不需要额外分支。
                specs = (Egress(label=DIRECT_LABEL, proxy=None), *specs)
            # 池子扇出：**直连插完之后**才展开（展开会跳过 direct）。
            specs = fanout_specs(specs, settings.imagefree_pool_fanout)
            self._egresses = specs
        self._cooldowns: dict[str, datetime] = {}
        self._clients: dict[str, ImageFreeClient] = {}
        for egress in self._egresses:
            injected = (clients or {}).get(egress.label)
            self._clients[egress.label] = injected or ImageFreeClient(
                settings,
                proxy=egress.proxy,
                label=egress.label,
                transport=(transports or {}).get(egress.label),
            )

    # ------------------------------------------------------------------ 构造
    @classmethod
    def for_single_client(cls, settings: Settings, client: ImageFreeClient) -> EgressPool:
        """只用一个**已建好**的客户端当唯一出口（测试注入假上游时用）。

        刻意**不解析** `IMAGEFREE_PROXIES`：否则注入的 client 会与配置里的出口标签对不上，
        池子会去另建一个真客户端 ⇒ 测试就不是零出网了。
        """
        return cls(
            settings,
            clients={DIRECT_LABEL: client},
            egresses=(Egress(label=DIRECT_LABEL, proxy=None),),
        )

    # ------------------------------------------------------------------ 基本信息
    @property
    def egresses(self) -> tuple[Egress, ...]:
        return self._egresses

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(e.label for e in self._egresses)

    def __len__(self) -> int:
        return len(self._egresses)

    @property
    def is_rotating(self) -> bool:
        """是否真的配了代理（>1 个出口，或唯一的出口不是直连）。"""
        return not (len(self._egresses) == 1 and self._egresses[0].is_direct)

    def client(self, label: str) -> ImageFreeClient:
        try:
            return self._clients[label]
        except KeyError:
            # 配置被改小之后，旧任务身上可能还挂着已不存在的出口标签。
            # 查询接口不需要身份 cookie（实测），所以退回直连是安全的；
            # 但**必须留痕**，否则"轮询悄悄换了出口"这件事没人知道。
            logger.warning("出口 '{}' 已不在配置里 ⇒ 该任务的轮询退回直连。", label)
            return self._clients[self._egresses[0].label]

    def browser_ids(self) -> dict[str, str | None]:
        return {label: client.browser_id for label, client in self._clients.items()}

    # ------------------------------------------------------------------ 选出口
    def available_egresses(
        self,
        *,
        in_flight_by_label: dict[str, int],
        per_egress_limit: int,
        now: datetime | None = None,
    ) -> list[Egress]:
        """返回**还有名额**的出口，**按配置顺序**（= 优先级）。

        名额按 IP 记：上游实测「同一 IP 同时最多 3 个在途任务」
        （docs/UPSTREAM.md §9）⇒ 一个出口能同时跑 `per_egress_limit` 个。
        🔴 **刻意不按剩余名额排序**：顺序即策略 —— 配置里直连在第一位，
        于是「直连优先、满了才溢出到代理池」是由**顺序**保证的。
        """
        stamp = now or utcnow()
        limit = max(1, per_egress_limit)
        available: list[Egress] = []
        for egress in self._egresses:
            if self.is_cooling(egress.label, now=stamp):
                continue
            if limit - in_flight_by_label.get(egress.label, 0) <= 0:
                continue
            available.append(egress)
        return available

    # ------------------------------------------------------------------ 冷却
    def note_wall(self, label: str, *, now: datetime | None = None) -> None:
        """某个出口撞上"在途互斥"的墙 ⇒ 冷置它，让后来的任务优先换别的出口。

        ⚠️ **只有一个出口时不冷置**：没有别的出口可换，冷置等于把唯一的路也堵上
        （用更长的死等换掉退避逻辑）。此时交给协调器的退避就够了。
        """
        if self._cooldown_seconds <= 0:
            return
        if len(self._egresses) <= 1:
            logger.debug("只有一个出口 '{}' ⇒ 不冷置（换无可换，仍走退避重试）。", label)
            return
        stamp = now or utcnow()
        self._cooldowns[label] = stamp + timedelta(seconds=self._cooldown_seconds)
        logger.warning("出口 '{}' 撞到上游在途互斥 ⇒ 冷却 {}s，优先换出口。", label, self._cooldown_seconds)

    def note_unavailable(self, label: str, *, now: datetime | None = None) -> None:
        """某个出口**连不上**（代理拒绝认证 / DNS / 连接被拒）⇒ 冷置它。

        与 `note_wall` 分开**只为日志语义准确**：一个是「这条通道的配额满了」，
        另一个是「这条通道坏了」；冷置时长共用 `IMAGEFREE_PROXY_COOLDOWN`。
        调用方（协调器）已经保证「还有别的出口可换」才会走到这里。
        """
        if self._cooldown_seconds <= 0:
            return
        stamp = now or utcnow()
        self._cooldowns[label] = stamp + timedelta(seconds=self._cooldown_seconds)
        logger.warning("出口 '{}' 连不上 ⇒ 冷置 {}s（这条通道暂时不可用）。", label, self._cooldown_seconds)

    def is_cooling(self, label: str, *, now: datetime | None = None) -> bool:
        until = self._cooldowns.get(label)
        if until is None:
            return False
        return (now or utcnow()) < until

    def cooldown_until(self, label: str) -> datetime | None:
        return self._cooldowns.get(label)

    # ------------------------------------------------------------------ 观测
    def state(self, *, in_flight_by_label: dict[str, int], now: datetime | None = None) -> list[dict[str, Any]]:
        """`/stats` 与 `/capabilities` 用的出口快照（**代理凭据打码**）。"""
        stamp = now or utcnow()
        out: list[dict[str, Any]] = []
        for egress in self._egresses:
            until = self._cooldowns.get(egress.label)
            out.append(
                {
                    "label": egress.label,
                    "proxy": egress.masked(),
                    "direct": egress.is_direct,
                    "in_flight": in_flight_by_label.get(egress.label, 0),
                    "cooling": self.is_cooling(egress.label, now=stamp),
                    "cooldown_until": until.isoformat() if until and until > stamp else None,
                    "browser_id_held": self._clients[egress.label].browser_id is not None,
                }
            )
        return out

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        for client in self._clients.values():
            client.close()


__all__ = [
    "DIRECT_LABEL",
    "MAX_POOL_FANOUT",
    "SUPPORTED_SCHEMES",
    "Egress",
    "EgressConfigError",
    "EgressPool",
    "fanout_specs",
    "parse_egress_specs",
]
