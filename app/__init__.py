#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imagefree-service：imagefree.net 的异步图片生成出口。

分层（依赖方向单向，不许反向）：

    main.py         HTTP 层（对外契约的唯一出口）
      └ service.py  业务层（受理/查询/删除，**不碰上游**）
          └ store.py   任务库（事实源）
      └ coordinator.py  后台跟进链（**唯一会碰上游的组件**）
          ├ upstream.py  上游客户端（两个端点 + 错误码映射）
          └ config.py    配置（每个旋钮有唯一读取点）

契约文档：docs/INTERFACE.md（对外） · docs/UPSTREAM.md（上游，含取证方式）
"""

__version__ = "0.1.1"

__all__ = ["__version__"]
