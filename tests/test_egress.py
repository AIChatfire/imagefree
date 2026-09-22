#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出口（加代理）的**离线**验证。

这一层最容易出现"看着生效、其实没生效"的假绿，所以要钉死三件事：

  1. **换出口真的发生了**（不是"重试了同一个出口"）；
  2. **换出口的理由是对的**（只在"上游没创建任何任务"时才换，且身份被固定时不白撞）；
  3. **一个出口一套身份**（cookie jar 相互独立），且**任务的轮询走回它自己的出口**。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import models
from app.config import Settings
from app.coordinator import Coordinator
from app.egress import DIRECT_LABEL, Egress, EgressConfigError, EgressPool, parse_egress_specs
from app.main import create_app
from app.service import GenerationService
from app.store import QUEUED, SUCCESS, TaskStore, utcnow
from app.upstream import ImageFreeClient
from tests.conftest import SUBMITTED_TASK_ID, EgressFixture, FakeUpstream

# ---------------------------------------------------------------------------
# 配置解析：写错必须响亮失败
# ---------------------------------------------------------------------------


def test_empty_config_means_a_single_direct_egress() -> None:
    """留空 = 直连，与"加代理之前"逐字一致。"""
    assert parse_egress_specs("") == (Egress(label=DIRECT_LABEL, proxy=None),)
    assert parse_egress_specs("   ")[0].label == DIRECT_LABEL


def test_unlabeled_entries_are_numbered() -> None:
    egresses = parse_egress_specs("http://10.0.0.1:8080,socks5://10.0.0.2:1080")
    assert [e.label for e in egresses] == ["proxy1", "proxy2"]
    assert egresses[1].proxy == "socks5://10.0.0.2:1080"


def test_labeled_entries_keep_their_names() -> None:
    egresses = parse_egress_specs("cn=http://10.0.0.1:8080, us=socks5h://10.0.0.2:1080")
    assert [e.label for e in egresses] == ["cn", "us"]


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://10.0.0.1:21",                       # scheme 不支持
        "http://",                                 # 缺主机名
        "cn=http://1.1.1.1:1,cn=http://2.2.2.2:2",  # 标签重复
        "http://1.1.1.1:1,http://1.1.1.1:1",       # 同一个代理写两遍
        "a b=http://1.1.1.1:1",                    # 标签含非法字符
        "cn=",                                     # 只写标签没写地址
    ],
)
def test_bad_proxy_config_fails_loudly(raw: str) -> None:
    """静默忽略的后果是"看着配了却什么都没发生" ⇒ 必须炸。"""
    with pytest.raises(EgressConfigError):
        parse_egress_specs(raw)


def test_same_proxy_twice_is_rejected_because_it_is_not_two_ips() -> None:
    with pytest.raises(EgressConfigError) as excinfo:
        parse_egress_specs("http://1.1.1.1:1,http://1.1.1.1:1")
    assert "同一个代理" in str(excinfo.value)


def test_credentials_are_masked_in_the_observation_surface() -> None:
    egress = parse_egress_specs("http://alice:sup3rsecret@10.0.0.1:8080")[0]
    masked = egress.masked()
    assert masked is not None
    assert "sup3rsecret" not in masked and "alice" not in masked
    assert "10.0.0.1:8080" in masked
    # 没凭据的地址原样返回（否则排障时看不出到底连的哪儿）
    assert parse_egress_specs("http://10.0.0.2:8080")[0].masked() == "http://10.0.0.2:8080"


def test_bad_config_makes_the_app_fail_to_start(make_settings: Any) -> None:
    """🔴 配置错误必须在**启动**暴露，而不是"跑起来之后静默全走直连"。"""
    settings = make_settings(imagefree_proxies="ftp://10.0.0.1:21")
    with pytest.raises(EgressConfigError):
        create_app(settings=settings, start_coordinator=False)


# ---------------------------------------------------------------------------
# 池子：选择与冷却
# ---------------------------------------------------------------------------


