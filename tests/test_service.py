#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务层与协调器的**离线**验证。

这里钉的是最容易悄悄错的两类东西：

  · **归一化**（比例吸附、模型别名、降级留痕）—— 错了会让调用方按 A 的预期拿到 B；
  · **收口规则**（何时终态、何时退避、超时怎么算）—— 错了会留下永不结束的任务，
    或者把"上游忙"当成"任务失败"。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from app import models
from app.config import Settings
from app.coordinator import RETRY_BACKOFF_SECONDS, Coordinator, SubmitGate
from app.egress import EgressPool
from app.errors import InvalidParameterError
from app.service import GenerationService, fingerprint_key, new_task_id
from app.store import (
    FAILURE,
    IN_PROGRESS,
    QUEUED,
    SUCCESS,
    TaskStore,
    utcnow,
)
from app.upstream import ImageFreeClient
from tests.conftest import IMAGE_URL, SUBMITTED_TASK_ID, FakeUpstream


def _service(store: TaskStore, settings: Settings) -> GenerationService:
    return GenerationService(store, settings)


def _coordinator(store: TaskStore, client: ImageFreeClient, settings: Settings) -> Coordinator:
    """单出口协调器（没配代理时的形态）。多出口的用例见 `tests/test_egress.py`。"""
    return Coordinator(store, EgressPool.for_single_client(settings, client), settings)


def _accept(service: GenerationService, **payload: Any) -> str:
    body = {"prompt": "a cat on a windowsill"}
    body.update(payload)
    return service.accept(models.GenerationRequest.model_validate(body), "fp")


# ---------------------------------------------------------------------------
# 归一化：比例吸附
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1024, 1024, "1:1"),
        (2048, 2048, "1:1"),
        (768, 1024, "3:4"),
        (1024, 768, "4:3"),
        (576, 1024, "9:16"),
        (1024, 576, "16:9"),
        (1920, 1080, "16:9"),
        (1080, 1920, "9:16"),
        (500, 500, "1:1"),
        (1000, 999, "1:1"),  # 微差必须仍然吸附到 1:1，不能跳到 4:3
    ],
)
def test_nearest_aspect_ratio(width: int, height: int, expected: str) -> None:
    assert models.nearest_aspect_ratio(width, height) == expected


def test_parse_size_accepts_common_separators() -> None:
    assert models.parse_size("1024x768") == (1024, 768)
    assert models.parse_size("1024X768") == (1024, 768)
    assert models.parse_size("1024×768") == (1024, 768)


@pytest.mark.parametrize("bad", ["big", "1024", "1024x", "x768", "-100x100"])
def test_parse_size_rejects_garbage(bad: str) -> None:
    with pytest.raises(InvalidParameterError) as excinfo:
        models.parse_size(bad)
    assert excinfo.value.param == "size"


# ---------------------------------------------------------------------------
# 归一化：模型解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["t2i", "T2I", "文生图", "生图", "image-t2i", "txt2img"])
def test_model_aliases_are_case_insensitive(alias: str) -> None:
    resolved, _ = models.resolve_model(alias)
    assert resolved == models.MODEL_T2I


def test_auto_model_is_reported_as_derived() -> None:
    resolved, note = models.resolve_model("auto")
    assert resolved == models.MODEL_T2I
    assert note and "自动推导" in note


def test_placeholder_models_are_not_treated_as_intent() -> None:
    resolved, note = models.resolve_model("dall-e-3")
    assert resolved == models.DEFAULT_MODEL
    assert note and "占位名" in note


def test_unknown_model_is_rejected() -> None:
    with pytest.raises(InvalidParameterError) as excinfo:
        models.resolve_model("my-private-model")
    assert excinfo.value.param == "model"


def test_missing_model_leaves_a_trail() -> None:
    resolved, note = models.resolve_model(None)
    assert resolved == models.DEFAULT_MODEL and note is not None


# ---------------------------------------------------------------------------
# 归一化：整体
# ---------------------------------------------------------------------------


def test_normalize_keeps_prompt_and_ratio() -> None:
    normalized = models.normalize(models.GenerationRequest(prompt="  cat  ", aspect_ratio="16:9"))
    assert normalized.prompt == "cat"
    assert normalized.aspect_ratio == "16:9"


def test_normalize_rejects_split_reference_image_types() -> None:
    with pytest.raises(InvalidParameterError):
        models.normalize(models.GenerationRequest(prompt="cat", image={"url": "x"}))
    with pytest.raises(InvalidParameterError):
        models.normalize(models.GenerationRequest(prompt="cat", image=["u"]))


