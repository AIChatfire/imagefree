#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力注册表 + 请求归一化。

本文件是**上游能力在代码里的唯一投影**：比例档、模型名与别名、
「认得但做不到」的字段清单、刻意缺席的能力。改它等于改对外契约。

设计纪律：
  · 上游**没有**的东西一律**不进**能力表（不做假能力，见 DELIBERATE_ABSENCES）；
  · 「上游没有」⇒ 进 `degradations`；「你写错了」⇒ `400 invalid_parameter`；
  · 任何换算/吸附都返回一句**可读的留痕文案**，由调用方在同一次响应里看到。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from .errors import CapabilityUnavailable, InvalidParameterError

# ---------------------------------------------------------------------------
# 常量：上游能力（来源 docs/UPSTREAM.md §2.1，**取证过的**）
# ---------------------------------------------------------------------------

SERVICE_NAME = "imagefree"
TASK_PREFIX = f"{SERVICE_NAME}_"

#: 上游只认这 5 个比例。顺序即 tie-break 优先级（`1:1` 优先）。
ASPECT_RATIOS: tuple[str, ...] = ("1:1", "3:4", "4:3", "9:16", "16:9")

#: UI 标称像素（**仅用于文案与吸附说明**，上游请求体里没有宽高字段）。
ASPECT_RATIO_PIXELS: dict[str, str] = {
    "1:1": "1024×1024",
    "3:4": "768×1024",
    "4:3": "1024×768",
    "9:16": "576×1024",
    "16:9": "1024×576",
}

DEFAULT_ASPECT_RATIO = "1:1"

#: 本服务的能力名（固定，不随上游变）。
MODEL_T2I = "image-t2i"
MODEL_AUTO = "image-auto"
MODEL_I2I = "image-i2i"
MODEL_UPSCALE = "image-upscale"
DEFAULT_MODEL = MODEL_T2I

#: **已枚举但未启用**的能力（列出来但明说不可用，见 docs/INTERFACE.md §3.3）。
#: 🔴 与「完全不列出」的区别：调用方能**发现它存在**、知道原因与开启方式，
#: 而不是拿到 400 之后永远不去问。命中它们 ⇒ **503**（部署状态），不是 400。
#: 🔴 `MODEL_I2I` 已于 2026-09-21 启用（走 /api/ai-photo-editor），不再在此表。
NOT_AVAILABLE_MODELS: dict[str, dict[str, str]] = {
    MODEL_UPSCALE: {
        "capability": "image_upscale",
        "reason": "上游有 `/api/image-upscaler`（实测**固定 2×**：1024→2048；4K 需链式两次，"
        "见 docs/UPSTREAM.md §10.3），同样被工具端点的 Turnstile 强制校验挡住。",
        "enable": "同图生图：`IMAGEFREE_TURNSTILE_TOKEN` + 放大链路上线（上传限额仅 1MB，"
        "超过须先转 JPEG，见 docs/UPSTREAM.md §10.3）。",
    },
}

#: 别名 → 能力名。**大小写不敏感**。照抄 ../hailuo 的习惯：
#: 中文别名与第三方 SDK 的常见写法都认。
MODEL_ALIASES: dict[str, str] = {
    # t2i
    "t2i": MODEL_T2I,
    "text2image": MODEL_T2I,
    "text-to-image": MODEL_T2I,
    "txt2img": MODEL_T2I,
    "文生图": MODEL_T2I,
    "生图": MODEL_T2I,
    "image-t2i": MODEL_T2I,
    # auto（本服务下等同 t2i，但会留痕说明）
    "auto": MODEL_AUTO,
    "image-auto": MODEL_AUTO,
    "image": MODEL_AUTO,
    # 未启用能力的别名：**也要认** —— 认出来才能给 503（含原因）；
    # 不认的话调用方拿到 400 会以为拼错了，永远发现不了这条路。
    # 🔴 规范 id 自身也在表里（与 t2i 同一待遇），否则枚举值自己反而 400。
    "image-i2i": MODEL_I2I,
    "image-upscale": MODEL_UPSCALE,
    "i2i": MODEL_I2I,
    "image-to-image": MODEL_I2I,
    "edit": MODEL_I2I,
    "图生图": MODEL_I2I,
    "编辑": MODEL_I2I,
    "upscale": MODEL_UPSCALE,
    "放大": MODEL_UPSCALE,
    "高清": MODEL_UPSCALE,
}