def _pool(settings: Settings, labels: tuple[str, ...]) -> EgressPool:
    """手工造池（不走 `IMAGEFREE_PROXIES` 解析），出口全是**不拨号**的假客户端。

    ⚠️ 出口客户端**不传 `proxy=`**：httpx 同时收到 `transport=` 与 `proxy=` 时会走代理传输层，
    把假传输绕开（见 `tests/test_egress.py::test_transport_plus_proxy_bypasses_the_fake_transport`）。
    """
    proxies = [f"http://10.0.0.{idx}:8080" for idx in range(1, len(labels) + 1)]
    clients = {
        label: ImageFreeClient(settings, label=label, transport=FakeUpstream().transport())
        for label in labels
    }
    egresses = tuple(
        Egress(label=label, proxy=proxy) for label, proxy in zip(labels, proxies, strict=True)
    )
    return EgressPool(settings, clients=clients, egresses=egresses)


def test_transport_plus_proxy_bypasses_the_fake_transport() -> None:
    """钉住一个**会导致假绿**的 httpx 行为：`transport=` 与 `proxy=` 同时给时走代理传输层。

    本用例断言"零出网守卫会响" —— 它红了就说明 httpx 改了行为，
    那时要重新评估测试夹具该怎么写（而不是删掉这条用例）。
    """
    import httpx

    settings = Settings(_env_file=None, task_db="sqlite+pysqlite:///:memory:")  # type: ignore[call-arg]
    client = ImageFreeClient(
        settings, proxy="http://127.0.0.1:9", transport=FakeUpstream().transport()
    )
    try:
        # 属性层面它**看着**是假传输 —— 这正是当初被骗过去的地方。
        assert isinstance(client._client._transport, httpx.MockTransport)
        with pytest.raises(AssertionError, match="真实出网"):
            client.submit("cat", "1:1")
    finally:
        client.close()


def test_pool_reports_whether_it_is_actually_rotating(make_settings: Any) -> None:
    assert EgressPool(make_settings()).is_rotating is False, "空配置 = 直连，不算轮换"
    single = _pool(make_settings(imagefree_proxies="http://10.0.0.1:8080"), ("proxy1",))
    assert single.is_rotating is True
    assert len(single) == 1
    single.close()


def test_available_egresses_respect_the_per_ip_inflight_limit(make_settings: Any) -> None:
    """名额按 IP 记、上限 3：剩余多的排前面；满了或冷却中就不再候选。"""
    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080,http://10.0.0.2:8080")
    pool = _pool(settings, ("proxy1", "proxy2"))
    now = utcnow()
    try:
        def labels(counts: dict[str, int], limit: int = 3) -> list[str]:
            return [
                e.label
                for e in pool.available_egresses(
                    in_flight_by_label=counts, per_egress_limit=limit, now=now
                )
            ]

        assert labels({}) == ["proxy1", "proxy2"]
        assert labels({"proxy1": 2}) == ["proxy1", "proxy2"], "顺序即优先级：还有名额就继续用它"
        assert labels({"proxy1": 3}) == ["proxy2"], "满了就不再候选"
        assert labels({"proxy1": 3, "proxy2": 3}) == []
        assert labels({"proxy1": 1}, limit=1) == ["proxy2"], "上限可配"
        pool.note_wall("proxy2", now=now)
        assert labels({}) == ["proxy1"], "冷却中的出口也不候选"
        later = pool.available_egresses(
            in_flight_by_label={}, per_egress_limit=3, now=now + timedelta(seconds=61)
        )
        assert [e.label for e in later] == ["proxy1", "proxy2"], "冷却到期后回来"
    finally:
        pool.close()


def test_single_egress_is_never_cooled(make_settings: Any) -> None:
    """换无可换时冷置唯一出口 = 用更长的死等换掉退避逻辑。"""
    pool = _pool(make_settings(), (DIRECT_LABEL,))
    now = utcnow()
    try:
        pool.note_wall(DIRECT_LABEL, now=now)
        assert pool.is_cooling(DIRECT_LABEL, now=now) is False
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# 协调器：换出口
# ---------------------------------------------------------------------------