def test_normalize_unknown_field_name_is_in_the_error() -> None:
    with pytest.raises(InvalidParameterError) as excinfo:
        models.normalize(models.GenerationRequest.model_validate({"prompt": "cat", "steps": 30}))
    assert excinfo.value.param == "steps"


# ---------------------------------------------------------------------------
# 任务库
# ---------------------------------------------------------------------------


def test_task_ids_are_unique_and_prefixed() -> None:
    ids = {new_task_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("imagefree_") and len(i) == len("imagefree_") + 32 for i in ids)


def test_fingerprint_is_stable_and_irreversible() -> None:
    assert fingerprint_key("abc") == fingerprint_key("abc")
    assert fingerprint_key("abc") != fingerprint_key("abd")
    assert "abc" not in fingerprint_key("abc")


def test_create_and_fetch_roundtrip(store: TaskStore) -> None:
    row = store.create_task(
        task_id="imagefree_x",
        key_fingerprint="fp",
        model=models.MODEL_T2I,
        prompt="cat",
        aspect_ratio="1:1",
        degradations=["默认比例"],
    )
    assert row.status == QUEUED
    fetched = store.get_task("imagefree_x")
    assert fetched is not None and fetched.degradations == ["默认比例"]


def test_list_is_scoped_by_key(store: TaskStore) -> None:
    for idx, fp in enumerate(["a", "a", "b"]):
        store.create_task(
            task_id=f"imagefree_{idx}",
            key_fingerprint=fp,
            model=models.MODEL_T2I,
            prompt="cat",
            aspect_ratio="1:1",
            degradations=None,
        )
    assert len(store.list_tasks("a")) == 2
    assert len(store.list_tasks("b")) == 1
    assert len(store.list_tasks("c")) == 0


def test_counts_split_active_and_in_flight(store: TaskStore) -> None:
    store.create_task(
        task_id="imagefree_a", key_fingerprint=None, model=models.MODEL_T2I,
        prompt="cat", aspect_ratio="1:1", degradations=None,
    )
    store.create_task(
        task_id="imagefree_b", key_fingerprint=None, model=models.MODEL_T2I,
        prompt="cat", aspect_ratio="1:1", degradations=None,
    )
    store.update_task("imagefree_b", status=IN_PROGRESS, upstream_task_id="u1")
    assert store.count_active() == 2
    assert store.count_in_flight() == 1, "闸门只看**已提交上游**的任务"
    store.mark_terminal("imagefree_b", status=SUCCESS, image_url=IMAGE_URL)
    assert store.count_in_flight() == 0
    assert store.count_by_status()[SUCCESS] == 1


def test_claim_respects_next_poll_time(store: TaskStore) -> None:
    now = utcnow()
    store.create_task(
        task_id="imagefree_later", key_fingerprint=None, model=models.MODEL_T2I,
        prompt="cat", aspect_ratio="1:1", degradations=None,
    )
    store.update_task("imagefree_later", next_poll_at=now + timedelta(seconds=60))
    assert store.claim_due_tasks(owner="w1", limit=5, lease_seconds=30, now=now) == []
    claimed = store.claim_due_tasks(
        owner="w1", limit=5, lease_seconds=30, now=now + timedelta(seconds=61)
    )
    assert [row.id for row in claimed] == ["imagefree_later"]


def test_claim_does_not_steal_a_live_lease(store: TaskStore) -> None:
    now = utcnow()
    store.create_task(
        task_id="imagefree_leased", key_fingerprint=None, model=models.MODEL_T2I,
        prompt="cat", aspect_ratio="1:1", degradations=None, now=now,
    )
    assert store.claim_due_tasks(owner="w1", limit=5, lease_seconds=30, now=now)
    assert store.claim_due_tasks(owner="w2", limit=5, lease_seconds=30, now=now) == []
    # 租约过期后可以被别人接管（worker 崩了以后的自愈）
    assert store.claim_due_tasks(
        owner="w2", limit=5, lease_seconds=30, now=now + timedelta(seconds=31)
    )


def test_release_expired_leases_returns_task_to_pool(store: TaskStore) -> None:
    now = utcnow()
    store.create_task(
        task_id="imagefree_stuck", key_fingerprint=None, model=models.MODEL_T2I,
        prompt="cat", aspect_ratio="1:1", degradations=None, now=now,
    )
    store.claim_due_tasks(owner="w1", limit=1, lease_seconds=30, now=now)
    released = store.release_expired_leases(now=now + timedelta(seconds=31))
    assert released == 1
    row = store.get_task("imagefree_stuck")
    assert row is not None and row.lease_owner is None


