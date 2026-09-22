# imagefree-service

`imagefree.net`（免费文生图站点）的**异步图片生成出口**。

把那个"只有网页端、没有公开接口"的站点，包成一条标准的 **受理 → 轮询** 两段式 API：

```bash
curl -X POST localhost:8400/async/v1/images/generations \
  -H 'content-type: application/json' \
  -d '{"prompt":"一只坐在窗台上的橘猫，午后阳光","aspect_ratio":"1:1"}'
# → 202 {"task_id": "imagefree_b8d9f0b8247f4eeda60f84c908e192cb"}

curl localhost:8400/async/v1/images/generations/imagefree_b8d9f0b8247f4eeda60f84c908e192cb
# → 202 {"task_id": "...", "status": "in_progress"}      # 还没好，继续轮询
# → 200 {"data":[{"url":"https://pub-….r2.dev/images/….png"}],"created":1789923012}
```

形态与 `../jimeng`、`../hailuo` **刻意一致**：换站点不该换调用方式。
对外契约（冻结）见 [`docs/INTERFACE.md`](docs/INTERFACE.md)；
上游取证（含未取证清单）见 [`docs/UPSTREAM.md`](docs/UPSTREAM.md)。

---

## 1. 三句话讲清上游

1. **没有鉴权**：`POST /api/generate` 只要有 `prompt` + `aspect_ratio` 就能出图，
   只认 **5 个比例档**（`1:1`/`3:4`/`4:3`/`9:16`/`16:9`）。
2. **限流是"在途互斥"**：同浏览器身份或同 IP **只允许一个任务在跑**
   （`FREE_GENERATION_ACTIVE` / `FREE_TASK_IP_ACTIVE`）⇒ 并发提交只会撞 429。
3. **Turnstile 是死开关**：前端 `D=false` 硬编码，token 恒为 `null`，
   服务端实测放行。**但开关随时可能被打开** ⇒ 留了 `IMAGEFREE_TURNSTILE_TOKEN` 注入口。

---

## 2. 快速开始

```bash
python3 -m venv /Users/betterme/.workbuddy/binaries/python/envs/imagefree
/Users/betterme/.workbuddy/binaries/python/envs/imagefree/bin/pip install -r requirements-dev.txt

cp .env.example .env          # 空着就能跑（上游免登录）
```

### 🔴 启动冒烟必须关协调器

```bash
COORDINATOR_ENABLED=0 gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

协调器是**唯一会向上游提交任务**的组件，而提交会消耗上游免费额度。
做"服务能不能起来"的冒烟时如果不关它，它会代替你向上游提交任务。

正常跑：

```bash
gunicorn -c gunicorn_conf.py "app.main:create_app()"     # 默认 :8400
python -m pytest -q                                      # 全量用例，**零出网**
```

或容器：

```bash
docker compose up -d --build && curl -s localhost:8400/healthz
```

---

## 3. 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/async/v1/images/generations` | 受理，**只回 `task_id`** |
| `GET` | `/async/v1/images/generations/{task_id}` | `202` 排队态 / `200` 结果 / `200` 失败 |
| `GET` | `/async/v1/images/generations` | 本 Key 的任务列表 |
| `DELETE` | `/async/v1/images/generations/{task_id}` | 只删**已终态**（上游没有取消端点） |
| `GET` | `/v1/models` | 模型清单（OpenAI 兼容形态，只列可调用模型） |
| `GET` | `/healthz` `/readyz` `/stats` `/capabilities` | 运维（**不属于对外契约**） |

鉴权：`Authorization: Bearer <key>`，`API_KEYS` 留空 = 关闭（启动会打 WARNING）。

---

## 4. 架构（依赖方向单向）

```
main.py            HTTP 层：对外契约的唯一出口 + 错误信封
  └ service.py     业务层：受理 / 查询 / 删除（**不碰上游**）
      └ store.py     任务库（事实源；受理必须落库，因为受理不提交上游）
  └ coordinator.py 后台跟进链：**唯一会碰上游的组件**（提交受闸门约束）
      ├ upstream.py  上游客户端：两个端点 + 错误码 → 语义映射
      └ config.py    配置（每个旋钮有唯一读取点）
```

**受理内零上游往返**是刻意的：上游抖动（Cloudflare 边缘、在途互斥）不该传染给受理响应，
否则调用方会重试 ⇒ 重复提交 ⇒ 撞限流。上游的一切不确定性都体现为任务的
`in_progress` / `failure`（附原因）。

### 4.1 遇到频控：加代理（可选）

上游的墙有**两层**：IP 维度（`FREE_TASK_IP_ACTIVE`）与**浏览器身份**维度
（`FREE_GENERATION_ACTIVE` / `FREE_TASK_BROWSER_ACTIVE`）。
所以"只换 IP"不够 —— 本服务的做法是**一个出口一整套身份**：

```bash
# .env（已被 .gitignore 覆盖；凭据绝不进仓库）
IMAGEFREE_PROXIES=socks5h://user:pass@pool.example.com:2088
IMAGEFREE_PROXY_RETRIES=3     # 撞墙后在同一出口当场重试（轮换池：下一连接大概率换 IP）
IMAGEFREE_PROXY_COOLDOWN=60   # 撞墙的出口冷置 60s，后来的任务优先换别的出口
```

三条关键语义：

1. **一个出口 = 一个 httpx.Client = 一个 cookie jar ⇒ 上游单独下发一个浏览器身份**。
   于是 IP 与浏览器两层墙同时被绕开（把身份固定成同一个值就只有第一层被绕开）。
2. **一个任务焊在一个出口上**：提交时选定并落库，之后所有轮询都走它 ——
   因为身份住在那个出口的 jar 里。（查询接口本就零额度、也不挑身份。）
