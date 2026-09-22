#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""右下角站点水印的离线去除（针对 pollinations.ai 类"角落白字"水印）。

来源：2026-09-21 对 pollinations 匿名档的实测（`docs/POLLINATIONS-RECON.md` §6）——
水印是**不透明白字**烙在右下角（`nologo` 参数匿名档无效，核心像素 44~48% 纯白，
被遮信息已丢失），所以只能"定位 + 合成"：精确掩膜 → Telea 修补 → 颗粒匹配。

三种判定（阈值来自实测：真水印 486~3248px，内容噪声 ≤89px）：

  · ``none``   (<15px)    无水印 —— 原图直出，**绝不误伤**；
  · ``sparse`` (15~79px)  白字压浅底、掩膜不可靠 —— 兜底矩形整块修；
  · ``text``   (≥80px)    正常白字 —— 精确掩膜。

颗粒匹配：修补区比周围平滑，把环形邻域测得的高频噪声（σ）补回修复区，
3× 特写下沥青/木纹等纹理明显更自然（A/B 见 RECON 报告）；σ<0.3 视为干净底，跳过。

速度实测：512² ≈5ms、768² ≈8.5ms —— inpaint 只随**掩膜面积**扩展，与图幅基本无关，
批量场景瓶颈在磁盘 IO 不在本算法。

质量边界（诚实声明）：纹理类背景（沥青/木纹/亚麻）放大检视无痕；水印压在
文字/人脸等结构化内容上时 Telea 会偏软，那需要 LaMa 级模型（按"轻量"约束未集成）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cv2
import numpy as np

#: 检测窗口：右 50% × 底 14%（水印固定贴角，窗口越小误检越少）。
_WINDOW_X_RATIO = 0.50
_WINDOW_Y_RATIO = 0.86

#: 白字判定：高亮度 + 低饱和（HSV）。上限放宽到 S≤60 以兼容轻微色边。
_TEXT_WHITE_LOW = (0, 0, 200)
_TEXT_WHITE_HIGH = (180, 60, 255)

#: 三档判定阈值（像素数）。来源与依据见模块 docstring。
_NONE_MAX_PX = 15
_SPARSE_MAX_PX = 80

#: 兜底矩形（sparse 档）：底 7.5% × 右 34%，四边留 6px 防 JPEG 呼吸边。
_RECT_FILL_RATIO_H = 0.075
_RECT_FILL_RATIO_W = 0.34
_RECT_INSET_PX = 6

#: 掩膜膨胀：盖住抗锯齿边与阴影。两轮 5×5 ≈ 外扩 4px，实测恰好包住描边。
_MASK_KERNEL = (5, 5)
_MASK_DILATE_ITER = 2

#: 颗粒匹配：环形邻域外径 13（内径 5）测 σ；σ 低于此值视为干净底不加噪。
_GRAIN_RING_OUTER = 13
_GRAIN_RING_INNER = 5
_GRAIN_SIGMA_MIN = 0.3
#: 固定种子 ⇒ 同一输入永远得到同一输出（可测试、可复现）。
_GRAIN_SEED = 0

WatermarkMode = Literal["none", "sparse", "text"]


@dataclass(frozen=True)
class WatermarkVerdict:
    """一次检测的完整结论。``mask`` 已膨胀，可直接喂给 inpaint。"""

    mode: WatermarkMode
    #: 命中的原始白色像素数（膨胀前，用于日志/统计）。
    text_px: int
    #: 膨胀后的修复掩膜；``mode == "none"`` 时为全零。
    mask: np.ndarray


def detect_watermark(img: np.ndarray) -> WatermarkVerdict:
    """在右下角窗口内定位白字水印，返回三档判定与膨胀后的掩膜。"""
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"期望 BGR 三通道图像，得到 shape={img.shape}")
    h, w = img.shape[:2]
    x0, y0 = int(w * _WINDOW_X_RATIO), int(h * _WINDOW_Y_RATIO)
    window = img[y0:h, x0:w]

    hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
    mask = np.zeros((h, w), dtype=np.uint8)
    text_px = int(cv2.countNonZero(cv2.inRange(hsv, _TEXT_WHITE_LOW, _TEXT_WHITE_HIGH)))
    mask[y0:h, x0:w] = cv2.inRange(hsv, _TEXT_WHITE_LOW, _TEXT_WHITE_HIGH)

    if text_px < _NONE_MAX_PX:
        return WatermarkVerdict("none", text_px, np.zeros((h, w), dtype=np.uint8))
    if text_px < _SPARSE_MAX_PX:
        # 白字压浅底：精确掩膜会漏描边，整块兜底矩形更稳。
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[h - int(h * _RECT_FILL_RATIO_H): h - _RECT_INSET_PX,
             w - int(w * _RECT_FILL_RATIO_W): w - _RECT_INSET_PX] = 255
        return WatermarkVerdict("sparse", text_px, mask)

    kernel = np.ones(_MASK_KERNEL, dtype=np.uint8)
    return WatermarkVerdict("text", text_px, cv2.dilate(mask, kernel, iterations=_MASK_DILATE_ITER))


