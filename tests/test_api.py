#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外契约测试：**逐条钉 docs/INTERFACE.md**。

契约文档改了而这里没改 ⇒ 这些用例会红。这是刻意的：
契约是给调用方看的，不能让实现偷偷漂移。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import TaskStore, utcnow
from app.upstream import ImageFreeClient
from tests.conftest import IMAGE_URL, SUBMITTED_TASK_ID, FakeUpstream

ACCEPT_PATH = "/async/v1/images/generations"


def _accept(api: TestClient, **payload: Any) -> Any:
    body = {"prompt": "a cat on a windowsill"}
    body.update(payload)
    return api.post(ACCEPT_PATH, json=body)


def _drive(app: Any, *, poll_after: float = 5.0) -> None:
    """手动推进协调器：提交 → 等一会儿 → 查询。等价于"把时间拨快"。"""
    coord = app.state.coordinator
    base = utcnow()
    coord.tick(now=base)
    coord.tick(now=base + timedelta(seconds=poll_after))


# ---------------------------------------------------------------------------
# §1 受理
# ---------------------------------------------------------------------------


def test_accept_returns_exactly_one_key(api: TestClient) -> None:
    resp = _accept(api)
    assert resp.status_code == 202
    assert set(resp.json()) == {"task_id"}, "🔴 多一个键就是契约变更"
    assert resp.json()["task_id"].startswith("imagefree_")
    assert resp.headers["location"] == f"{ACCEPT_PATH}/{resp.json()['task_id']}"


def test_accept_does_not_touch_upstream(api: TestClient, fake: FakeUpstream) -> None:
    """受理内零上游往返 —— 上游抖动不该传染给受理响应。"""
    _accept(api)
    assert fake.submit_calls == []