#: 占位名 ⇒ 等价于"没写 model"。第三方 SDK 常硬编码这些值，
#: 它们**不代表调用意图**（照搬 ../hailuo 的结论）。
PLACEHOLDER_MODELS: frozenset[str] = frozenset(
    {
        "dall-e",
        "dall-e-2",
        "dall-e-3",
        "gpt-image-1",
        "gpt-image-1.5",
        "gpt-image-2",
        "flux",
        "flux-pro",
        "flux-schnell",
        "sdxl",
        "stable-diffusion",
        "seedream",
        "seedream-4.5",
        "midjourney",
        "mj",
        "nano-banana",
        "gemini-2.5-flash-image",
    }
)

#: 「认得但做不到」的字段：**不报错**，进 `degradations`（docs/INTERFACE.md §1）。
DEGRADABLE_FIELDS: frozenset[str] = frozenset(
    {
        "quality",
        "watermark",
        "response_format",
        "style",
        "stream",
        "user",
        "background",
        "output_format",
        "moderation",
        "sequential_image_generation",
        "seed",
        "negative_prompt",
    }
)

#: 刻意缺席的能力：**响亮失败**，不假装支持。`/capabilities` 会把它带出来。
#: 🔴 `image_to_image` 已于 2026-09-21 启用（model="image-i2i"），不再是缺席项。
DELIBERATE_ABSENCES: tuple[dict[str, str], ...] = (
    {
        "capability": "high_resolution_2k_4k",
        "request_example": '{"size": "2048x2048"}',
        "behavior": "吸附到 5 档比例（产出长边 ≈1024）+ 留痕写明实际像素",
        "reason": "生成端点**没有分辨率参数**（实测产出固定 1024 级）。站点另有"
        "`/api/image-upscaler`（超分放大，body 只有 image_url、**无倍率参数**），未接入"
        "⇒ 本服务**不承诺** 2K/4K 产物。见 docs/UPSTREAM.md §10。",
    },
    {
        "capability": "multi_image_per_request",
        "request_example": '{"n": 4}',
        "behavior": "400 invalid_parameter (param=n)",
        # ⚠️ 这条理由在 2026-09-21 被实测改写：不再是「必然撞墙」，而是「本服务不做隐式放大」。
        "reason": "上游一次任务只出一张图 ⇒ n>1 只能靠**提交 N 次上游任务**实现。"
        "本服务**不做这种隐式放大**：额度消耗会乘 N，且单 IP 只有 3 个在途名额"
        "（实测，见 docs/UPSTREAM.md §9），超出就得排队或加出口。需要多张请显式多次调用。",
    },
    {
        "capability": "model_selection",
        "request_example": '{"model": "flux"}',
        "behavior": "占位名走默认；非占位名且非别名 ⇒ 400",
        "reason": "上游未暴露模型选择，服务端用哪个模型**未取证** ⇒ 不编模型列表。",
    },
    {
        "capability": "cancel",
        "request_example": "DELETE /async/v1/images/generations/{id}（非终态）",
        "behavior": "400 task_not_deletable",
        "reason": "上游**没有取消端点**，只有查询。本地置删不会让上游停下来。",
    },
    {
        "capability": "credits_and_cost",
        "request_example": "—",
        "behavior": "响应里不给 usage",
        "reason": "上游是免费额度制，没有积分/费用数据可给 ⇒ 不编数字。",
    },
)


# ---------------------------------------------------------------------------
# 归一化结果
# ---------------------------------------------------------------------------