@contextmanager
def _env(
    fixture: Any, *, capacity: int | None = None
) -> Iterator[tuple[TaskStore, GenerationService, Coordinator]]:
    store = TaskStore(fixture.settings.task_db)
    settings = fixture.settings
    if capacity is not None:
        settings = settings.model_copy(update={"if_concurrency": capacity})
    coord = Coordinator(store, fixture.pool, settings)
    try:
        yield store, GenerationService(store, fixture.settings), coord
    finally:
        store.close()


def _accept(service: GenerationService, prompt: str = "cat") -> str:
    return service.accept(models.GenerationRequest.model_validate({"prompt": prompt}), None)


def test_effective_capacity_is_egress_count_times_per_ip_limit(make_pool: Any) -> None:
    """实测：单 IP 在途上限 3（docs/UPSTREAM.md §2.2.2）⇒ **加出口能真的扩容量**。"""
    with _env(make_pool(("proxy1",)), capacity=9) as (_s, _svc, coord):
        assert coord.capacity == 3, "1 个出口 × 单 IP 3 = 3"
    with _env(make_pool(("proxy1", "proxy2")), capacity=5) as (_s, _svc, coord):
        assert coord.capacity == 5, "2 个出口物理上限 6 ⇒ 被 IF_CONCURRENCY=5 压回 5"
    with _env(make_pool(("proxy1", "proxy2")), capacity=99) as (_s, _svc, coord):
        assert coord.capacity == 6, "2 个出口 × 3"


def test_three_tasks_fit_on_one_egress_but_the_fourth_waits(make_pool: Any) -> None:
    """一个出口能同时跑 3 个（实测），第 4 个要等名额腾出来。

    这条刻意**反着**钉以前那个错误结论（"一个出口只允许一个在途任务"）：
    实测 3 条并发全部被受理，第 4、5 条才撞 429。
    """
    fixture = make_pool(("proxy1",))
    with _env(fixture, capacity=9) as (store, service, coord):
        assert coord.capacity == 3
        ids = [_accept(service, f"t{index}") for index in range(4)]
        now = utcnow()
        assert coord.tick(now=now)["submitted"] == 3
        assert len(fixture.fakes["proxy1"].submit_calls) == 3
        assert store.count_in_flight() == 3
        # 再推进一轮：名额仍被占着 ⇒ 第 4 个不许发出去（在途的三个只是被轮询）
        coord.tick(now=now + timedelta(seconds=1))
        assert len(fixture.fakes["proxy1"].submit_calls) == 3
        assert store.get_task(ids[3]).status == QUEUED  # type: ignore[union-attr]


def test_tasks_fill_the_first_egress_then_overflow(make_pool: Any) -> None:
    """出口**按配置顺序**填：先把第一个的 3 个名额用满，溢出才轮到下一个。

    「直连优先、并发溢出走池子」正是靠这条顺序保证的。
    """
    fixture = make_pool(("proxy1", "proxy2"))
    with _env(fixture, capacity=4) as (store, service, coord):
        for index in range(4):
            _accept(service, f"t{index}")
        assert coord.tick(now=utcnow())["submitted"] == 4
        assert len(fixture.fakes["proxy1"].submit_calls) == 3, "第一个出口先填满 3 个"
        assert len(fixture.fakes["proxy2"].submit_calls) == 1, "第 4 个溢出到下一个出口"
        assert {row.egress for row in store.list_tasks(None)} == {"proxy1", "proxy2"}


