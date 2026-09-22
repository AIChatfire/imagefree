#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量去除图片右下角的 pollinations.ai 类白字水印（核心逻辑在 `app.watermark`）。

用法：

    # 单张 / 多张：输出为同目录 <名字>_clean.<原扩展名>
    python scripts/remove_watermark.py a.jpg b.png

    # 覆盖到指定目录
    python scripts/remove_watermark.py a.jpg b.jpg --out-dir /tmp/clean

    # 并发批量（默认 8 线程；cv2/numpy 的 C 层操作释放 GIL，线程池即得真并行）
    python scripts/remove_watermark.py *.jpg --workers 16

行为（三档自动判定，见 `app/watermark.py` 模块 docstring）：
  · 无水印 —— 原样拷贝（**绝不误伤**），回执标注 `none`；
  · sparse（白字压浅底）—— 兜底矩形整块修；
  · text（正常白字）—— 精确掩膜 + 颗粒匹配。

零网络、零上游额度：纯本地 OpenCV 处理，512² 单张 ≈5ms。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.watermark import remove_watermark_file

#: 批处理默认并发。cv2 的 imdecode/imencode/inpaint 与 numpy 大数组运算都释放 GIL，
#: 线程池即可吃满多核，无需进程池（省去解释器启动与序列化开销）。
_DEFAULT_WORKERS = 8


def _process_one(path: pathlib.Path, out_dir: pathlib.Path | None):
    return remove_watermark_file(path, (out_dir / path.name) if out_dir else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量去除右下角站点水印（本地处理，零网络）")
    parser.add_argument("images", nargs="+", type=pathlib.Path, help="输入图片（JPEG/PNG，按魔数识别）")
    parser.add_argument("--out-dir", type=pathlib.Path, default=None,
                        help="输出目录（默认与原图同目录，命名为 <名字>_clean.<ext>）")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS,
                        help=f"并发线程数（默认 {_DEFAULT_WORKERS}；1 = 串行）")
    args = parser.parse_args(argv)

    out_dir = args.out_dir.resolve() if args.out_dir else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)  # 批处理友好：输出目录不存在就建
    failures: list[str] = []
    started = time.perf_counter()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_process_one, p, out_dir): p for p in args.images}
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                out_path, mode, elapsed = future.result()
            except (OSError, ValueError) as exc:  # 读不到/解不开：业务性失败，逐条报告不中断批次
                failures.append(f"{path}: {exc}")
                print(f"FAIL {path}: {exc}", file=sys.stderr)
                continue
            done += 1
            print(f"OK  [{mode:6s}] {path} -> {out_path}  ({elapsed * 1000:.0f}ms)")

    if failures:
        print(f"\n{len(failures)} 个文件失败", file=sys.stderr)
        return 1
    if done:
        wall = time.perf_counter() - started
        print(f"\n{done} 个文件，墙钟 {wall:.2f}s（{wall / done * 1000:.0f}ms/张，workers={args.workers}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
