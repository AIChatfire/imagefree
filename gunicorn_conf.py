#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gunicorn 配置。

## 🔴 末尾那对括号不能省

目标是**工厂**而不是模块级对象：

```bash
gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'` /
`App failed to load.` —— 而**单测全绿也照样炸**，因为它们都直接调 `create_app()`。
`tests/test_wiring.py::test_dockerfile_cmd_target_resolves` 钉的就是这条。

## 默认单 worker 是**架构约束**，不是保守参数

上游 imagefree.net 的限流是**在途互斥**（同浏览器身份 / 同 IP 只允许一个任务在跑），
而本服务的闸门账本（最小间隔 / 每分钟上限）是**进程内**状态。
副本数 N 会把闸门放宽 N 倍，并且 N 个副本会同时抢同一个上游名额
⇒ 除一个以外全部撞 `FREE_TASK_*`。

更根本的是：**多副本对吞吐毫无帮助** —— 上游一次只允许一个任务在跑。
⇒ 提吞吐只有一条路：换上游，不是加副本。
"""
from __future__ import annotations

import os

#: 默认 1。见模块 docstring —— 这是架构约束，不是调参起点。
workers = int(os.environ.get("WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8400')}"

#: 超时必须**大于**一次上游提交的最坏耗时（含 Cloudflare 边缘的首字节延迟），
#: 否则请求会被 gunicorn 掐掉 —— 而受理请求其实不碰上游，所以 120s 绰绰有余。
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

#: 受理请求很轻（只落库），日志交给 loguru（应用内） ⇒ 不重复出 access log。
accesslog = None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info").lower()

#: 进程名带上 worker 数，`ps` 一眼能看出是不是有人偷偷放宽了副本数。
proc_name = f"imagefree-service[w{workers}]"

#: 预载能让多 worker 共享同一份只读内存；但本服务在 import 期**不连上游**，
#: 预载收益有限，而它会掩盖"某个 worker 启动失败"这件事 ⇒ 关闭。
preload_app = False

max_requests = 0        # 长驻进程；任务状态在库里，不需要靠重启换血
worker_tmp_dir = None