def test_ip_wall_switches_to_another_egress_in_the_same_tick(make_pool: Any) -> None:
    """这就是"遇到频控可以加代理"的落点：撞墙那次**没创建任何任务** ⇒ 换出口重试零额度损失。

    这里把原地重试关掉（`IMAGEFREE_PROXY_RETRIES=0`），单独验“换出口”这一步；
    原地重试见 `test_rotating_proxy_retries_in_place_before_switching_egress`。
    """
    fixture = make_pool(("proxy1", "proxy2"), imagefree_proxy_retries=0)
    fixture.fakes["proxy1"].fail_submit("FREE_TASK_IP_ACTIVE", "network task limit reached")
    with _env(fixture) as (store, service, coord):
        task_id = _accept(service)
        # ⚠️ now 必须在**受理之后**取：受理把 next_poll_at 设成它自己的 created_at，
        # 拿一个更早的 now 去 claim 会一个都抢不到（用例就变成什么都没发生）。
        now = utcnow()
        summary = coord.tick(now=now)
        assert summary["submitted"] == 1
        row = store.get_task(task_id)
        assert row is not None
        assert row.egress == "proxy2", "必须换到另一个出口"
        assert row.status != QUEUED
        assert len(fixture.fakes["proxy1"].submit_calls) == 1, "撞墙的出口只被试了一次"
        assert len(fixture.fakes["proxy2"].submit_calls) == 1
        assert fixture.pool.is_cooling("proxy1", now=now) is True
        assert fixture.pool.is_cooling("proxy2", now=now) is False


def test_rotating_proxy_retries_in_place_before_switching_egress(make_pool: Any) -> None:
    """轮换型代理：下一连接很可能就是新出口 IP ⇒ 先在同一出口原地重试，别的出口留着备用。

    这条钉的是"每连接换 IP"的语义（skills/per-request-egress-rotation）：
    撞墙后立刻开新连接，比等十几秒退避划算得多，而且被拒=上游没创建任务、零额度损失。
    """
    fixture = make_pool(("proxy1", "proxy2"))
    seen = {"n": 0}

    def scripted(_body: dict[str, Any]) -> dict[str, Any]:
        seen["n"] += 1
        if seen["n"] <= 2:  # 头两次的连接落在了"还在忙"的那个 IP 上
            return {"error": "ip busy", "errorCode": "FREE_TASK_IP_ACTIVE"}
        return {"taskId": SUBMITTED_TASK_ID}

    fixture.fakes["proxy1"].submit_override = scripted
    with _env(fixture) as (store, service, coord):
        task_id = _accept(service)
        now = utcnow()
        summary = coord.tick(now=now)
        assert summary["submitted"] == 1
        row = store.get_task(task_id)
        assert row is not None and row.egress == "proxy1", "原地重试成功就该留在 proxy1"
        assert len(fixture.fakes["proxy1"].submit_calls) == 3
        assert fixture.fakes["proxy2"].submit_calls == [], "原地重试能解决就不该去动别的出口"
        assert fixture.pool.is_cooling("proxy1", now=now) is False, "成功就不该被冷置"


def _pool_with_direct(settings: Settings, labels: tuple[str, ...]) -> tuple[EgressPool, dict[str, FakeUpstream]]:
    """造一个「直连在第一位 + 若干代理出口」的池（注入假上游，零出网）。"""
    egresses = [Egress(label=DIRECT_LABEL, proxy=None)] + [
        Egress(label=label, proxy=f"http://10.0.0.{index}:8080")
        for index, label in enumerate(labels, start=1)
    ]
    fakes = {egress.label: FakeUpstream() for egress in egresses}
    clients = {
        egress.label: ImageFreeClient(
            settings, label=egress.label, transport=fakes[egress.label].transport()
        )
        for egress in egresses
    }
    return EgressPool(settings, clients=clients, egresses=tuple(egresses)), fakes


def test_direct_egress_is_prepended_when_explicitly_enabled(make_settings: Any) -> None:
    """**显式**开直连（`IMAGEFREE_USE_DIRECT=1`）时：直连在第一位，满了才轮到池子。

    ⚠️ 这不是默认行为（2026-09-22 起默认 = 全走池子，见下一条用例）——
    所以这里必须显式写 1；靠默认值的话，哪天默认再变这条会静默测错东西。
    """
    settings = make_settings(
        imagefree_proxies="http://10.0.0.1:8080,http://10.0.0.2:8080",
        imagefree_use_direct=1,
    )
    pool = EgressPool(settings)
    try:
        assert pool.labels == (DIRECT_LABEL, "proxy1", "proxy2"), "直连必须排在最前面"
        assert pool.is_rotating is True
        assert [e.label for e in pool.available_egresses(in_flight_by_label={}, per_egress_limit=3)] == [
            DIRECT_LABEL,
            "proxy1",
            "proxy2",
        ]
        # 直连名额满了 ⇒ 下一个候选就是池子的出口
        remaining = pool.available_egresses(
            in_flight_by_label={DIRECT_LABEL: 3}, per_egress_limit=3
        )
        assert [e.label for e in remaining] == ["proxy1", "proxy2"]
    finally:
        pool.close()


