#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置：**每个旋钮都有唯一读取点**。

约定（照搬 ../jimeng 与 ../hailuo）：

  · 每条配置都在 `.env.example` 里写了「为什么是这个默认值」；
  · `tests/test_wiring.py::test_every_knob_is_read_somewhere` 会逐条断言
    它**在 app/ 或 gunicorn_conf.py 里真的被读过** —— 拿不出依据的旋钮不存在。

本服务（imagefree）与 hailuo 的关键差别：**上游没有凭据**。
所以没有 `*_TOKEN` 这种硬前提，也没有 `upstream_not_configured` 这个失败态。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部运行期配置。字段名即环境变量名（大写）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- 上游
    #: 上游基址。自建/灰度环境才需要改。
    imagefree_base_url: str = "https://imagefree.net"

    #: 上游浏览器身份 cookie 的值（`imagefree_free_generation_id`）。
    #: 留空 ⇒ 由上游在首次提交时下发，本进程用 cookie jar 自动持有
    #: （实测查询接口不需要它；提交是否需要**未取证**，见 docs/UPSTREAM.md §2.3）。
    imagefree_free_generation_id: str = ""

    #: 上游 Turnstile token。上游当前把开关硬编码成关闭（docs/UPSTREAM.md §5），
    #: 所以默认留空、提交时发 `null`。**开关一旦被打开**，这里必须填上才能继续出图。
    imagefree_turnstile_token: str = ""

    #: **按需铸造** token 的命令（无头 Linux 部署的推荐形态）。
    #: 🔴 为什么不靠上面那个静态值：token **只有几分钟有效期** ⇒ 启动时注入的静态值
    #: 在重启几分钟后就失效，图生图/放大必然报 `upstream_turnstile_required`。
    #: 配了本项 ⇒ 每次工具端点提交前**现铸现用**（实测铸一次约 3~15s）。
    #:   例：`node scripts/mint_turnstile.cjs --headless --json`
    #: stdout 取 token（支持 JSON 的 `.token` 字段，或整行就是 token）。
    #: ⚠️ 铸造环境必须设 **TZ**（与出口 IP 地理一致），否则挑战会升级为交互式 ⇒ 无头必失败。
    imagefree_turnstile_mint_cmd: str = ""

    #: 单次铸造的超时（秒）。无头环境带上浏览器启动，60s 是宽松但安全的预算。
    imagefree_turnstile_mint_timeout: float = 60.0

    #: 单次上游请求的超时（秒）。上游是 Cloudflare 边缘，首字节慢但稳定；
    #: 30s 足够覆盖提交与查询（查询实测亚秒级）。
    upstream_timeout: float = 30.0

    #: **单个出口 IP 上允许同时在跑的任务数** —— 上游实测：**3**。
    #: 第 4 个并发会拿到 429 `FREE_TASK_IP_ACTIVE`（见 docs/UPSTREAM.md §2.2.2）。
    #: 它与 `IF_CONCURRENCY` 共同决定容量：
    #:     有效容量 = min(IF_CONCURRENCY, 出口数 × 本值)
    #: 所以**加出口是真的能扩容量**（这也正是“遇到频控就加代理”的依据）。
    imagefree_ip_concurrency: int = 3

    # ---------------------------------------------------------------- 出口（可选加代理）
    #: 出口清单，逗号分隔。**留空 = 直连**（与加代理之前逐字一致）。
    #: 写法：`http://127.0.0.1:7890` 或 `cn=http://user:pass@10.0.0.1:8080,socks5://10.0.0.2:1080`
    #: 🔴 每个出口会各自建一个 httpx.Client ⇒ **独立 cookie jar ⇒ 独立浏览器身份**。
    #: 这一步不能省：上游限流是 **IP 与浏览器身份两层**，只换 IP 会被浏览器那层挡住。
    #: 配了多个出口时**不要**再固定 `IMAGEFREE_FREE_GENERATION_ID`（那会让所有出口共用一个身份）。
    imagefree_proxies: str = ""

    #: 配了代理时，是否仍把**直连**当作一个出口、并排在**最前面**。
    #: 1 = 直连优先，池子只兜底（直连的名额满了，溢出的任务才走代理池）；
    #: 0 = **全部走池子**（直连完全不出现，连查询也不走直连）—— **默认**。
    #: 🔴 默认走池子的理由：上游按 **IP** 记账 ⇒ 直连烧的是**本机出口 IP 的额度**；
    #:    轮换型池子每连接换一个 IP ⇒ 等价于扩容量且不动本机名额。
    #: `IMAGEFREE_PROXIES` 留空时本项无意义：仍只有直连一个出口（与加代理之前逐字一致）。
    imagefree_use_direct: int = 0

    #: 🔴 **池子扇出**：把每一个代理出口复制成 N 个（各自独立客户端 ⇒ 独立 cookie jar
    #: + 独立连接池）= **扩容量**的手段。上游按 IP 记在途名额（单 IP 上限 3，实测见
    #: docs/UPSTREAM.md §9）；轮换型池子（每 TCP 连接换一个出口 IP）复制出来的每个出口
    #: 都会真的拿到不同 IP ⇒ N 个出口最多 `3N` 个在途任务，且身份彼此独立。
    #: 1（默认）= 不展开，与加这个参数之前**逐字一致**。
    #: ⚠️ 只对**代理**出口生效 —— 直连不参与（它只有一个 IP，复制 = 同一名额被重复计数）。
    #: ⚠️ 每个出口一个客户端；上限见 `egress.py::MAX_POOL_FANOUT`。
    imagefree_pool_fanout: int = 1

    #: 某个出口撞上"同 IP/同浏览器已有在途任务"后，冷置它多少秒。
    #: 目的是让**后来的任务优先换别的出口**，而不是死等同一个 IP。0 = 不冷却。
    imagefree_proxy_cooldown: float = 60.0

    #: 撞上在途互斥后，在**同一个出口**上当场再试几次（每次都是新连接）。
    #: 轮换型代理的下一连接很可能就是新出口 IP ⇒ 比等十几秒退避划算得多；
    #: 直连出口的 IP 不会变，代码里对直连出口**跳过**原地重试。
    #: 每次被拒都是"上游什么都没创建" ⇒ 重试**不消耗额度**。
    imagefree_proxy_retries: int = 3

    # ---------------------------------------------------------------- 对外鉴权
    #: 逗号分隔的静态白名单。**留空 = 鉴权整体关闭**（仅限内网/联调，启动打 WARNING）。
    #: 🔴 生产**必须**设：不设的话任何能访问本端口的人都能消耗你的免费额度。
    api_keys: str = ""

    # ---------------------------------------------------------------- 节奏闸门
    #: 同时在上游跑的任务数。**默认 1 是刻意的**：上游的限流维度是
    #: 「同浏览器/同 IP 只允许一个在途任务」（FREE_TASK_*），并发 > 1 的第二个请求
    #: 必然被拒 ⇒ 并发只会把失败率抬高，不会提高吞吐。
    if_concurrency: int = 1

    #: 相邻两次**提交**之间的最小间隔（秒）。0 = 不限。
    if_min_interval: float = 0.0

    #: 每分钟最多提交几个任务。0 = 不限。
    if_per_minute: int = 0

    # ---------------------------------------------------------------- 轮询
    #: 提交之后、第一次查询之前的等待（秒）。**早问一次是零代价的**
    #: （未落库只是"还没好"），晚问才是代价 ⇒ 取小值。
    poll_grace: float = 0.5

    #: 两次查询之间的间隔（秒）。上游出图实测 20~60s
    #: （前端自己用 6/4/10/15/20 然后 30 的退避序列，见 docs/UPSTREAM.md §3.1）。
    #: 本服务用**固定 5s**：固定间隔对账简单，且查询是零额度的只读动作。
    poll_interval: float = 5.0

    #: 单个任务的最长等待秒数。超时 → 任务终态 failure（code=task_timeout）。
    #: 默认 780 略大于前端自己的预算（≈775s）；上游超时后是否仍会跑完**未取证**。
    task_timeout: float = 780.0

    #: 图生图（image-i2i，走 /api/ai-photo-editor）的独立超时预算（秒）。
    #: 🔴 编辑器实测**长期 pending**（>10 分钟仍 pending/50，docs/UPSTREAM.md §10.4）
    #: ⇒ 生成链路的 780s 预算对它完全不适用，这里给 1 小时。
    task_timeout_i2i: float = 3600.0

    # ---------------------------------------------------------------- 图生图（image-i2i）
    #: 参考图大小上限（MB）。上游 `/api/ai-photo-editor` 的上传限额实测为 **10MB**。
    i2i_upload_limit_mb: float = 10.0

    #: 「代取调用方参考图」（下载 image URL / 解 data URI）的超时（秒）。
    #: 这是调用方给的地址，不是上游 —— 超时上限收紧，失败走任务终态 failure。
    i2i_fetch_timeout: float = 30.0

    # ---------------------------------------------------------------- 协调器
    #: ⚠️ 提交会消耗上游免费额度。**冒烟/自检时务必设 0**，否则它会代替你向上游提交。
    coordinator_enabled: int = 1

    #: 协调器 tick 间隔（秒）。
    coordinator_tick: float = 1.0

    #: 任务租约时长（秒）。多副本时靠它选主；必须 >= 2×poll_interval，
    #: 否则每轮都换主 = 等于没有租约。单副本部署时它只承担"崩了能自愈"。
    coordinator_lease: float = 30.0

    #: 优雅关闭时等协调器收尾的预算（秒）。**略小于** `gunicorn_conf.graceful_timeout`（30s）。
    #: 协调器收到停止信号后会**拦在提交动作之前**（不再发起新的上游请求），所以这里等的
    #: 通常只是"一个已经在飞的 HTTP 请求"，不是整轮 tick。
    #: 等超了只能强杀：那一刻若正好有提交在飞，重启后会被判 `submit_unknown`（不会静默双建）。
    coordinator_stop_grace: float = 25.0

    # ---------------------------------------------------------------- 持久化
    #: 任务库是**事实源**（受理不碰上游 ⇒ 任务必须先落地）。
    #: 生产用 PostgreSQL；SQLite 仅供本地试跑。
    task_db: str = "sqlite+pysqlite:///./imagefree.db"

    #: 终态任务保留天数。过期由协调器清理。
    task_retention_days: int = 7

    # ---------------------------------------------------------------- 可观测性
    #: 留空 = 只在本地留 span、不上报（启动时会说明原因）。
    logfire_token: str = ""
    otel_service_name: str = "imagefree-service"
    logfire_environment: str = ""
    #: 1 = 把上游原始报文绑成 span 属性。观测面**刻意不脱敏**：事后脱敏会改掉
    #: 上游的实际字段名，让人对着面板排查一个不存在的字段。
    otel_capture_upstream: int = 1
    #: 脱敏开关。**默认 0**（理由同 hailuo：SDK 自带 scrubber 按值子串命中
    #: `token`/`credential` ⇒ 会把产物 URL 整条打成 [Scrubbed]）。
    otel_scrubbing: int = 0

    # ---------------------------------------------------------------- 服务
    host: str = "0.0.0.0"
    port: int = 8400
    log_level: str = "INFO"

    # ---------------------------------------------------------------- 派生量
    def documented_defaults(self) -> dict[str, Any]:
        """给 `/capabilities` 用的配置快照（**不含任何凭据值**）。"""
        return {
            "upstream_base_url": self.imagefree_base_url,
            "if_concurrency": self.if_concurrency,
            "poll_interval": self.poll_interval,
            "task_timeout": self.task_timeout,
            "task_timeout_i2i": self.task_timeout_i2i,
            "i2i_upload_limit_mb": self.i2i_upload_limit_mb,
            "i2i_fetch_timeout": self.i2i_fetch_timeout,
            "task_retention_days": self.task_retention_days,
            "auth_enabled": bool(self.api_keys.strip()),
            "turnstile_token_configured": bool(self.imagefree_turnstile_token),
            "pinned_browser_id": bool(self.imagefree_free_generation_id),
            "egress": {
                # 只报"配了几个出口"，**不报代理地址**（地址里可能带凭据；
                # 打码后的快照在 /stats 的 egress 段里）。
                "configured": _egress_entry_count(self.imagefree_proxies),
                "rotating": bool(self.imagefree_proxies.strip()),
                "use_direct": bool(self.imagefree_use_direct),
                "pool_fanout": self.imagefree_pool_fanout,
                "cooldown_seconds": self.imagefree_proxy_cooldown,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试里用 `Settings(...)` 直接构造，不走这里。"""
    return Settings()


def _egress_entry_count(raw: str) -> int:
    """数一下出口清单里有几条 —— **只数，不校验**。

    校验（scheme/标签/重复）在 `app/egress.py::parse_egress_specs`；
    这里不能引用它（会与 `egress.py` 形成循环导入），也不该在配置快照里做校验
    （配置写错的后果是**启动失败**，不是"/capabilities 少一个字段"）。
    留空时返回 1：只有一个直连出口。
    """
    entries = [item for item in (raw or "").split(",") if item.strip()]
    return len(entries) or 1


#: 兜底导出，方便 `python -c "from app.config import settings"` 快速看配置。
settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
