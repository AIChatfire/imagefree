#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发探针：一次打 N 条**并发提交**，用来刻画上游的频控维度。

它回答的问题（都是本服务容量设计的依据）：

  1. 同一 IP / 同一浏览器身份下，**同时**能跑几个任务？
  2. 第 2 条被拒时是**哪个** `errorCode`（IP 维度还是浏览器身份维度）？
  3. 被拒时的 **HTTP 状态码**是多少（200+error？429？403？）、有没有 `Retry-After`？
  4. 用**全新身份**（独立 cookie jar）从同一 IP 再打一条，会被放行还是被拒？
     —— 这一步把墙归因到 IP 或身份上（`--identity=fresh`）。
  5. （`--watch`）被受理的任务多久出图，以及**槽位什么时候释放**。

用法（🔴 每一条被受理的提交都会消耗 1 次免费额度）：

    # 3 条并发、共用一个身份（= 本服务默认形态）
    python scripts/burst.py --n 3 --i-know-this-consumes-quota

    # 3 条并发、每条一个全新身份（归因到 IP 还是身份）
    python scripts/burst.py --n 3 --identity fresh --i-know-this-consumes-quota

    # 顺带盯着被受理的任务到终态
    python scripts/burst.py --n 3 --watch --i-know-this-consumes-quota

🚫 不给 `--i-know-this-consumes-quota` 一律拒绝执行 —— 这是提交类操作的统一纪律
（与 `scripts/probe.py generate` 一致）。

⚠️ 走的是**原始 HTTP**（不经本服务的协调器与闸门），因为要测的正是上游本身的墙；
经本服务打的话会被闸门串行化，什么都测不出来。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

DEFAULT_BASE = "https://imagefree.net"

_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/json",
    "origin": DEFAULT_BASE,
    "referer": f"{DEFAULT_BASE}/zh",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}


def _new_client(args: argparse.Namespace) -> httpx.Client:
    proxy = args.proxy or None
    return httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=_HEADERS,
        timeout=args.timeout,
        proxy=proxy,
        trust_env=proxy is None,
    )


def _submit(client: httpx.Client, args: argparse.Namespace, index: int) -> dict[str, Any]:
    """一次提交。**不做任何错误映射** —— 要的就是原始状态码与原始报文。"""
    body = {
        "prompt": args.prompt,
        "aspect_ratio": args.aspect_ratio,
        "turnstile_token": None,
    }
    started = time.monotonic()
    try:
        resp = client.post("/api/generate", json=body)
    except Exception as exc:  # noqa: BLE001 - 传输层失败也是结论的一部分
        return {"i": index, "elapsed": round(time.monotonic() - started, 2), "transport_error": repr(exc)}
    elapsed = round(time.monotonic() - started, 2)
    try:
        payload: Any = resp.json()
    except ValueError:
        payload = {"_not_json": resp.text[:200]}
    return {
        "i": index,
        "http": resp.status_code,
        "elapsed": elapsed,
        "body": payload,
        "retry_after": resp.headers.get("retry-after"),
        "set_cookie": resp.headers.get("set-cookie"),
        "cf_ray": resp.headers.get("cf-ray"),
        "cookie_after": client.cookies.get("imagefree_free_generation_id"),
    }