def test_use_direct_zero_sends_everything_through_the_pool(make_settings: Any) -> None:
    settings = make_settings(
        imagefree_proxies="http://10.0.0.1:8080", imagefree_use_direct=0
    )
    pool = EgressPool(settings)
    try:
        assert pool.labels == ("proxy1",), "关掉直连就该完全走池子"
    finally:
        pool.close()


def test_default_is_pool_only_without_direct_egress(make_settings: Any) -> None:
    """🔴 **默认**（不设 `IMAGEFREE_USE_DIRECT`）= 全走池子：直连不出现在出口清单里。

    理由：上游按 **IP** 记账 ⇒ 直连烧的是**本机出口 IP 的额度**；轮换型池子每连接换一个
    IP。这条钉的是**默认值本身**（不是某个显式配置的行为）—— 有人把默认改回「直连优先」
    时会红。
    """
    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080")
    assert settings.imagefree_use_direct == 0, "默认必须是 0（全走池子）"
    pool = EgressPool(settings)
    try:
        assert pool.labels == ("proxy1",), "默认不该出现直连出口"
        assert pool.is_rotating is True
    finally:
        pool.close()


def test_empty_proxies_still_falls_back_to_direct(make_settings: Any) -> None:
    """没配池子时默认值无意义：仍然只有直连一个出口（新用户 clone 下来直接能跑）。"""
    settings = make_settings(imagefree_proxies="")
    assert settings.imagefree_use_direct == 0
    pool = EgressPool(settings)
    try:
        assert pool.labels == (DIRECT_LABEL,), "空配置 ⇒ 唯一出口是直连"
        assert pool.is_rotating is False
    finally:
        pool.close()


def test_pool_fanout_expands_one_entry_into_independent_egresses(make_settings: Any) -> None:
    """`IMAGEFREE_POOL_FANOUT=N`：一个池子入口展开成 N 个出口 —— 这是**扩容量**。

    上游按 IP 记在途名额（单 IP 3）；轮换型池子复制出的每个出口都能拿到不同 IP
    ⇒ N 个出口最多 3N 个在途。关键是**独立客户端**：共用客户端 = 共用 cookie jar = 同一个身份。
    """
    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080", imagefree_pool_fanout=3)
    pool = EgressPool(settings)
    try:
        assert pool.labels == ("proxy1-1", "proxy1-2", "proxy1-3")
        assert len({id(pool.client(label)) for label in pool.labels}) == 3, "必须是三个独立客户端"
    finally:
        pool.close()


def test_pool_fanout_default_is_one_and_leaves_labels_untouched(make_settings: Any) -> None:
    """默认不扇出：标签都不动 —— 与加这个参数之前**逐字一致**。"""
    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080")
    assert settings.imagefree_pool_fanout == 1, "默认必须是 1（不展开）"
    pool = EgressPool(settings)
    try:
        assert pool.labels == ("proxy1",)
    finally:
        pool.close()


def test_pool_fanout_never_multiplies_the_direct_egress(make_settings: Any) -> None:
    """直连**不参与**扇出：它只有一个 IP，复制只会让同一名额被重复计数。"""
    settings = make_settings(
        imagefree_proxies="http://10.0.0.1:8080",
        imagefree_pool_fanout=2,
        imagefree_use_direct=1,
    )
    pool = EgressPool(settings)
    try:
        assert pool.labels == (DIRECT_LABEL, "proxy1-1", "proxy1-2")
    finally:
        pool.close()


