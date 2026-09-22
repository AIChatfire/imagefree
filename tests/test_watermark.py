#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`app.watermark` 的**离线**验证：全部合成图像，零网络、零上游额度。

钉的是三件最容易悄悄错的事：

  · **三档判定**的边界 —— `none` 档必须"逐像素不动"（绝不误伤干净图）；
  · **确定性** —— 固定随机种子，同一输入两次运行必须完全一致（管线可复现）;
  · **bytes/文件封装** —— 格式按魔数识别、none 档原字节直出（零重编码损失）。
"""
from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from app.watermark import (
    _SPARSE_MAX_PX,
    detect_watermark,
    remove_watermark,
    remove_watermark_bytes,
    remove_watermark_file,
)

SIZE = 256


def _textured_base(seed: int = 7) -> np.ndarray:
    """带噪声的渐变底图：整体压暗（≤170），保证检测窗内背景不会被误判成白字。"""
    rng = np.random.default_rng(seed)
    x = np.linspace(30, 160, SIZE, dtype=np.float32)
    grad = np.tile(x, (SIZE, 1))
    noise = rng.normal(0, 6, (SIZE, SIZE)).astype(np.float32)
    base = np.clip(grad + noise, 0, 255).astype(np.uint8)
    return cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)


def _draw_white_text(img: np.ndarray, thick: int = 2) -> np.ndarray:
    """在右下角画一行白色"水印"（窗口内、不越界，像素量落在 text 档）。"""
    out = img.copy()
    h, w = out.shape[:2]
    cv2.putText(out, "watermark", (int(w * 0.55), h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), thick, cv2.LINE_AA)
    return out


def test_none_mode_leaves_clean_image_bit_identical() -> None:
    """干净图必须原样直出：任何"修复动作"都是误伤。"""
    base = _textured_base()
    verdict = detect_watermark(base)
    assert verdict.mode == "none"

    cleaned = remove_watermark(base)
    assert np.array_equal(cleaned, base)


def test_text_mode_removes_mark_and_spares_rest() -> None:
    """水印被清掉（残留白字低于 none 阈值），且掩膜外像素逐像素不动。"""
    marked = _draw_white_text(_textured_base())
    verdict = detect_watermark(marked)
    assert verdict.mode == "text"
    assert verdict.text_px >= _SPARSE_MAX_PX  # 护栏：夹具确实落在 text 档而非 sparse

    cleaned = remove_watermark(marked)

    # 1) 修复后再检测：残留必须落在 none 档
    assert detect_watermark(cleaned).mode == "none"
    # 2) 掩膜之外的区域必须逐像素不动（证明没有全局误伤）
    untouched = ~cv2.dilate(verdict.mask, np.ones((7, 7), np.uint8)).astype(bool)
    assert np.array_equal(cleaned[untouched], marked[untouched])
    # 3) 修复区确实动过了
    assert not np.array_equal(cleaned[cv2.dilate(verdict.mask, np.ones((3, 3), np.uint8)).astype(bool)],
                              marked[cv2.dilate(verdict.mask, np.ones((3, 3), np.uint8)).astype(bool)])


def test_sparse_mode_falls_back_to_rect() -> None:
    """白色小斑点（15~79px）走兜底矩形档：掩膜是整块矩形而非精确字廓。"""
    img = _textured_base()
    h, w = img.shape[:2]
    img[h - 20:h - 14, w - 60:w - 54] = 255  # 36px 白斑 → sparse 档
    verdict = detect_watermark(img)
    assert verdict.mode == "sparse"

    cleaned = remove_watermark(img)
    assert detect_watermark(cleaned).mode in ("none", "sparse")  # 兜底整修后不应残留 text 档
    assert not np.array_equal(cleaned, img)


def test_threshold_boundaries() -> None:
    """判定阈值的两端：10px → none；120px → text。防阈值被悄悄改动。"""
    base = _textured_base()
    h, w = base.shape[:2]

    tiny = base.copy()
    tiny[h - 20:h - 18, w - 60:w - 55] = 255  # 10px
    assert detect_watermark(tiny).mode == "none"

    big = _draw_white_text(base, thick=3)
    assert detect_watermark(big).mode == "text"


def test_determinism_same_input_same_output() -> None:
    """固定随机种子 ⇒ 颗粒匹配不会引入运行间抖动（管线可复现）。"""
    marked = _draw_white_text(_textured_base())
    assert np.array_equal(remove_watermark(marked), remove_watermark(marked))


def test_bytes_roundtrip_jpeg_and_png() -> None:
    """字节接口：JPEG/PNG 按魔数识别；none 档原字节直出（零重编码）。"""
    marked = _draw_white_text(_textured_base())
    ok, jpg = cv2.imencode(".jpg", marked)
    assert ok
    cleaned_bytes, mode = remove_watermark_bytes(jpg.tobytes())
    assert mode == "text"
    assert cleaned_bytes[:2] == b"\xff\xd8"  # 保持 JPEG
    assert detect_watermark(cv2.imdecode(
        np.frombuffer(cleaned_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)).mode == "none"

    clean = _textured_base()
    ok, png = cv2.imencode(".png", clean)
    assert ok
    same_bytes, mode = remove_watermark_bytes(png.tobytes())
    assert mode == "none"
    assert same_bytes == png.tobytes()  # 原字节直出，一 bit 都没动


def test_bytes_rejects_unknown_magic() -> None:
    """非 JPEG/PNG 直接拒绝（不猜扩展名）。"""
    with pytest.raises(ValueError, match="JPEG / PNG"):
        remove_watermark_bytes(b"GIF89a" + b"\x00" * 64)


def test_file_wrapper_names_output_and_reports(tmp_path) -> None:
    """文件封装：默认 <名字>_clean.<ext>，none 档原字节拷贝，回传耗时。"""
    marked = _draw_white_text(_textured_base())
    src = tmp_path / "marked.jpg"
    ok, encoded = cv2.imencode(".jpg", marked)
    assert ok
    src.write_bytes(encoded.tobytes())

    dst, mode, elapsed = remove_watermark_file(src)
    assert dst.name == "marked_clean.jpg" and dst.exists()
    assert mode == "text"
    assert 0 <= elapsed < 5  # 秒级上限护栏（实测 ≈5ms，留足 CI 余量）

    clean_src = tmp_path / "clean_src.jpg"
    ok, encoded2 = cv2.imencode(".jpg", _textured_base())
    assert ok
    clean_src.write_bytes(encoded2.tobytes())
    dst2, mode2, _ = remove_watermark_file(clean_src)
    assert mode2 == "none"
    assert dst2.read_bytes() == clean_src.read_bytes()


def test_speed_sanity_on_realistic_size() -> None:
    """512² 单张全流程 < 0.5s（实测 ≈5ms；护栏放宽两个数量级防 CI 抖动）。"""
    big = cv2.resize(_draw_white_text(_textured_base()), (512, 512), interpolation=cv2.INTER_LINEAR)
    started = time.perf_counter()
    remove_watermark(big)
    assert time.perf_counter() - started < 0.5


def test_concurrent_batch_matches_sequential() -> None:
    """线程池批量与串行逐张的结果**逐张一致**（cv2/numpy 释放 GIL ⇒ 可真并行，但不得串染）。"""
    from concurrent.futures import ThreadPoolExecutor

    marked = [_draw_white_text(_textured_base(seed=10 + i)) for i in range(8)]
    sequential = [remove_watermark(m) for m in marked]

    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent = list(pool.map(remove_watermark, marked))

    for seq, con in zip(sequential, concurrent, strict=True):
        assert np.array_equal(seq, con)