@dataclass
class NormalizedRequest:
    """受理请求归一化后的产物（**此时还没碰上游**）。"""

    prompt: str
    aspect_ratio: str
    model: str
    degradations: list[str] = field(default_factory=list)
    #: 图生图的参考图引用（http(s) URL 或 `data:image/…` URI）。
    #: 仅 `model == MODEL_I2I` 时非空；下载与上传都在协调器里做（受理零上游往返）。
    image_ref: str | None = None

    @property
    def is_i2i(self) -> bool:
        return self.model == MODEL_I2I

    @property
    def turnstile_required_hint(self) -> str | None:
        return None


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class GenerationRequest(BaseModel):
    """受理请求。

    `extra="allow"` 是**刻意**的：未知字段要由我们分成两类
    （"上游没有" vs "你写错了"），pydantic 的 422 做不到这个区分。
    """

    model_config = ConfigDict(extra="allow")

    prompt: str
    model: str | None = None
    image: Any = None
    n: int | None = None
    aspect_ratio: str | None = None
    size: str | None = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$", re.IGNORECASE)


def ratio_value(ratio: str) -> float:
    """`"16:9"` → 1.777…"""
    w, h = ratio.split(":")
    return float(w) / float(h)


def nearest_aspect_ratio(width: int, height: int) -> str:
    """按**对数距离**吸附到最近的档位（宽高比是对数量级，不是线性量级）。

    tie 时取 `ASPECT_RATIOS` 里靠前的（`1:1` 优先）——确定性优先于"聪明"。
    """
    target = math.log(width / height)
    best, best_dist = ASPECT_RATIOS[0], float("inf")
    for ratio in ASPECT_RATIOS:
        dist = abs(math.log(ratio_value(ratio)) - target)
        if dist < best_dist - 1e-12:
            best, best_dist = ratio, dist
    return best


def parse_size(size: str) -> tuple[int, int]:
    """解析 `"1024x1024"`。写错了 ⇒ 400（这是"你写错了"，不是"上游没有"）。"""
    m = _SIZE_RE.match(size)
    if not m:
        raise InvalidParameterError(
            f"size='{size}' 无法解析。正确写法是 `\"宽x高\"`，例如 `\"1024x1024\"`；"
            f"或直接用原生字段 aspect_ratio（可选值：{', '.join(ASPECT_RATIOS)}）。",
            param="size",
        )
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        raise InvalidParameterError(f"size='{size}' 的宽高必须为正数。", param="size")
    return w, h


def resolve_model(raw: str | None) -> tuple[str, str | None]:
    """`model` → (能力名, 留痕文案或 None)。"""
    if raw is None or not str(raw).strip():
        return DEFAULT_MODEL, f"未指定模型 ⇒ 使用默认 {DEFAULT_MODEL}。"
    key = str(raw).strip().lower()
    if key in MODEL_ALIASES:
        resolved = MODEL_ALIASES[key]
        if resolved in NOT_AVAILABLE_MODELS:
            info = NOT_AVAILABLE_MODELS[resolved]
            raise CapabilityUnavailable(
                f"model='{raw}' ⇒ {resolved}（{info['capability']}）当前**未启用**。"
                f"原因：{info['reason']} 开启方式：{info['enable']}",
                param="model",
                detail={"model": resolved, "capability": info["capability"]},
            )
        if resolved == MODEL_AUTO:
            return MODEL_T2I, f"model='{raw}' ⇒ 自动推导为 {MODEL_T2I}（本服务只有文生图一种能力）。"
        if key != resolved:
            return resolved, f"model='{raw}' 是别名 ⇒ 解析为 {resolved}。"
        return resolved, None
    if key in PLACEHOLDER_MODELS:
        return (
            DEFAULT_MODEL,
            f"model='{raw}' 是第三方 SDK 的占位名，**不代表调用意图** ⇒ 使用默认 {DEFAULT_MODEL}。",
        )
    raise InvalidParameterError(
        f"model='{raw}' 无法识别。本服务的能力：{MODEL_T2I}（文生图，默认）、"
        f"{MODEL_I2I}（图生图，需 image 数组）（别名：{', '.join(sorted(MODEL_ALIASES))}）。",
        param="model",
    )