def _grain_match(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """把环形邻域的高频噪声补回修复区（轻量质感增强，~+2ms）。"""
    dilated = cv2.dilate(mask, np.ones((_GRAIN_RING_OUTER,) * 2, dtype=np.uint8))
    annulus = cv2.subtract(dilated, cv2.dilate(mask, np.ones((_GRAIN_RING_INNER,) * 2, dtype=np.uint8)))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sigma = (gray - cv2.GaussianBlur(gray, (0, 0), 2))[annulus > 0].std()
    if sigma < _GRAIN_SIGMA_MIN:
        return img
    noise = np.random.default_rng(_GRAIN_SEED).normal(0, sigma, img.shape).astype(np.float32)
    out = img.astype(np.float32)
    out[cv2.dilate(mask, np.ones((3, 3), dtype=np.uint8)) > 0] += noise[
        cv2.dilate(mask, np.ones((3, 3), dtype=np.uint8)) > 0
    ]
    return np.clip(out, 0, 255).astype(np.uint8)


def remove_watermark(img: np.ndarray) -> np.ndarray:
    """去除右下角站点水印，返回新图（不修改入参）。

    三档行为见模块 docstring；``none`` 档返回**逐像素相同的拷贝**。
    确定性：固定随机种子，同一输入永远得到同一输出。
    """
    verdict = detect_watermark(img)
    if verdict.mode == "none":
        return img.copy()
    out = cv2.inpaint(img, verdict.mask, 5, cv2.INPAINT_TELEA)
    return _grain_match(out, verdict.mask)


def _sniff_format(data: bytes) -> str:
    """按魔数识别 JPEG/PNG，未知格式直接拒绝（不猜）。"""
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    raise ValueError("仅支持 JPEG / PNG 字节流（按魔数识别，不信任扩展名）")


def remove_watermark_bytes(data: bytes) -> tuple[bytes, WatermarkMode]:
    """字节流进、字节流出（保持原格式），返回 ``(新字节, 判定档位)``。

    ``none`` 档**原字节直出**（零重编码、零画质损失）；其余档重编码一次。
    供服务管线/上传后处理调用；CLI 走 :func:`remove_watermark_file`。
    """
    fmt = _sniff_format(data)
    array = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if array is None:
        raise ValueError("图像解码失败（文件损坏或格式与魔数不符）")
    verdict = detect_watermark(array)
    if verdict.mode == "none":
        return data, "none"
    cleaned = _grain_match(cv2.inpaint(array, verdict.mask, 5, cv2.INPAINT_TELEA), verdict.mask)
    ok, encoded = cv2.imencode(fmt, cleaned)
    if not ok:  # pragma: no cover - imencode 对合法 ndarray 实际不会失败
        raise ValueError("图像编码失败")
    return encoded.tobytes(), verdict.mode


def remove_watermark_file(
    in_path: str | Path, out_path: str | Path | None = None
) -> tuple[Path, WatermarkMode, float]:
    """文件级封装：读 → 去水印 → 写，返回 ``(输出路径, 档位, 耗时秒)``。

    默认输出 ``<名字>_clean.<原扩展名>``（同目录）；``none`` 档原字节拷贝。
    """
    src = Path(in_path)
    data = src.read_bytes()
    started = time.perf_counter()
    cleaned, mode = remove_watermark_bytes(data)
    elapsed = time.perf_counter() - started
    dst = Path(out_path) if out_path is not None else src.with_name(f"{src.stem}_clean{src.suffix}")
    dst.write_bytes(data if mode == "none" else cleaned)
    return dst, mode, elapsed