def test_pool_fanout_over_the_cap_fails_loudly(make_settings: Any) -> None:
    """超上限 ⇒ **启动期响亮失败**（"一行配置造几百个客户端"的口子必须堵住）。"""
    from app.egress import MAX_POOL_FANOUT, EgressConfigError

    with pytest.raises(EgressConfigError, match="POOL_FANOUT"):
        EgressPool(
            make_settings(
                imagefree_proxies="http://10.0.0.1:8080",
                imagefree_pool_fanout=MAX_POOL_FANOUT + 1,
            )
        )


def test_overflow_tasks_go_to_the_pool_when_direct_is_full(make_settings: Any) -> None:
    """直连跑满 3 个之后，第 4 个任务自动落到代理出口 —— 这就是要的行为。

    一条真实请求都没发：出口全是注入的假上游。
    """
    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080")
    pool, fakes = _pool_with_direct(settings, ("proxy1",))
    fixture = EgressFixture(settings=settings, fakes=fakes, pool=pool)
    try:
        with _env(fixture, capacity=9) as (store, service, coord):
            assert coord.capacity == 6, "2 个出口 × 单 IP 3 = 6"
            for index in range(4):
                _accept(service, f"t{index}")
            now = utcnow()
            assert coord.tick(now=now)["submitted"] == 4
            assert len(fakes["direct"].submit_calls) == 3, "直连先用满 3 个名额"
            assert len(fakes["proxy1"].submit_calls) == 1, "第 4 个溢出到池子"
            rows = {row.egress for row in store.list_tasks(None)}
            assert rows == {"direct", "proxy1"}
    finally:
        pool.close()


def test_broken_egress_is_cooled_and_the_next_one_takes_over(make_settings: Any) -> None:
    """出口连不上（例如代理拒绝认证）⇒ 冷置它、换下一条路，**而不是**让任务失败。"""
    import httpx

    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080,http://10.0.0.2:8080")
    pool = _pool(settings, ("proxy1", "proxy2"))
    fixture = EgressFixture(settings=settings, fakes={"proxy1": FakeUpstream(), "proxy2": FakeUpstream()}, pool=pool)
    broken = FakeUpstream()
    broken.submit_raises = httpx.ProxyError("Invalid username/password")
    pool._clients["proxy1"] = __import__("app.upstream", fromlist=["ImageFreeClient"]).ImageFreeClient(
        settings, label="proxy1", transport=broken.transport()
    )
    fixture.fakes["proxy1"] = broken
    try:
        with _env(fixture) as (store, service, coord):
            task_id = _accept(service)
            now = utcnow()
            summary = coord.tick(now=now)
            assert summary["submitted"] == 1 and summary["finalized"] == 0
            row = store.get_task(task_id)
            assert row is not None and row.egress == "proxy2", "坏掉的出口要被跳过"
            assert pool.is_cooling("proxy1", now=now) is True
            assert len(broken.submit_calls) == 1, "坏出口不该被反复撞"
    finally:
        pool.close()


def test_broken_direct_hands_over_to_the_pool(make_settings: Any) -> None:
    """直连坏了也一样：池子顶上（这正是"触发并发/出故障就走池子"的另一种形态）。"""
    import httpx

    settings = make_settings(imagefree_proxies="http://10.0.0.1:8080")
    pool, fakes = _pool_with_direct(settings, ("proxy1",))
    broken = FakeUpstream()
    broken.submit_raises = httpx.ProxyError("connection refused")
    pool._clients[DIRECT_LABEL] = __import__("app.upstream", fromlist=["ImageFreeClient"]).ImageFreeClient(
        settings, label=DIRECT_LABEL, transport=broken.transport()
    )
    fakes[DIRECT_LABEL] = broken
    fixture = EgressFixture(settings=settings, fakes=fakes, pool=pool)
    try:
        with _env(fixture) as (store, service, coord):
            task_id = _accept(service)
            assert coord.tick(now=utcnow())["submitted"] == 1
            row = store.get_task(task_id)
            assert row is not None and row.egress == "proxy1"
            assert len(broken.submit_calls) == 1
    finally:
        pool.close()