def test_missing_prompt_is_400_invalid_parameter(api: TestClient) -> None:
    resp = api.post(ACCEPT_PATH, json={"aspect_ratio": "1:1"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "invalid_parameter"
    assert err["param"] == "prompt"


def test_blank_prompt_is_400(api: TestClient) -> None:
    resp = _accept(api, prompt="   ")
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "prompt"


def test_non_object_body_is_400(api: TestClient) -> None:
    resp = api.post(ACCEPT_PATH, json=["not", "an", "object"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_parameter"


def test_unknown_field_is_400_with_the_field_name(api: TestClient) -> None:
    """未知字段 = "你写错了" ⇒ 4xx，并指出字段名。"""
    resp = _accept(api, response_format_typo="url")
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "response_format_typo"


def test_recognized_but_unsupported_field_is_degradation_not_error(api: TestClient) -> None:
    """「上游没有」≠「你写错了」：认得但做不到 ⇒ 不报错，留痕。"""
    task_id = _accept(api, quality="high", style="vivid").json()["task_id"]
    body = api.get(f"{ACCEPT_PATH}/{task_id}").json()
    assert "degradations" in body
    joined = " ".join(body["degradations"])
    assert "quality" in joined and "style" in joined


# ---------------------------------------------------------------------------
# §3.3 / §4 刻意缺席的能力
# ---------------------------------------------------------------------------


def test_reference_image_is_rejected_loudly(api: TestClient) -> None:
    resp = _accept(api, image=["https://example.com/ref.png"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["param"] == "image"
    assert "图生图" in err["message"]


def test_reference_image_as_string_gets_the_correct_usage(api: TestClient) -> None:
    resp = _accept(api, image="https://example.com/ref.png")
    assert resp.status_code == 400
    assert "数组" in resp.json()["error"]["message"]


def test_empty_image_array_is_fine(api: TestClient) -> None:
    task_id = _accept(api, image=[]).json()["task_id"]
    body = api.get(f"{ACCEPT_PATH}/{task_id}").json()
    assert any("无参考图" in d for d in body["degradations"])


@pytest.mark.parametrize("n", [0, 2, 10])
def test_n_other_than_one_is_rejected(api: TestClient, n: int) -> None:
    resp = _accept(api, n=n)
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "n"


def test_n_equal_one_is_accepted(api: TestClient) -> None:
    assert _accept(api, n=1).status_code == 202


# ---------------------------------------------------------------------------
# §1 比例与 size 换算
# ---------------------------------------------------------------------------


def test_size_is_converted_to_the_nearest_ratio_with_a_trail(api: TestClient) -> None:
    task_id = _accept(api, size="1024x1024").json()["task_id"]
    body = api.get(f"{ACCEPT_PATH}/{task_id}").json()
    assert any("aspect_ratio=1:1" in d for d in body["degradations"])
    # 🔴 留痕必须写到**实际产出像素**，否则调用方不知道会拿到多大
    assert any("1024×1024" in d for d in body["degradations"])


def test_bad_size_is_400(api: TestClient) -> None:
    resp = _accept(api, size="big")
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "size"


def test_unsupported_ratio_is_400(api: TestClient) -> None:
    resp = _accept(api, aspect_ratio="21:9")
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "aspect_ratio"


def test_native_ratio_wins_over_size_and_says_so(api: TestClient) -> None:
    task_id = _accept(api, aspect_ratio="16:9", size="1024x1024").json()["task_id"]
    body = api.get(f"{ACCEPT_PATH}/{task_id}").json()
    assert any("以 aspect_ratio 为准" in d for d in body["degradations"])


def test_default_ratio_is_reported_as_a_degradation(api: TestClient) -> None:
    """默认值代入也是降级 —— 调用方要知道它没指定比例。"""
    task_id = _accept(api).json()["task_id"]
    body = api.get(f"{ACCEPT_PATH}/{task_id}").json()
    assert any("aspect_ratio=1:1" in d for d in body["degradations"])


# ---------------------------------------------------------------------------
# §1 受理响应里**没有** degradations（冻结成只有 task_id）
# ---------------------------------------------------------------------------


def test_accept_response_never_carries_degradations(api: TestClient) -> None:
    resp = _accept(api, size="1000x1000")
    assert set(resp.json()) == {"task_id"}


# ---------------------------------------------------------------------------
# §2 查询
# ---------------------------------------------------------------------------


def test_queued_task_returns_202(api: TestClient) -> None:
    task_id = _accept(api).json()["task_id"]
    resp = api.get(f"{ACCEPT_PATH}/{task_id}")
    assert resp.status_code == 202
    assert resp.json()["status"] in {"queued", "in_progress"}
    assert resp.json()["task_id"] == task_id


def test_success_returns_only_url_and_created(api: TestClient, app: Any, fake: FakeUpstream) -> None:
    fake.complete()
    task_id = _accept(api).json()["task_id"]
    _drive(app)
    resp = api.get(f"{ACCEPT_PATH}/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) <= {"status", "data", "created", "degradations"}, (
        "🔴 契约冻结：成功响应只允许 status / data / created（+ 非空时的 degradations）"
    )
    assert body["status"] == "completed", "终态必须显式给 status（对齐 new-api，全小写）"
    assert set(body["data"][0]) == {"url"}, "🔴 data[] 里只有 url"
    assert body["data"][0]["url"] == IMAGE_URL
    assert isinstance(body["created"], int)


def test_failure_returns_200_not_4xx(api: TestClient, app: Any, fake: FakeUpstream) -> None:
    """失败也回 200：任务跑完了，只是结果是失败。回 4xx 会误触发重试。"""
    fake.script_status(SUBMITTED_TASK_ID, {"status": "failed"})
    task_id = _accept(api).json()["task_id"]
    _drive(app)
    resp = api.get(f"{ACCEPT_PATH}/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "task_failed"
    assert body["task_id"] == task_id


def test_unknown_task_is_404(api: TestClient) -> None:
    resp = api.get(f"{ACCEPT_PATH}/imagefree_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


# ---------------------------------------------------------------------------
# §2.5 对外 status 词汇（对齐 new-api 的 task 状态，全小写）
# ---------------------------------------------------------------------------


def test_public_status_vocabulary_is_lowercase_and_newapi_aligned() -> None:
    """🔴 词汇守卫：取值就是 new-api 那套（全小写），且映射**不得退化成恒等**。

    依据 new-api `relaykit/dto/openai_video.go::VideoStatus*`：
    `queued` / `in_progress` / `completed` / `failed`（内部 SUCCESS/FAILURE 渲染成后两者）。
    """
    from app.service import PUBLIC_STATUS
    from app.store import FAILURE, IN_PROGRESS, QUEUED, SUCCESS

    # ① 覆盖完整性：库内四个状态**必须都有**映射（将来加状态时这里会红）
    assert set(PUBLIC_STATUS) == {QUEUED, IN_PROGRESS, SUCCESS, FAILURE}
    # ② 词汇表就是 new-api 那套
    assert set(PUBLIC_STATUS.values()) == {"queued", "in_progress", "completed", "failed"}
    # ③ 全小写
    for public in PUBLIC_STATUS.values():
        assert public == public.lower(), f"{public!r} 必须全小写"
    # ④ 与具体取值无关的结构判据：**终态**两套词表必须**不同名** ——
    #    有人把映射改成直通（对外直接吐 "success"/"failure"）时只有这条会红。
    #    ⚠️ 非终态（queued / in_progress）两套词表**本来就同名**，那是有意的，不进判据。
    for terminal in (SUCCESS, FAILURE):
        assert PUBLIC_STATUS[terminal] != terminal, (
            f"终态 {terminal!r} 的映射退化成了恒等 —— 对外必须是 completed / failed"
        )
    assert PUBLIC_STATUS[SUCCESS] == "completed"
    assert PUBLIC_STATUS[FAILURE] == "failed"


def test_public_status_rejects_states_outside_the_vocabulary() -> None:
    """库内冒出词表外状态 ⇒ **响亮失败**，绝不静默透出（那会让调用方以为任务还在跑）。"""
    from app.service import public_status

    with pytest.raises(RuntimeError, match="未知的库内状态"):
        public_status("weird_state")


# ---------------------------------------------------------------------------
# §2.6 终态契约（终态最重要：与 HTTP 码不许矛盾，也不许被伪装成非终态）
# ---------------------------------------------------------------------------


def test_202_and_200_agree_with_terminality(api: TestClient, app: Any, fake: FakeUpstream) -> None:
    """`202` ⇔ 非终态、`200` ⇔ 终态 —— status 与 HTTP 码永远不许矛盾。"""
    task_id = _accept(api).json()["task_id"]
    pending = api.get(f"{ACCEPT_PATH}/{task_id}")
    assert pending.status_code == 202
    assert pending.json()["status"] in {"queued", "in_progress"}

    fake.complete()
    _drive(app)
    done = api.get(f"{ACCEPT_PATH}/{task_id}")
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["data"][0]["url"], "终态成功**必然**带 url（不会出现 completed 却没图）"


def test_terminal_row_is_never_claimed_again(store: TaskStore) -> None:
    """🔴 终态**不可回退**：收口之后协调器再也抢不到它（不会被改回非终态）。

    这是「终态」的定义性保障 —— 谁把 `ACTIVE_STATUSES` 放宽，这条会红。
    """
    from app.store import SUCCESS

    now = utcnow()
    store.create_task(
        task_id="imagefree_frozen",
        key_fingerprint=None,
        model="image-t2i",
        prompt="p",
        aspect_ratio="1:1",
        degradations=None,
        now=now,
    )
    assert store.claim_due_tasks(owner="w1", limit=1, lease_seconds=30, now=now)
    store.mark_terminal("imagefree_frozen", status=SUCCESS, image_url="https://x/y.png", now=now)
    # 即便把时间拨到很远（任何退避都早该到点），终态任务也不再进候选池。
    later = now + timedelta(hours=6)
    assert store.claim_due_tasks(owner="w2", limit=5, lease_seconds=30, now=later) == []


def test_unknown_internal_status_fails_loudly_instead_of_looking_queued(
    api: TestClient, app: Any
) -> None:
    """🔴 库内出现词表外状态 ⇒ **响亮失败**，绝不静默兜成 `queued`。

    静默兜底会把「未知」说成「还没好」，调用方会**永远轮询** —— 终态被伪装成非终态，
    这是终态最不能出的错。单查与列表两条路径必须是同一行为。
    """
    task_id = _accept(api).json()["task_id"]
    app.state.store.update_task(task_id, status="weird_state")  # 直接污染库（模拟脏数据/漏迁移）
    with pytest.raises(RuntimeError, match="未知的库内状态"):
        api.get(f"{ACCEPT_PATH}/{task_id}")


def test_terminal_result_cannot_be_overwritten(store: TaskStore) -> None:
    """🔴 终态**不可覆盖**：迟到的收口（租约交接 / 慢响应 / 并发副本）不能让**已 announced**
    的结果翻面 —— 调用方可能已经拿着 `completed` + url 走了。"""
    from app.store import FAILURE, SUCCESS

    now = utcnow()
    store.create_task(
        task_id="imagefree_announced",
        key_fingerprint=None,
        model="image-t2i",
        prompt="p",
        aspect_ratio="1:1",
        degradations=None,
        now=now,
    )
    assert (
        store.mark_terminal(
            "imagefree_announced", status=SUCCESS, image_url="https://x/y.png", now=now
        )
        is True
    )
    # 第二刀（迟到的 failure）必须被**忽略**，并如实返回 False
    assert (
        store.mark_terminal(
            "imagefree_announced",
            status=FAILURE,
            error_code="task_failed",
            error_message="late",
            now=now,
        )
        is False
    ), "终态不得被覆盖"
    row = store.get_task("imagefree_announced")
    assert row is not None
    assert row.status == SUCCESS and row.image_url == "https://x/y.png"


def test_success_without_url_refuses_to_render(api: TestClient, app: Any) -> None:
    """🔴「成功但没图」的自相矛盾数据 ⇒ **响亮失败**，绝不渲染成 `completed` + `url: null`。"""
    task_id = _accept(api).json()["task_id"]
    app.state.store.update_task(task_id, status="success", image_url=None)
    with pytest.raises(RuntimeError, match="却没有产物 URL"):
        api.get(f"{ACCEPT_PATH}/{task_id}")


def test_get_does_not_require_authorization(make_settings: Callable[..., Settings], fake: FakeUpstream) -> None:
    """id 本身就是凭据（128 位随机），所以单任务查询不强制鉴权。"""
    settings = make_settings(api_keys="secret-key")
    with TestClient(
        create_app(
            settings=settings,
            store=TaskStore(settings.task_db),
            client=ImageFreeClient(settings, transport=fake.transport()),
            start_coordinator=False,
        )
    ) as api:
        assert api.get(f"{ACCEPT_PATH}/imagefree_nope").status_code == 404  # 不是 401


# ---------------------------------------------------------------------------
# §2.4 鉴权语义：三种情况分得很清
# ---------------------------------------------------------------------------


@pytest.fixture
def secured(make_settings: Callable[..., Settings], fake: FakeUpstream) -> Any:
    settings = make_settings(api_keys="good-key,another")
    app = create_app(
        settings=settings,
        store=TaskStore(settings.task_db),
        client=ImageFreeClient(settings, transport=fake.transport()),
        start_coordinator=False,
    )
    with TestClient(app) as client:
        yield client


def test_accept_without_key_is_401_when_auth_enabled(secured: TestClient) -> None:
    assert secured.post(ACCEPT_PATH, json={"prompt": "cat"}).status_code == 401


def test_accept_with_wrong_key_is_401(secured: TestClient) -> None:
    resp = secured.post(
        ACCEPT_PATH, json={"prompt": "cat"}, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


def test_accept_with_good_key_works(secured: TestClient) -> None:
    resp = secured.post(
        ACCEPT_PATH, json={"prompt": "cat"}, headers={"Authorization": "Bearer good-key"}
    )
    assert resp.status_code == 202


def test_list_requires_authorization(secured: TestClient) -> None:
    assert secured.get(ACCEPT_PATH).status_code == 401


def test_delete_requires_authorization(secured: TestClient) -> None:
    assert secured.delete(f"{ACCEPT_PATH}/imagefree_x").status_code == 401


def test_task_list_is_scoped_to_the_key(secured: TestClient) -> None:
    mine = secured.post(
        ACCEPT_PATH, json={"prompt": "cat"}, headers={"Authorization": "Bearer good-key"}
    ).json()["task_id"]
    theirs = secured.get(ACCEPT_PATH, headers={"Authorization": "Bearer another"}).json()
    assert theirs["data"] == []
    mine_list = secured.get(ACCEPT_PATH, headers={"Authorization": "Bearer good-key"}).json()
    assert [item["task_id"] for item in mine_list["data"]] == [mine]


def test_cross_key_delete_is_404_and_local_only(secured: TestClient) -> None:
    task_id = secured.post(
        ACCEPT_PATH, json={"prompt": "cat"}, headers={"Authorization": "Bearer good-key"}
    ).json()["task_id"]
    resp = secured.delete(f"{ACCEPT_PATH}/{task_id}", headers={"Authorization": "Bearer another"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# §8 删除
# ---------------------------------------------------------------------------


def test_delete_active_task_is_loud_failure(api: TestClient) -> None:
    """上游没有取消端点 ⇒ 未终态删除必须响亮失败。"""
    task_id = _accept(api).json()["task_id"]
    resp = api.delete(f"{ACCEPT_PATH}/{task_id}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "task_not_deletable"


def test_delete_terminal_task_succeeds(api: TestClient, app: Any, fake: FakeUpstream) -> None:
    fake.complete()
    task_id = _accept(api).json()["task_id"]
    _drive(app)
    resp = api.delete(f"{ACCEPT_PATH}/{task_id}")
    assert resp.status_code == 200
    assert resp.json() == {"task_id": task_id, "status": "deleted"}
    assert api.get(f"{ACCEPT_PATH}/{task_id}").status_code == 404


# ---------------------------------------------------------------------------
# §3 能力清单 / 运维端点
# ---------------------------------------------------------------------------


def test_capability_registry_exposes_availability_and_enable_hints(api: TestClient) -> None:
    """能力全集（`/capabilities` 的 `capability` 段）：i2i 已启用；upscale **列出来但明说不可用**。

    🔴 2026-09-22 起 `/async/v1/models` 已移除（`/v1/models` 只列可调用模型）
    ⇒「未启用能力 + 原因 + 开启方式」的可发现性**只**在这里，必须有用例钉住。
    """
    body = api.get("/capabilities").json()["capability"]
    assert body["object"] == "list"
    by_id = {m["id"]: m for m in body["data"]}
    assert set(by_id) == {"image-t2i", "image-i2i", "image-upscale"}
    model = by_id["image-t2i"]
    assert model["available"] is True
    assert model["id"] == "image-t2i"
    assert model["aspect_ratios"] == ["1:1", "3:4", "4:3", "9:16", "16:9"]
    assert model["aspect_ratio_pixels"]["1:1"] == "1024×1024"
    assert model["accepts_reference_images"] is False
    assert model["size_is_advisory"] is True, "size 只影响选档，不改像素"

    i2i = by_id["image-i2i"]
    assert i2i["available"] is True
    assert i2i["capability"] == "image_to_image"
    assert i2i["accepts_reference_images"] is True
    assert i2i["max_reference_images"] == 1
    assert i2i["upload_limit_mb"] == 10

    upscale = by_id["image-upscale"]
    assert upscale["available"] is False
    assert "Turnstile" in upscale["reason"]
    assert "IMAGEFREE_TURNSTILE_TOKEN" in upscale["enable"], "必须写明开启方式"


def test_healthz_is_zero_dependency(api: TestClient) -> None:
    assert api.get("/healthz").json() == {"status": "ok"}


def test_i2i_without_image_is_400_and_never_touches_upstream(
    api: TestClient, fake: FakeUpstream
) -> None:
    """model=image-i2i 但没给参考图 ⇒ 400（调用方写错了）；且闸门在上游之前，零请求。"""
    resp = api.post(ACCEPT_PATH, json={"prompt": "cat", "model": "image-i2i"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "invalid_parameter"
    assert err["param"] == "image"
    assert fake.submit_calls == [] and fake.i2i_upload_url_calls == []
    assert fake.i2i_create_calls == [], "🔴 闸门在上游之前：一个请求都不许发"


def test_not_available_model_is_503_and_never_touches_upstream(
    api: TestClient, fake: FakeUpstream
) -> None:
    """upscale 仍是"枚举但未启用" ⇒ 503 部署状态（调用方没写错）。"""
    resp = api.post(ACCEPT_PATH, json={"prompt": "cat", "model": "image-upscale"})
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "capability_unavailable"
    assert err["type"] == "service_unavailable"
    assert "Turnstile" in err["message"]
    assert fake.submit_calls == [], "🔴 闸门在上游之前：一个请求都不许发"


def test_not_available_model_aliases_also_get_503(api: TestClient) -> None:
    """别名也要认：未启用能力的别名给 503（含原因），不认就会变成 400 假装拼错。"""
    for alias in ("upscale", "放大"):
        resp = api.post(ACCEPT_PATH, json={"prompt": "cat", "model": alias})
        assert resp.status_code == 503, alias
        assert resp.json()["error"]["code"] == "capability_unavailable"


def test_readyz_reports_db_and_upstream_config(api: TestClient) -> None:
    body = api.get("/readyz").json()
    assert body["status"] == "ready"
    assert body["db"]["ok"] is True
    assert body["upstream"]["base_url"].startswith("https://")
    assert "turnstile_token_configured" in body["upstream"]


def test_stats_reports_gate_and_counts(api: TestClient) -> None:
    _accept(api)
    body = api.get("/stats").json()
    assert body["tasks"]["queued"] == 1
    assert body["gate"]["if_concurrency"] == 1


def test_capabilities_lists_deliberate_absences(api: TestClient) -> None:
    body = api.get("/capabilities").json()
    names = {item["capability"] for item in body["deliberate_absences"]}
    # image_to_image 已于 2026-09-21 启用（model=image-i2i），不再是缺席项。
    assert {"multi_image_per_request", "cancel"} <= names
    assert "image_to_image" not in names
    assert body["config"]["auth_enabled"] is False


# ---------------------------------------------------------------------------
# §0.1 models 端点：单一路径 `/v1/models`（2026-09-22 收敛，**干净断裂**）
# ---------------------------------------------------------------------------


def test_openai_models_endpoint_has_openai_shape(api: TestClient) -> None:
    """`GET /v1/models`：**OpenAI Model 对象的最小形状** —— 四字段，不多不少。"""
    from app.models import MODEL_RELEASED_AT

    resp = api.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"], "模型清单不能是空的"
    for item in body["data"]:
        assert set(item) == {"id", "object", "created", "owned_by"}, (
            "只该有 OpenAI 的四个键 —— 塞自有字段就不叫「结构兼容」了"
        )
        assert item["object"] == "model"
        assert isinstance(item["id"], str) and item["id"]
        assert item["created"] == MODEL_RELEASED_AT
        assert item["owned_by"] == "imagefree"


def test_models_lists_exactly_the_available_models(api: TestClient) -> None:
    """单一数据源：`/v1/models` == `capability_payload()` 里 `available=True` 的那批。

    未启用能力（upscale）**不得**出现在这里：本端点是给「自动探测 → 自动选模型」用的，
    列一个调不通的 id 等于把 503 埋给调用方。可发现性由 `/capabilities` 负责。
    """
    from app.models import capability_payload

    ids = [m["id"] for m in api.get("/v1/models").json()["data"]]
    assert ids == [m["id"] for m in capability_payload()["data"] if m["available"]]
    assert "image-t2i" in ids and "image-i2i" in ids
    assert "image-upscale" not in ids


def test_models_form_must_not_degrade_to_the_capability_registry(api: TestClient) -> None:
    """🔴 与具体取值无关的结构判据：`/v1/models` 的条目**必须**与能力全集条目不同。

    有人图省事把端点改成直接返回 `capability_payload()`（形态退化）时这条会红 ——
    它约束的是两个形态之间的关系，把断言里的期望值一起改掉也绕不过它。
    """
    from app.models import capability_payload

    openai_item = api.get("/v1/models").json()["data"][0]
    rich_item = capability_payload()["data"][0]
    assert set(openai_item) != set(rich_item)
    assert "created" not in rich_item, "能力全集不是 OpenAI 形态，不该长 created"
    assert "capability" not in openai_item, "对外模型清单不许混入自有扩展字段"
    assert "aliases" not in openai_item


def test_models_route_is_v1_only_and_old_path_is_gone(api: TestClient) -> None:
    """路由表实查 + **干净断裂**：`/v1/models` 在，`/async/v1/models` **不在**（不做别名兼容）。

    测试绿 ≠ 路由表符合预期 —— 旧路由可能被留成兼容别名；再真打一次确认是 404。
    """
    paths = {getattr(route, "path", None) for route in api.app.routes}
    assert "/v1/models" in paths
    assert "/async/v1/models" not in paths, "旧路径已移除，不做别名兼容"
    assert api.get("/async/v1/models").status_code == 404