def test_purge_deletes_only_old_terminal_tasks(store: TaskStore) -> None:
    now = utcnow()
    for task_id in ("imagefree_old", "imagefree_new", "imagefree_active"):
        store.create_task(
            task_id=task_id, key_fingerprint=None, model=models.MODEL_T2I,
            prompt="cat", aspect_ratio="1:1", degradations=None,
        )
    store.mark_terminal("imagefree_old", status=FAILURE, error_code="task_failed", error_message="x")
    store.mark_terminal("imagefree_new", status=SUCCESS, image_url=IMAGE_URL)
    store.update_task(
        "imagefree_old", finished_at=now - timedelta(days=10)
    )
    deleted = store.purge_terminal_older_than(7, now=now)
    assert deleted == 1
    assert store.get_task("imagefree_old") is None
    assert store.get_task("imagefree_new") is not None
    assert store.get_task("imagefree_active") is not None


# ---------------------------------------------------------------------------
# 协调器：提交 / 查询 / 收口
# ---------------------------------------------------------------------------


def test_tick_submits_queued_task(store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream) -> None:
    task_id = _accept(_service(store, settings))
    summary = _coordinator(store, client, settings).tick()
    assert summary["submitted"] == 1
    row = store.get_task(task_id)
    assert row is not None
    assert row.status == IN_PROGRESS and row.upstream_task_id == SUBMITTED_TASK_ID
    assert len(fake.submit_calls) == 1


def test_tick_polls_until_success(store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream) -> None:
    fake.script_status(
        SUBMITTED_TASK_ID,
        {"status": "pending", "progress": 20},
        {"status": "processing", "progress": 70},
        {"status": "completed", "image": IMAGE_URL, "progress": 100},
    )
    task_id = _accept(_service(store, settings))
    coord = _coordinator(store, client, settings)
    base = utcnow()
    coord.tick(now=base)  # 提交
    coord.tick(now=base + timedelta(seconds=1))  # 第一次查询 → pending
    assert store.get_task(task_id).status == IN_PROGRESS  # type: ignore[union-attr]
    coord.tick(now=base + timedelta(seconds=7))  # 第二次查询 → processing
    coord.tick(now=base + timedelta(seconds=13))  # 第三次查询 → completed
    row = store.get_task(task_id)
    assert row is not None and row.status == SUCCESS and row.image_url == IMAGE_URL


def test_success_response_carries_the_degradation_trail(store: TaskStore, settings: Settings) -> None:
    service = _service(store, settings)
    task_id = _accept(service, size="1000x1000", quality="high")
    _, body = service.get(task_id)
    assert any("aspect_ratio=1:1" in d for d in body["degradations"])
    assert any("quality" in d for d in body["degradations"])


def test_tick_times_out_a_stuck_task(
    store: TaskStore, client: ImageFreeClient, make_settings: Callable[..., Settings], fake: FakeUpstream
) -> None:
    settings = make_settings(task_timeout=10)
    task_id = _accept(_service(store, settings))
    coord = _coordinator(store, client, settings)
    base = utcnow()
    coord.tick(now=base)                                  # 提交
    summary = coord.tick(now=base + timedelta(seconds=11))  # 超时
    assert summary["finalized"] == 1
    row = store.get_task(task_id)
    assert row is not None and row.status == FAILURE
    assert row.error_code == "task_timeout"
    assert "可能" in (row.error_message or ""), "不能断言上游已经停了"


def test_content_policy_fails_immediately_without_retry(
    store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream
) -> None:
    fake.fail_submit("CONTENT_BLOCKED", "prompt violates the safety policy")
    task_id = _accept(_service(store, settings))
    coord = _coordinator(store, client, settings)
    summary = coord.tick()
    assert summary["finalized"] == 1
    row = store.get_task(task_id)
    assert row is not None and row.status == FAILURE
    assert row.error_code == "content_policy_violation"


def test_turnstile_requirement_is_a_loud_deployment_failure(
    store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream
) -> None:
    fake.fail_submit("TURNSTILE_REQUIRED", "please verify you are human (turnstile)")
    task_id = _accept(_service(store, settings))
    _coordinator(store, client, settings).tick()
    row = store.get_task(task_id)
    assert row is not None and row.status == FAILURE
    assert row.error_code == "upstream_turnstile_required"