def test_polls_go_back_through_the_task_own_egress(make_pool: Any) -> None:
    """浏览器身份住在那个出口的 cookie jar 里 ⇒ 轮询必须走回同一个出口。"""
    fixture = make_pool(("proxy1", "proxy2"))
    fixture.fakes["proxy1"].fail_submit("FREE_TASK_IP_ACTIVE")
    with _env(fixture) as (store, service, coord):
        task_id = _accept(service)
        now = utcnow()
        coord.tick(now=now)
        assert store.get_task(task_id).egress == "proxy2"  # type: ignore[union-attr]
        coord.tick(now=now + timedelta(seconds=1))  # 这一轮是轮询
        assert fixture.fakes["proxy2"].status_calls == [SUBMITTED_TASK_ID]
        assert fixture.fakes["proxy1"].status_calls == [], "轮询不该从别的出口出去"


def test_all_egresses_walled_is_a_backoff_not_a_failure(make_pool: Any) -> None:
    fixture = make_pool(("proxy1", "proxy2"), imagefree_proxy_retries=0)
    fixture.fakes["proxy1"].fail_submit("FREE_TASK_IP_ACTIVE")
    fixture.fakes["proxy2"].fail_submit("FREE_TASK_IP_ACTIVE")
    with _env(fixture) as (store, service, coord):
        task_id = _accept(service)
        now = utcnow()
        summary = coord.tick(now=now)
        assert summary["wall_blocked"] == 1 and summary["finalized"] == 0
        row = store.get_task(task_id)
        assert row is not None and row.status == QUEUED
        assert row.error_code == "upstream_ip_task_active"
        assert row.next_poll_at is not None and row.next_poll_at > now
        assert len(fixture.fakes["proxy1"].submit_calls) == 1
        assert len(fixture.fakes["proxy2"].submit_calls) == 1


def test_pinned_browser_identity_stops_the_failover(make_pool: Any) -> None:
    """身份被固定 ⇒ 所有出口共用同一个浏览器身份，换出口对浏览器那层的墙无效。

    此时**不该**把 N 个出口各试一遍（每次都是白撞，还给上游多留几笔痕迹）。
    """
    fixture = make_pool(("proxy1", "proxy2"), imagefree_free_generation_id="pinned-id")
    fixture.fakes["proxy1"].fail_submit("FREE_GENERATION_ACTIVE", "generation already in progress")
    with _env(fixture) as (store, service, coord):
        task_id = _accept(service)
        assert coord.tick(now=utcnow())["wall_blocked"] == 1
        assert len(fixture.fakes["proxy1"].submit_calls) == 1
        assert fixture.fakes["proxy2"].submit_calls == [], "身份固定时不该再试别的出口"
        assert store.get_task(task_id).status == QUEUED  # type: ignore[union-attr]


def test_each_egress_learns_its_own_browser_identity(make_pool: Any) -> None:
    """一个出口一套身份：cookie jar 相互独立。"""
    fixture = make_pool(("proxy1", "proxy2"))
    fixture.fakes["proxy1"].submit_set_cookie = "imagefree_free_generation_id=issued-to-proxy1; Path=/"
    with _env(fixture) as (_store, service, coord):
        _accept(service)
        coord.tick(now=utcnow())
        ids = fixture.pool.browser_ids()
        assert ids["proxy1"] == "issued-to-proxy1"
        assert ids["proxy2"] is None, "另一个出口不该凭空拥有同一个身份"


def test_learned_identity_is_reused_by_the_same_egress(make_pool: Any) -> None:
    """拿到的身份要**持续带上**（否则上游每次都当我们是新访客，浏览器维度的记账就散了）。"""
    fixture = make_pool(("proxy1", "proxy2"))
    fixture.fakes["proxy1"].submit_set_cookie = "imagefree_free_generation_id=keep-me; Path=/"
    fixture.fakes["proxy1"].complete()  # 让 proxy1 上那个任务走完，好让它再次空闲
    with _env(fixture, capacity=2) as (store, service, coord):
        first = _accept(service, "first")
        now = utcnow()
        coord.tick(now=now)  # 第一次提交 → 学到身份
        coord.tick(now=now + timedelta(seconds=1))  # 轮询 → completed → 终态，proxy1 空出来
        assert store.get_task(first).status == SUCCESS  # type: ignore[union-attr]
        _accept(service, "second")
        coord.tick(now=now + timedelta(seconds=2))  # 再提交：仍走 proxy1（顺序在先且空闲）
        submits = fixture.fakes["proxy1"].submit_headers
        assert len(submits) == 2, "两次提交都应该走 proxy1"
        assert "cookie" not in {k.lower() for k in submits[0]}, "第一次提交时还没有身份"
        assert "imagefree_free_generation_id=keep-me" in submits[1].get("cookie", "")