def run_burst(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.identity == "shared":
        client = _new_client(args)
        clients = [client] * args.n
    else:
        clients = [_new_client(args) for _ in range(args.n)]
    try:
        # ThreadPoolExecutor + 无缓冲 ⇒ 请求几乎同刻发出（这才是"并发"）
        with ThreadPoolExecutor(max_workers=args.n) as pool:
            futures = [
                pool.submit(_submit, clients[i - 1], args, i) for i in range(1, args.n + 1)
            ]
            results = [f.result() for f in futures]
    finally:
        for unique in {id(c): c for c in clients}.values():
            unique.close()
    return results


def print_results(results: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    accepted: list[str] = []
    codes: dict[str, int] = {}
    print("\n序号  HTTP  耗时    errorCode / taskId")
    print("----  ----  ------  ------------------------------------------")
    for row in results:
        if "transport_error" in row:
            print(f"{row['i']:>4}  --    {row['elapsed']:>5}s  {row['transport_error'][:60]}")
            codes["transport_error"] = codes.get("transport_error", 0) + 1
            continue
        body = row["body"]
        code = body.get("errorCode") or body.get("error") or ""
        task_id = body.get("taskId")
        if task_id:
            accepted.append(str(task_id))
            marker = f"✅ taskId={task_id}"
        else:
            marker = f"❌ {code}"
        codes[str(code) or "unknown"] = codes.get(str(code) or "unknown", 0) + 1
        print(f"{row['i']:>4}  {row['http']:>4}  {row['elapsed']:>5}s  {marker}")
        extras = []
        if row["retry_after"]:
            extras.append(f"Retry-After={row['retry_after']}")
        if row["set_cookie"]:
            # 🔴 打全：这一轮的发现就是"每次提交都下发一个新的身份 UUID"，
            # 只看"有没有"会漏掉"每次都不一样"这个事实。
            extras.append(f"Set-Cookie={row['set_cookie'][:120]}")
        if extras:
            print(f"                               {' | '.join(extras)}")
        if row["cookie_after"]:
            print(f"                               提交后该客户端持有身份={row['cookie_after']}")
    print(f"\n被受理 {len(accepted)} 条 / 共 {len(results)} 条；错误码分布：{json.dumps(codes, ensure_ascii=False)}")
    return accepted, codes


def watch(client: httpx.Client, task_ids: list[str], args: argparse.Namespace) -> None:
    """盯着被受理的任务到终态，报"提交→成图"耗时（这也是槽位被占的时长）。"""
    deadline = time.monotonic() + args.watch_timeout
    pending = set(task_ids)
    started = {tid: time.monotonic() for tid in task_ids}
    print("\n---- 跟踪被受理的任务 ----")
    while pending and time.monotonic() < deadline:
        for tid in sorted(pending):
            try:
                resp = client.get("/api/generate/status", params={"taskId": tid})
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                print(f"  {tid[:8]} 查询失败：{exc!r}")
                continue
            status = payload.get("status")
            if status == "completed" and payload.get("image"):
                print(f"  {tid[:8]} ✅ 出图，用时 {round(time.monotonic() - started[tid], 1)}s → {payload['image']}")
                pending.discard(tid)
            elif status == "failed":
                print(f"  {tid[:8]} ❌ 上游判定失败（用时 {round(time.monotonic() - started[tid], 1)}s）")
                pending.discard(tid)
        if pending:
            time.sleep(args.poll_interval)
    if pending:
        print(f"  ⏰ 超时未终态：{sorted(t[:8] for t in pending)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="imagefree.net 并发探针（🔴 消耗免费额度）")
    parser.add_argument("--n", type=int, default=3, help="并发条数（1..5，默认 3）")
    parser.add_argument("--prompt", default="a red apple on a wooden table")
    parser.add_argument("--aspect-ratio", default="1:1", choices=["1:1", "3:4", "4:3", "9:16", "16:9"])
    parser.add_argument(
        "--identity",
        choices=["shared", "fresh"],
        default="shared",
        help="shared=N 条共用一个身份（本服务默认形态）；fresh=每条一个全新身份（把墙归因到 IP/身份）",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--proxy", default="", help="走指定代理（默认直连）")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--watch", action="store_true", help="跟踪被受理的任务到终态")
    parser.add_argument("--watch-timeout", type=float, default=600.0, help="跟踪的总超时（秒）")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--i-know-this-consumes-quota",
        action="store_true",
        help="确认每条被受理的提交都会**消耗 1 次免费额度**（不加则拒绝执行）",
    )
    args = parser.parse_args(argv)

    if not args.i_know_this_consumes_quota:
        print(
            "拒绝执行：每一条被受理的提交都会**消耗 1 次免费额度**。\n"
            "确需执行请显式追加：python scripts/burst.py --n 3 --i-know-this-consumes-quota",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.n <= 5:
        print("--n 必须在 1..5 之间（再往上只是多花额度，信息量不增）", file=sys.stderr)
        return 2

    print(
        f"并发提交 {args.n} 条（身份模式={args.identity}，出口={'直连' if not args.proxy else '代理'}）"
    )
    results = run_burst(args)
    accepted, _codes = print_results(results)

    latencies = [row["elapsed"] for row in results if "elapsed" in row]
    if latencies:
        print(
            f"提交耗时：min {min(latencies)}s / 中位 {round(statistics.median(latencies), 2)}s / max {max(latencies)}s"
        )

    if accepted and args.watch:
        with _new_client(args) as client:
            watch(client, accepted, args)
    elif accepted:
        print("\n（加 --watch 可跟踪这些任务到终态；或用 scripts/probe.py status <taskId> 逐个查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