def normalize(req: GenerationRequest) -> NormalizedRequest:
    """把受理请求归一化成"上游能懂"的三元组 + 留痕列表。

    🔴 只做归一化，**不碰上游**（受理内零上游往返，见 docs/INTERFACE.md §1）。
    """
    degradations: list[str] = []

    # --- prompt
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise InvalidParameterError("prompt 不能为空字符串。", param="prompt")

    # --- model
    model, model_note = resolve_model(req.model)
    if model_note:
        degradations.append(model_note)

    # --- image（t2i：刻意缺席 ⇒ 响亮失败；i2i：恰 1 条参考图）
    images = req.image
    entries: list[Any] | None = None
    if images is not None:
        if isinstance(images, str):
            raise InvalidParameterError(
                "image 必须是**数组**（每条一个参考图引用），不能是裸字符串。",
                param="image",
            )
        if not isinstance(images, list):
            raise InvalidParameterError(
                f"image 类型不对（收到 {type(images).__name__}），应为数组。", param="image"
            )
        entries = images

    image_ref: str | None = None
    if model == MODEL_I2I:
        if not entries:
            raise InvalidParameterError(
                f"model='{MODEL_I2I}' 需要 `image` 数组且**恰好 1 条**参考图"
                "（http(s) URL 或 `data:image/…` URI）。指令式编辑：prompt 描述要改什么。",
                param="image",
            )
        if len(entries) != 1:
            raise InvalidParameterError(
                f"model='{MODEL_I2I}' 只接受 1 张参考图（收到 {len(entries)} 条）。"
                "上游 /api/ai-photo-editor 一次只吃一张。",
                param="image",
            )
        ref = entries[0]
        if not isinstance(ref, str) or not ref.strip():
            raise InvalidParameterError(
                "image[0] 必须是非空字符串（http(s) URL 或 `data:image/…` URI）。",
                param="image",
            )
        ref = ref.strip()
        if not (ref.startswith(("http://", "https://")) or ref.startswith("data:image/")):
            raise InvalidParameterError(
                "image[0] 只接受 http(s) URL 或 `data:image/…` URI"
                "（本服务会代你下载并转存到上游存储 —— 上游不接受外链）。",
                param="image",
            )
        image_ref = ref
        degradations.append(
            f"模型 {MODEL_I2I} ⇒ 图生图（上游 /api/ai-photo-editor，指令式编辑）。"
            "⚠️ 编辑器实测可能**长期 pending**（10 分钟+），超时预算独立为 "
            "task_timeout_i2i=3600s。"
        )
    elif entries:
        raise InvalidParameterError(
            f"收到 {len(entries)} 张参考图，但模型是 {model}（文生图）。"
            f"要走图生图请显式指定 model=\"{MODEL_I2I}\"（上游走 /api/ai-photo-editor，"
            "指令式编辑：prompt 写要改什么）；只想文生图请去掉 image。",
            param="image",
        )
    elif images is not None:
        degradations.append("image=[] ⇒ 无参考图（文生图）。")

    # --- n（上游一次任务只出一张）
    if req.n is not None:
        if not isinstance(req.n, int) or isinstance(req.n, bool):
            raise InvalidParameterError(f"n 必须是整数（收到 {req.n!r}）。", param="n")
        if req.n != 1:
            raise InvalidParameterError(
                f"n={req.n} 不支持：上游一次任务只出一张图（并且是**在途互斥**限流，"
                "串行提交多张必然撞 FREE_TASK_*）。请用 n=1 或省略；需要多张就多次调用。",
                param="n",
            )

    # --- 比例：原生字段优先，size 只做换算
    aspect_ratio: str
    if req.aspect_ratio is not None and str(req.aspect_ratio).strip():
        raw_ratio = str(req.aspect_ratio).strip()
        if raw_ratio not in ASPECT_RATIOS:
            raise InvalidParameterError(
                f"aspect_ratio='{raw_ratio}' 不是上游支持的档位。可选：{', '.join(ASPECT_RATIOS)}。",
                param="aspect_ratio",
            )
        aspect_ratio = raw_ratio
        if req.size:
            degradations.append(
                f"同时给了 size='{req.size}' 与 aspect_ratio='{aspect_ratio}' ⇒ "
                f"**以 aspect_ratio 为准**，size 被忽略（上游只认比例档）。"
            )
    elif req.size is not None and str(req.size).strip():
        raw_size = str(req.size).strip()
        if raw_size.lower() == "auto":
            aspect_ratio = DEFAULT_ASPECT_RATIO
            degradations.append(
                f"size='auto' ⇒ 由本服务决定，取默认 aspect_ratio={DEFAULT_ASPECT_RATIO}。"
            )
        else:
            w, h = parse_size(raw_size)
            aspect_ratio = nearest_aspect_ratio(w, h)
            pixels = ASPECT_RATIO_PIXELS[aspect_ratio]
            degradations.append(
                f"size='{raw_size}' ⇒ aspect_ratio={aspect_ratio}"
                f"（本服务按宽高比吸附到上游 5 档枚举 {', '.join(ASPECT_RATIOS)}）。"
                f"🔴 上游请求体只认比例、没有宽高字段 ⇒ **实际产出是 {pixels}**，"
                f"不是你请求的 {w}×{h}；需要别的像素只能换比例档或以本服务之外的方式放大。"
            )
    else:
        aspect_ratio = DEFAULT_ASPECT_RATIO
        degradations.append(
            f"未指定比例 ⇒ aspect_ratio={DEFAULT_ASPECT_RATIO}"
            f"（上游默认档，产出 {ASPECT_RATIO_PIXELS[DEFAULT_ASPECT_RATIO]}）。"
        )

    if image_ref is not None:
        degradations.append(
            f"图生图上游（/api/ai-photo-editor）**没有比例字段** ⇒ '{aspect_ratio}' 不影响产出"
            "（编辑产物跟随参考图）。"
        )

    # --- 「认得但做不到」的字段：不报错，留痕
    extra: dict[str, Any] = dict(req.model_extra or {})
    for name, value in sorted(extra.items()):
        if name in DEGRADABLE_FIELDS:
            degradations.append(
                f"{name}={value!r} 上游无对应字段 ⇒ 忽略（本服务不静默：此条即留痕）。"
            )
        else:
            raise InvalidParameterError(
                f"未知字段 '{name}'。本服务只接受：prompt / model / image / n / "
                f"aspect_ratio / size，外加下列【认得但会忽略】的字段："
                f"{', '.join(sorted(DEGRADABLE_FIELDS))}。",
                param=name,
            )

    return NormalizedRequest(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        model=model,
        degradations=degradations,
        image_ref=image_ref,
    )