def test_another_egress_does_not_send_the_first_egress_cookie(make_pool: Any) -> None:
    """出口之间 cookie 互不串（每个出口一个客户端）。"""
    fixture = make_pool(("proxy1", "proxy2"))
    fixture.fakes["proxy1"].submit_set_cookie = "imagefree_free_generation_id=only-proxy1; Path=/"
    with _env(fixture, capacity=4) as (_store, service, coord):
        for index in range(4):
            _accept(service, f"t{index}")
        coord.tick(now=utcnow())
        assert len(fixture.fakes["proxy1"].submit_calls) == 3
        assert len(fixture.fakes["proxy2"].submit_calls) == 1
        assert "cookie" not in {k.lower() for k in fixture.fakes["proxy2"].submit_headers[0]}


# ---------------------------------------------------------------------------
# 观测端点
# ---------------------------------------------------------------------------


def _client_for(fixture: Any, store: TaskStore) -> TestClient:
    app = create_app(
        settings=fixture.settings, store=store, pool=fixture.pool, start_coordinator=False
    )
    return TestClient(app)


def test_stats_reports_egress_state_and_effective_capacity(make_pool: Any) -> None:
    fixture = make_pool(("proxy1", "proxy2"), if_concurrency=5)
    store = TaskStore(fixture.settings.task_db)
    try:
        with _client_for(fixture, store) as api:
            body = api.get("/stats").json()
            assert body["gate"]["if_concurrency"] == 5
            assert body["gate"]["effective_capacity"] == 5, "2 个出口物理上限 6 ⇒ 5 生效"
            assert [item["label"] for item in body["egress"]] == ["proxy1", "proxy2"]
            assert all(item["in_flight"] == 0 for item in body["egress"])
            assert body["egress"][0]["proxy"] == "http://10.0.0.1:8080"

            caps = api.get("/capabilities").json()
            assert caps["egress"]["rotating"] is True
            assert [e["label"] for e in caps["egress"]["endpoints"]] == ["proxy1", "proxy2"]
    finally:
        store.close()


def test_readyz_reports_egress_without_proxy_addresses(make_pool: Any) -> None:
    fixture = make_pool(("proxy1", "proxy2"))
    store = TaskStore(fixture.settings.task_db)
    try:
        with _client_for(fixture, store) as api:
            body = api.get("/readyz").json()
            egress = body["upstream"]["egress"]
            assert egress["count"] == 2 and egress["rotating"] is True
            assert egress["labels"] == ["proxy1", "proxy2"]
            assert egress["identity_acquired"] == {"proxy1": False, "proxy2": False}
    finally:
        store.close()


def test_observation_surface_never_leaks_proxy_credentials(make_settings: Any, fake: FakeUpstream) -> None:
    settings = make_settings(imagefree_proxies="http://alice:sup3rsecret@10.0.0.9:8080")
    pool = EgressPool(
        settings, clients={"proxy1": ImageFreeClient(settings, label="proxy1", transport=fake.transport())}
    )
    store = TaskStore(settings.task_db)
    try:
        app = create_app(settings=settings, store=store, pool=pool, start_coordinator=False)
        with TestClient(app) as api:
            blob = api.get("/capabilities").text + api.get("/stats").text + api.get("/readyz").text
        assert "sup3rsecret" not in blob
        assert "alice" not in blob
        assert "10.0.0.9:8080" in blob, "地址本身要留着（排障要看连的是哪台）"
    finally:
        store.close()
        pool.close()