3. **有效容量 = min(IF_CONCURRENCY, 出口数 × 3)**：上游按 IP 记名额，**实测单 IP 上限 3 个在途任务**
   （5 条并发 → 3 受理 + 2 个 `429 FREE_TASK_IP_ACTIVE`；任务终态后名额立刻恢复）。
   所以**加出口是真的能扩容量**，一格出口 = 3 个在途任务（启动日志会写明实际容量）。
   ⚠️ 上游在 429 上给 `Retry-After: 7200`，但**与实测不符**，本服务不据此长等。
4. **直连优先，并发溢出才走池子**（`IMAGEFREE_USE_DIRECT=1`，默认）：
   直连作为**第一个出口**，先把它的 3 个名额用满；溢出的任务自动落到代理出口
   —— 「触发并发就走代理池」是由**出口顺序**保证的，不需要额外开关。
   某条路连不上（代理认证失败等）会被**冷置并自动换下一条**，不会拖垮已受理的任务。

**零额度验证出口是否真的换了 IP**（只打 `/api/geo` 与 IP 回显，不碰生成）：

```bash
python scripts/probe.py egress --echo-ip --repeat 3
```

⚠️ 出口是**明确知情的取舍**：它绕开的是上游的免费额度记账，放大的是上游的成本。

### 为什么默认单副本、并发 1

上游是**在途互斥**的：一次只允许一个任务在跑。所以：

- `IF_CONCURRENCY=1`（默认）—— 并发 > 1 的第二个请求必然被拒，只抬高失败率；
- `gunicorn_conf.py` 默认 `workers=1` —— 闸门账本是**进程内**状态，副本数 N 会把它放宽 N 倍；
- 更根本的是：**多副本对吞吐毫无帮助**。提吞吐的唯一途径是换上游，不是加副本。

---

## 4.2 部署到 Linux（无头）

跑在**没有显示器**的机器上时，只有**图生图**（`image-i2i`）需要额外准备：
它走的工具端点**强制 Turnstile**，而 token 只有几分钟有效期 ⇒ 生产上要配**按需铸造**：

```bash
IMAGEFREE_TURNSTILE_MINT_CMD=node scripts/mint_turnstile.cjs --headless --json
TZ=Asia/Shanghai      # 🔴 硬要求：缺 TZ ⇒ 时区与出口 IP 不一致 ⇒ 挑战变交互式 ⇒ 无头必失败
```

容器/裸机/systemd/验收清单/故障速查见 **[`docs/DEPLOY.md`](docs/DEPLOY.md)**。
只做文生图的话，`docker compose up -d` 就够了（不需要浏览器）。

## 5. 能力边界（**不做假能力**）

| 请求 | 行为 | 理由 |
|---|---|---|
| `image` 传非空数组 + 默认/`t2i` 模型 | `400` | 文生图收下图照跑 = 骗调用方。图生图有正路：**`model="image-i2i"` + `image` 恰 1 条**（2026-09-21 已启用，上游 `/api/ai-photo-editor`；需部署配置 `IMAGEFREE_TURNSTILE_TOKEN`，编辑器可能 pending 10 分钟+，见 docs/INTERFACE.md §3.1） |
| `n > 1` | `400` | 上游一次任务只出一张；用 N 次任务凑会撞在途互斥且额度乘 N |
| `size="1024x1024"` | 换算成最近的 `aspect_ratio`（**留痕**） | 上游只认比例，没有宽高字段 |
| `quality` / `style` / `watermark` … | 忽略（**留痕**） | 「上游没有」≠「你写错了」：前者留痕，后者 4xx |
| 未知字段 | `400 invalid_parameter` | 真正的问题在请求体里 |

`degradations` 挂在**查询响应**上（受理响应被冻结成只有 `task_id`）。
`GET /capabilities` 会带出完整的 `deliberate_absences`。

---

## 6. 测试

```bash
python -m pytest -q
```

- **零真实上游调用**：三层防线 —— `httpx.MockTransport` 顶掉两个端点、
  autouse 夹具把 `httpx.HTTPTransport.handle_request` 改成"一碰就炸"、
  缺依赖直接 import 失败（**不 skip**：跳过会让人把"没跑"当成"跑过了"）。
- 提交任务是**消耗额度**的动作，这条红线由 `tests/conftest.py` 保证。
- `tests/test_wiring.py` 是接线门禁：每个配置项必须真被读、`.env.example` 不许漂、
  `Dockerfile` 的 `CMD` 目标必须能解析（`app.main:create_app()`，**括号不能省**）。

### 上游探测

```bash
python scripts/probe.py status <taskId>     # 只读，零额度
python scripts/probe.py status <taskId> --tool ai-photo-editor   # 只读查图生图任务，零额度
python scripts/probe.py generate --prompt cat --i-know-this-consumes-quota   # 🔴 花额度
python scripts/probe.py tool ai-photo-editor --image ref.png --prompt "改成水彩风" \
    --turnstile-token <现铸token> --watch-timeout 3600 --i-know-this-consumes-quota   # 🔴 花额度
```

第二条**必须**显式带开闸参数，否则脚本拒绝执行。

### 离线去水印（针对 pollinations 类右下角白字）

```bash
python scripts/remove_watermark.py 图1.jpg 图2.png    # 本地处理，零网络、零额度，512² ≈5ms
```

三档自动判定：无水印**原样直出**（绝不误伤）/ 白字压浅底走兜底矩形 / 正常白字走
精确掩膜 + 颗粒匹配。核心在 `app/watermark.py`，取证与质量边界见
`docs/POLLINATIONS-RECON.md` §6。注意：imagefree 上游（imagefree.net）的产物**没有**
水印，本工具面向外部图源（如 pollinations 匿名档）的接入前/后处理。