def capability_payload() -> dict[str, Any]:
    """能力表的 JSON 投影 —— `GET /capabilities` 里的 `capability` 段。

    ⚠️ 2026-09-22 起**不再**有 models 端点直接返回它：`/async/v1/models` 已移除，
    `/v1/models` 是 OpenAI 兼容的最小形态（`openai_models_payload()`）。
    本函数现在是「全集（含未启用能力的**原因与开启方式**）」的唯一出口。
    """
    i2i_aliases = sorted(k for k, v in MODEL_ALIASES.items() if v == MODEL_I2I)
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_T2I,
                "object": "model",
                "owned_by": SERVICE_NAME,
                "capability": "text_to_image",
                "description": "文生图。上游 imagefree.net，免费额度制。",
                "aspect_ratios": list(ASPECT_RATIOS),
                #: 每档的**实际产出像素**（`1:1` → 1024×1024、`4:3` → 1024×768 已下载核对，
                #: 见 docs/UPSTREAM.md §8；其余档位来自站点 UI 的标称值）。
                #: 请求里的 `size` 只影响**选档**，不改像素。
                "aspect_ratio_pixels": dict(ASPECT_RATIO_PIXELS),
                "default_aspect_ratio": DEFAULT_ASPECT_RATIO,
                "max_images_per_request": 1,
                "accepts_reference_images": False,
                "size_is_advisory": True,
                "available": True,
                "aliases": sorted(k for k, v in MODEL_ALIASES.items() if v in (MODEL_T2I, MODEL_AUTO)),
            },
            {
                "id": MODEL_I2I,
                "object": "model",
                "owned_by": SERVICE_NAME,
                "capability": "image_to_image",
                "description": "图生图（指令式编辑）：1 张参考图 + prompt 描述要改什么。"
                "上游 /api/ai-photo-editor；本服务代取参考图并转存到上游存储"
                "（上游不接受外链）。⚠️ 编辑器可能长期 pending（10 分钟+），超时预算 3600s；"
                "部署需配置 IMAGEFREE_TURNSTILE_TOKEN（工具端点强制 Turnstile）。",
                "accepts_reference_images": True,
                "max_reference_images": 1,
                "reference_formats": ["http(s) URL", "data:image/… URI"],
                "upload_limit_mb": 10,
                "aspect_ratios": [],
                "size_is_advisory": True,
                "available": True,
                "aliases": i2i_aliases,
            },
            # 🔴 列出来但明说不可用：调用方能发现它、知道原因与开启方式
            # （而不是 400 之后以为拼错了）。命中时受理端给 503。
            *[
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": SERVICE_NAME,
                    "capability": info["capability"],
                    "description": "已枚举但**未启用**（不假装能用）。命中时受理返回 503。",
                    "available": False,
                    "reason": info["reason"],
                    "enable": info["enable"],
                    "aliases": sorted(k for k, v in MODEL_ALIASES.items() if v == model_id),
                }
                for model_id, info in NOT_AVAILABLE_MODELS.items()
            ],
        ],
    }