def test_upstream_busy_is_retried_not_failed(
    store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream
) -> None:
    """上游在途互斥 = "等一会儿再来"，**不是**任务失败。

    单出口时（没配代理）没有别的出口可换 ⇒ 走退避，tick 摘要记为 `wall_blocked`。
    多出口时同一个错误会触发**换出口重试**（见 `tests/test_egress.py`）。
    """
    fake.fail_submit("FREE_TASK_IP_ACTIVE", "network task limit reached")
    task_id = _accept(_service(store, settings))
    coord = _coordinator(store, client, settings)
    summary = coord.tick()
    assert summary["wall_blocked"] == 1
    assert summary["finalized"] == 0
    row = store.get_task(task_id)
    assert row is not None and row.status == QUEUED
    assert row.error_code == "upstream_ip_task_active"
    assert row.next_poll_at is not None and row.next_poll_at > utcnow()
    # 退避之后上游空了 ⇒ 这次能提交成功
    fake.submit_override = None
    coord.tick(now=row.next_poll_at + timedelta(seconds=1))
    assert store.get_task(task_id).status == IN_PROGRESS  # type: ignore[union-attr]


def test_poll_failure_is_retried_not_failed(
    store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream
) -> None:
    task_id = _accept(_service(store, settings))
    coord = _coordinator(store, client, settings)
    base = utcnow()
    coord.tick(now=base)
    fake.status_raises = httpx_timeout()
    summary = coord.tick(now=base + timedelta(seconds=1))
    assert summary["retry_scheduled"] == 1
    row = store.get_task(task_id)
    assert row is not None and row.status == IN_PROGRESS


def httpx_timeout() -> Exception:
    import httpx

    return httpx.ConnectTimeout("boom")


def test_retry_backoff_grows_with_attempts(store: TaskStore, client: ImageFreeClient, settings: Settings) -> None:
    coord = _coordinator(store, client, settings)
    store.create_task(
        task_id="imagefree_bo", key_fingerprint=None, model=models.MODEL_T2I,
        prompt="cat", aspect_ratio="1:1", degradations=None,
    )
    delays = []
    for attempts in (1, 2, 3, 4, 99):
        store.update_task("imagefree_bo", attempts=attempts)
        delays.append(coord.retry_delay(store.get_task("imagefree_bo")))
    assert delays[0] < delays[1] < delays[2] < delays[3]
    assert delays[3] == delays[4] == max(RETRY_BACKOFF_SECONDS)


def test_gate_blocks_second_submission_while_one_is_in_flight(
    store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream
) -> None:
    """上游是在途互斥的 ⇒ 第二个任务必须等，不能撞上去（撞了就是 429）。"""
    service = _service(store, settings)
    first = _accept(service)
    second = _accept(service)
    coord = _coordinator(store, client, settings)
    base = utcnow()
    assert coord.tick(now=base)["submitted"] == 1
    summary = coord.tick(now=base + timedelta(seconds=1))
    assert summary["submitted"] == 0
    assert store.get_task(first).status == IN_PROGRESS  # type: ignore[union-attr]
    assert store.get_task(second).status == QUEUED  # type: ignore[union-attr]
    assert len(fake.submit_calls) == 1, "被闸门挡住时**一个上游请求都不许发**"


def test_gate_min_interval(settings: Settings) -> None:
    gate = SubmitGate(settings)
    now = utcnow()
    gate.note_submit(now)
    allowed, reason = gate.can_submit(in_flight=0, now=now + timedelta(seconds=1))
    assert allowed, "默认 IF_MIN_INTERVAL=0 ⇒ 不限"
    gate = SubmitGate(settings.model_copy(update={"if_min_interval": 60.0}))
    gate.note_submit(now)
    allowed, reason = gate.can_submit(in_flight=0, now=now + timedelta(seconds=1))
    assert not allowed and reason is not None


def test_gate_per_minute_cap(settings: Settings) -> None:
    gate = SubmitGate(settings.model_copy(update={"if_per_minute": 2}))
    now = utcnow()
    for offset in (0, 10, 20):
        allowed, _ = gate.can_submit(in_flight=0, now=now + timedelta(seconds=offset))
        if allowed:
            gate.note_submit(now + timedelta(seconds=offset))
    allowed, reason = gate.can_submit(in_flight=0, now=now + timedelta(seconds=30))
    assert not allowed and reason is not None


def test_tick_purges_expired_terminal_tasks(store: TaskStore, client: ImageFreeClient, settings: Settings, fake: FakeUpstream) -> None:
    fake.complete()
    task_id = _accept(_service(store, settings))
    coord = _coordinator(store, client, settings)
    base = utcnow()
    coord.tick(now=base)
    coord.tick(now=base + timedelta(seconds=1))
    store.update_task(task_id, finished_at=base - timedelta(days=30))
    # 换一个**新的**协调器：清理每小时最多一次（进程内节流），
    # 同一个实例的第二轮 tick 不会重复清理。
    fresh = _coordinator(store, client, settings)
    summary = fresh.tick(now=base + timedelta(seconds=2))
    assert summary["purged"] == 1
    assert store.get_task(task_id) is None