#: 能力表版本时间（unix 秒）= 2026-09-21T00:00:00Z（由 `date -u` 求得，勿手写）。
#: **语义**：本服务能力表**最近一次结构性变更**的日期（2026-09-21 = `image-i2i` 启用）。
#: 只用于 OpenAI 兼容裸端点的 `created` 字段 —— 它**不是**上游模型的创建时间
#: （上游实际用哪个模型对我们不可见，见 docs/UPSTREAM.md「未取证」）。
#: ⚠️ 能力结构再变（启用/移除模型）时更新此值，并同步 docs/INTERFACE.md §0。
MODEL_RELEASED_AT: int = 1789948800


def openai_models_payload() -> dict[str, Any]:
    """`GET /v1/models` —— **OpenAI 标准形态**（本服务唯一的 models 端点）。

    🔴 单向映射：**只用于出站渲染**。禁止拿它写库、写缓存，
    也禁止让 `capability_payload()` 反过来从它派生 —— 两套形态的服务对象不同：

      · 本函数（对外模型清单）：给 OpenAI 生态的客户端/网关做**自动探测与自动选模型**。
        `data` 里**只放可调用的模型**（`available=True`）；每条**只有**四字段
        `id` / `object` / `created` / `owned_by`（OpenAI Model 对象的最小形状）——
        不加任何自有字段，加了就不叫"结构兼容"。
      · `capability_payload()`（能力全集）：含未启用能力的**原因与开启方式**、比例档、
        别名、`size_is_advisory` 等扩展字段。它的出口是 `GET /capabilities`（运维端点）；
        未启用能力的**可发现性**由它保证 —— 这里不列它们不等于"藏着"。

    ⚠️ 2026-09-22：旧的 `/async/v1/models`（直接返回 `capability_payload()` 的富形态）
    **已移除，且不做别名兼容**（裁决见 docs/INTERFACE.md §0.1）。
    """
    return {
        "object": "list",
        "data": [
            {
                "id": item["id"],
                "object": "model",
                "created": MODEL_RELEASED_AT,
                "owned_by": SERVICE_NAME,
            }
            for item in capability_payload()["data"]
            if item.get("available")
        ],
    }


__all__ = [
    "ASPECT_RATIOS",
    "ASPECT_RATIO_PIXELS",
    "DEFAULT_ASPECT_RATIO",
    "DEFAULT_MODEL",
    "DEGRADABLE_FIELDS",
    "DELIBERATE_ABSENCES",
    "MODEL_ALIASES",
    "MODEL_AUTO",
    "MODEL_I2I",
    "MODEL_RELEASED_AT",
    "MODEL_T2I",
    "MODEL_UPSCALE",
    "NOT_AVAILABLE_MODELS",
    "PLACEHOLDER_MODELS",
    "SERVICE_NAME",
    "TASK_PREFIX",
    "GenerationRequest",
    "NormalizedRequest",
    "capability_payload",
    "nearest_aspect_ratio",
    "normalize",
    "openai_models_payload",
    "parse_size",
    "ratio_value",
    "resolve_model",
]
