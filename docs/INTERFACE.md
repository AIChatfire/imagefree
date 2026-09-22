# 对外契约（冻结）

> 本文件是**唯一的对外契约真相**。改动 = 破坏调用方，必须同步 `tests/test_api.py`。
> 上游侧的字段、错误码与取证结论在 `docs/UPSTREAM.md`；推导过程在 `.workbuddy/memory/`。
>
> 形态与 `../jimeng`、`../hailuo` **刻意保持一致**：调用方换站点不该换调用方式。

---

## 0. 端点

| 方法 | 路径 | 状态码 | 用途 |
|---|---|---|---|
| `POST` | `/async/v1/images/generations` | `202` | 受理，**只回一个 `task_id`** |
| `GET` | `/async/v1/images/generations/{task_id}` | `202`/`200` | 非终态回排队态；终态回结果 |
| `GET` | `/async/v1/images/generations` | `200` | 本 Key 的任务列表 |
| `DELETE` | `/async/v1/images/generations/{task_id}` | `200`/`400` | 删除**已终态**的任务 |
| `GET` | `/v1/models` | `200` | 模型清单（OpenAI 兼容形态；只列可调用模型） |

运维端点（**不属于对外契约**）：`GET /healthz`（零依赖，容器探活用）、
`GET /readyz`、`GET /stats`、`GET /capabilities`。

鉴权：`Authorization: Bearer <key>`。`API_KEYS` 为空时**关闭鉴权**（仅限内网，
启动会打 WARNING）。任务与 Key 指纹绑定。

### 0.1 `models` 端点：单一路径 `/v1/models`（**干净断裂**）

模型清单只有一个出口，返回 **OpenAI 兼容形态**（每条只有四个键）：

```json
{"object": "list",
 "data": [{"id": "image-t2i", "object": "model", "created": 1789948800, "owned_by": "imagefree"}]}
```

裁决（改代码前先读这里）：

- **只列可调用的模型**（`available: true`）。本端点的用途是「自动探测 → 自动选模型」，
  列一个调不通的 id 等于把 `503` 埋给调用方。**全集**（含未启用能力的**原因与开启方式**）
  由 `GET /capabilities` 提供 —— 不列 ≠ 隐藏。
- `created` 是**本服务能力表的版本时间**（`app/models.py::MODEL_RELEASED_AT`），
  **不是**上游模型的创建时间（上游实际用哪个模型未取证，见 `UPSTREAM.md`）。
- 形态由**单一数据源**派生（`capability_payload()` → `openai_models_payload()`），
  且不得退化成同一份：`tests/test_api.py` 有一条与具体取值无关的结构判据钉住它。
- 🔴 **兼容性裁决：不提供兼容模式。** `/async/v1/models`（旧的富形态）已于 2026-09-22
  **移除** —— 不保留旧路径、不做别名、不双形态、不路径版本化、不复用富形态的字段。
  调用方须改用 `/v1/models`；要全集 / 开启方式就读 `/capabilities`。
  `tests/test_api.py::test_models_route_is_v1_only_and_old_path_is_gone` 钉住它（旧路径必须 404）。
- 新增模型时两套形态自动同步（`/v1/models` 按 `available` 过滤）；
  `MODEL_RELEASED_AT` 需随能力结构变更手动更新。

---

## 1. 受理

```http
POST /async/v1/images/generations
Authorization: Bearer <key>
Content-Type: application/json

{ "prompt": "一只坐在窗台上的橘猫，午后阳光", "aspect_ratio": "1:1" }
```

**响应 `202`**：

```json
{ "task_id": "imagefree_b8d9f0b8247f4eeda60f84c908e192cb" }
```

`Location: /async/v1/images/generations/{task_id}`

🔴 **只有 `task_id` 一个键。** 不返回状态/上游 id/时间戳 —— 多一个键就是契约变更。

**受理请求内零上游往返**：建任务（消耗免费额度的动作）交给后台协调器，受节奏闸门约束
⇒ 上游网络抖动体现为任务 `failed`（附原因），而不是让受理请求跟着一起抖。

### 请求字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `model` | 否 | 见 §3。留空 ⇒ 默认 `image-t2i`（**并在 `degradations` 留痕**） |
| `prompt` | **是** | 提示词（上游唯一的内容输入） |
| `image` | 否 | 参考图数组。**`model="image-i2i"` 时必须恰 1 条**（http(s) URL 或 `data:image/…` URI，≤10MB —— 本服务代取并转存到上游存储，上游不接受外链）。随默认模型传非空数组 ⇒ `400`（提示用 `image-i2i`，见 §3.3） |
| `n` | 否 | 🔴 **只能是 `1`**（上游一次任务只出一张）。`>1` ⇒ `400`，理由见 §4 |
| `aspect_ratio` | 否 | 原生比例，**只认 5 个**：`1:1`/`3:4`/`4:3`/`9:16`/`16:9`。默认 `1:1` |
| `size` | 否 | `"1024x1024"`。**本服务换算**成最近的 `aspect_ratio`（按宽高比的对数距离吸附），**必留痕**。🔴 它**只影响选档、不改像素** —— 上游请求体没有宽高字段，每档产出是固定的（见 §3.2） |
| `quality` | 否 | 上游无此能力 ⇒ 进 `degradations`（不报错） |
| `seed` | 否 | 整数。上游无此字段 ⇒ 进 `degradations` |
| `negative_prompt` | 否 | 上游无此字段 ⇒ 进 `degradations` |

### 关于「认得但做不到」的字段

出现 `watermark` / `response_format` / `style` / `stream` / `user` / `background` /
`output_format` / `moderation` / `sequential_image_generation` 时**不报错**，
而是进 `degradations`（见 §5）。**其它未知字段 → `400 invalid_parameter`**。

判据是：**这是"上游没有"还是"你写错了"？** 两者修复动作完全不同 ——
都塞进"不支持"会让人去查上游能力表，而真正的问题在请求体里。

---

## 2. 查询

```http
GET /async/v1/images/generations/{task_id}
```

**不需要 Authorization**（见 §2.4）。

### 2.1 非终态 → `202`

```json
{ "task_id": "imagefree_...", "status": "queued" }
```

`status ∈ {queued, in_progress}`（全小写，词汇表见 §2.5）。**202 的意思是"还没好，继续轮询"**。

### 2.2 成功 → `200`

```json
{
  "status": "completed",
  "data": [ { "url": "https://pub-62e693a7058040f98bba94ed1d6f880b.r2.dev/images/xxx.png" } ],
  "created": 1789923012
}
```

**终态一定带显式 `status`**（成功 `completed` / 失败 `failed`，见 §2.5）——
调用方不必靠 HTTP 码或"有没有 `error` 键"去推断终态。

两条刻意的取舍：

1. **`data[]` 里只有 `url`。** 与冻结契约逐字一致 —— 多一个键就多一分"形状不同"的风险。
2. **结果 URL 是上游直链（R2），原样透传、不做转存。**
   ⚠️ 有效期**未取证**（见 `docs/UPSTREAM.md` §7）；需要长期可用链接时得另做转存，本服务没做。
3. **不给 `usage`。** 上游是免费额度制、**没有积分或费用数据可给** ⇒ 不编数字。
4. **非空时会多一个 `degradations` 键**（本服务的加性扩展，见 §5）——
   受理响应被冻结成只有 `task_id`，所以降级痕迹只能挂在**查询响应**上。

### 2.3 失败 → `200`

```json
{
  "task_id": "imagefree_...",
  "status": "failed",
  "error": { "message": "...", "type": "...", "code": "..." }
}
```

**失败也回 200**：任务本身完成了（只是结果是失败），请求没出错。
回 4xx 会让调用方的重试逻辑误触发。

### 2.4 不存在 / 不属于本 Key → `404`

```json
{ "error": { "message": "任务 ... 不存在，或不属于当前 API Key。",
             "type": "invalid_request_error", "code": "task_not_found" } }
```

刻意**不区分**这两种情况（区分等于告诉攻击者"这个 id 存在"），且**本地拦死、不发上游请求**。

鉴权语义（三种情况分得很清）：

- **完全没带** `Authorization` ⇒ **放行**。`task_id` 是不可猜的 128 位随机值，
  且只在受理时返回给带 Key 的调用方 ⇒ **id 本身就是凭据**。
- **带了但无效** ⇒ 照旧 `401`（否则调用方的配置错误会被静默吞掉）。
- **带了且有效、但不是属主** ⇒ 照样能查（与"没带"同一待遇）。

⚠️ **列表与删除仍然强制鉴权** —— 否则可以枚举/删除别人的任务。

### 2.5 `status` 词汇（**对齐 new-api 的 task 状态，全小写**）

| 对外 `status` | 含义 | 出现处 |
|---|---|---|
| `queued` | 已受理，还没提交（或正在等待重试） | 查询 `202` |
| `in_progress` | 上游任务在跑 | 查询 `202` |
| `completed` | **终态**：成功 | 查询 `200`（带 `data[].url`） |
| `failed` | **终态**：失败 | 查询 `200`（带 `error`） |

- 取值对齐 new-api 的对外 task 状态（其内部 `SUCCESS`/`FAILURE` 渲染为
  `completed`/`failed`，见 `relaykit/dto/openai_video.go` 的 `VideoStatus*`），**全小写**。
- 🔴 **不提供 `unknown`**：本服务的状态机是**封闭的**（上面四个），不存在未知态 ——
  加一个永不出现的枚举值等于假能力（与 §3「刻意缺席的能力」同一条纪律）。
- 🔴 库内词汇（`success`/`failure`，在 `/stats` 里能看到）与对外词汇是**两套**：
  映射只写一处（`app/service.py::public_status`，单向、只用于出站渲染）。
  **禁止**回写库、禁止拿它做 SQL 条件 —— 混用会静默匹配不到行，且不报错。
- 删除回执的 `status` 是 `deleted`（同样小写）；它不是任务状态，是操作回执。
- 🔴 **终态与 HTTP 码严格对应**：`202` ⇔ 非终态，`200` ⇔ 终态。库内数据不自洽
  （状态在词表外 / 「成功却没图」）时，服务端**宁可 `500` 也不渲染矛盾的终态** ——
  这种 500 **不是重试信号**，是需要运维介入的数据问题。
- 🔴 **终态不可覆盖**：结果一旦收口就不再翻面（迟到的收口 / 并发副本会被忽略并记日志）——
  调用方拿到 `completed` + url 之后，不必担心它变成 `failed`。

---

## 3. 能力与 `model` 取值

### 3.1 本服务的能力名

| `model` | 能力 | 输入图 | prompt |
|---|---|---|---|
| `image-t2i` | 文生图（默认） | 0（传 `[]` 或省略） | 必需 |
| `image-auto` | 自动（等同 `image-t2i`） | 0 | 必需 |
| `image-i2i` | **图生图 / 指令式编辑**（2026-09-21 启用，上游 `/api/ai-photo-editor`） | **恰 1 条**（URL / data URI，≤10MB） | 必需（描述要改什么） |

**别名**（**大小写不敏感**）：
`t2i` / `text2image` / `txt2img` / `文生图` / `生图` / `imagefree` /
`image-auto` / `image` / `auto`；
`i2i` / `image-to-image` / `edit` / `图生图` / `编辑`

**占位名**（`dall-e-3` / `gpt-image-1` / `flux` / `sdxl` / `seedream` …）
等价于"没写 `model`"，走默认 —— 第三方 SDK 常硬编码这些值，它们不代表调用意图。

**图生图（image-i2i）须知**：
- 部署侧必须配置 `IMAGEFREE_TURNSTILE_TOKEN`（工具端点**强制 Turnstile**，实测 400，
  见 `docs/UPSTREAM.md` §10.1；铸 token 配方 `scripts/mint_turnstile.cjs`，
  token 有效期仅几分钟 ⇒ **现铸现用**）。未配置 ⇒ 任务终态
  `failed`（code=`upstream_turnstile_required`，503 语义，**部署问题**）。
- 编辑器实测**长期 pending**（>10 分钟，§10.4）⇒ 超时预算独立为
  `TASK_TIMEOUT_I2I=3600`（生成链路的 780s 不适用）。
- 比例档对图生图**无效**（上游无该字段，产物跟随参考图）⇒ 受理会留痕说明。
- 参考图由本服务**代取**（URL/data URI → 下载 → 转存上游存储）：
  拒内网/保留地址、限 10MB、非 `image/*` 拒绝 ⇒ 任务终态 `failed`
  （code=`invalid_image`）。**仍为同一异步两段式**：受理回 `task_id`，轮询查结果。

### 3.2 上游实际能力（速查）

| 项 | 值 |
|---|---|
| 比例档 | `1:1`(1024²) / `3:4`(768×1024) / `4:3`(1024×768) / `9:16`(576×1024) / `16:9`(1024×576) |
| 默认比例 | `1:1` |
| 张数 | 恒 1 |
| 每档产出像素 | 见 `aspect_ratio_pixels`（`GET /capabilities` 可查）；`1:1`/`4:3` 已**实测**（1024×1024 / 1024×768） |
| 计费 | 无（免费额度制）。限流 = **单 IP 在途任务上限 3**（实测，见 `docs/UPSTREAM.md` §9） |

> `model` 枚举（`GET /capabilities` 的 `capability` 段）现在有 **3 条**：`image-t2i` 与 `image-i2i`
> （均 `available: true`）与 `image-upscale`（`available: false`，带 `reason` 与 `enable`）。
> 🔴 「列出来但明说不可用」是刻意的：调用方能**发现**这条路、知道为什么没开、以及怎么开 ——
> 这比 400 之后以为拼错了要诚实得多。命中未启用能力时受理返回 **503**（部署状态）。
> 🔴 `image-i2i` 已于 2026-09-21 启用（上游 `/api/ai-photo-editor`，三步流：代取参考图 →
> 转存站点存储 → 建任务），仍走同一套异步两段式契约。
> ⚠️ `/v1/models` **只列可调用的 2 条** —— 想看未启用能力必须读 `/capabilities`。

### 刻意缺席的能力（**必须响亮失败，不做假能力**）

| 请求 | 行为 | 理由 |
|---|---|---|
| `image` 传非空数组 + 默认/`t2i` 模型 | `400 invalid_parameter`（`param="image"`，**提示写明用 `image-i2i`**） | 文生图收下图照跑 = 骗调用方；图生图有正路（显式 `model="image-i2i"`），不猜意图 |
| `n > 1` | `400 invalid_parameter`（`param="n"`） | 上游一次任务只出一张。**要么全给要么明确拒绝**，不存在"收下 10 张只给 1 张" |

`GET /capabilities` 会带出 `DELIBERATE_ABSENCES`，把这两条写明。

---

## 4. 张数 `n` 的语义

- 不传 ⇒ **1**。
- 传 `1` ⇒ 生效。
- 传 `>1` / `<1` / 非整数 ⇒ **`400`**。

🔴 不通过"提交 N 次上游任务"来实现 `n>1` —— 那会把免费额度消耗乘 N，
且上游有在途互斥 ⇒ 第 2 张起必然撞 `FREE_TASK_*`，等于用一个必然失败的设计骗调用方。

---

## 5. `degradations`（本服务的加性扩展）

任何"请求了 A、实际做了 B"都会出现在这里（**仅非空时出现该键**）：

```json
{ "degradations": ["未指定模型 ⇒ 使用默认 image-t2i。",
                   "size=1024x1024 ⇒ aspect_ratio=1:1（本服务按宽高比吸附到上游 5 档枚举）。",
                   "quality='high' 上游无此能力 ⇒ 忽略。"] }
```

来源四类：**参数换算/吸附**、**默认值代入**、**认得但做不到的字段**、**上游超时后仍未知**。

🔴 **静默降级 = 让人按 A 的预期拿到 B。** 凡会改变结果或消耗额度的取舍都必须留痕，
并且**必须在同一个响应上** —— 只在日志里写等于没写。

---

## 6. 错误信封

```json
{ "error": { "message": "...", "type": "...", "code": "...",
             "param": "可选", "retry_after": 可选, "detail": "可选" } }
```

| `code` | HTTP | 含义与**下一步** |
|---|---|---|
| `invalid_parameter` | 400 | 请求写错了。`param` 指出是哪个字段 |
| `content_policy_violation` | 400 | 上游拦截 ⇒ 换 prompt |
| `task_not_deletable` | 400 | 未终态任务不能删（上游**没有取消端点**） |
| `invalid_api_key` | 401 | 调用方的 Key 不对 |
| `task_not_found` | 404 | 不存在或不属于本 Key |
| `upstream_browser_task_active` | 429 | 上游的浏览器维度墙（**本仓从未触发过**）⇒ 见 §10 |
| `upstream_ip_task_active` | 429 | 上游：**同 IP 在途任务已达 3 个**（实测上限）⇒ 等名额释放，或换出口 IP；见 §10 |
| `upstream_rate_limited` | 429 | 上游通用限流 ⇒ **可退避重试** |
| `upstream_egress_unavailable` | 502 | **出口连不上**（代理拒绝认证 / DNS / 连接被拒）⇒ 多出口会自动换路；单出口才退避 |
| `upstream_error` | 502 | 上游 5xx / 非 JSON / WAF 页 |
| `capability_unavailable` | 503 | 能力不可用 |
| `upstream_timeout` | 504 | 上游超时 |
| `upstream_turnstile_required` | 503 | 上游把 Turnstile 开关打开了而本服务没有 token ⇒ **部署问题** |

**任务级**错误码（出现在 §2.3 的 `error` 对象里，HTTP 仍是 `200`）：

| `code` | 含义与**下一步** |
|---|---|
| `task_failed` | 上游明确回了 failed |
| `task_timeout` | 等待超过预算（文生图 780s / 图生图 3600s） |
| `invalid_image` | 图生图的参考图不合格（下载失败 / 超 10MB / 非 `image/*` / 指向内网） |
| `submit_unknown` | 🔴 **服务在「提交上游」的窗口内重启**：上游**可能已建了任务**（免费额度可能已扣），但本地没有任务 ID 可续跟。为不白烧额度，服务**不会自动重提** —— 需要重试请**重新提交一次**（会消耗新的额度）。这个窗口**无法被消除**（上游没有幂等键），只能留痕 + 保守处理 |

`Retry-After` **只在是真的才知道**的时候给 —— 编一个数字等于伪造事实。
上面两个 `*_task_active` 是「在途互斥」，**等待时长取决于别人**，所以**不给** `Retry-After`。

---

## 7. Turnstile 预案（当前不触发）

上游把开关硬编码成关闭（见 `docs/UPSTREAM.md` §5），但**开关随时可能被打开**。

| 部署情况 | 行为 |
|---|---|
| 开关关（现状） | 正常生成，发 `turnstile_token: null` |
| 开关开 + 配了 `IMAGEFREE_TURNSTILE_TOKEN` | 用配置的 token 提交 |
| 开关开 + 没配 token | 任务终态 `failure`，`code=upstream_turnstile_required`（503 语义写进任务错误）；**不静默重试**（重试只会加深风控） |

判决依据：上游若开始要求 token，会表现为提交被拒 —— **必须响亮失败并指明是部署问题**，
而不是让调用方以为 prompt 写错了。

---

## 8. 删除

| 任务状态 | `DELETE` 行为 |
|---|---|
| 非终态（`queued` / `in_progress`） | **`400`** —— 上游**没有取消端点** |
| 终态 | `200 {"task_id": "...", "status": "deleted"}`，删掉本地记录 |

🔴 未终态任务的删除**必须响亮失败**。本地置"已取消"就返回成功有三个后果：
① 上游任务继续跑、继续占额度，而调用方以为停了；② 本地与上游状态永久不一致；
③ 没有任何出口能看出来。

---

## 9. 测试与运行

```bash
python -m pytest -q          # **零真实上游调用**
```

- **零真实上游调用**：所有用例注入假上游（`httpx.MockTransport` 覆盖全部端点），
  一个字节都不发出去。提交任务会消耗免费额度，这条红线由夹具保证。
- **缺依赖就响亮失败，不静默跳过** —— 跳过会让人把"没跑"当成"跑过了"。

---

## 10. 出口与代理（运维能力，**不属于对外契约**）

上游的限流有**两层**（IP 与浏览器身份，见 `docs/UPSTREAM.md` §2.2.1），
所以本服务支持配置多个**出口**：

```bash
IMAGEFREE_PROXIES=cn=socks5h://user:pass@10.0.0.1:2088,us=http://10.0.0.2:8080
IMAGEFREE_PROXY_RETRIES=3     # 撞墙后在同一出口当场重试几次（轮换池：下一连接大概率是新 IP）
IMAGEFREE_PROXY_COOLDOWN=60   # 撞墙的出口冷置多少秒，后来的任务优先换别的出口
IMAGEFREE_POOL_FANOUT=3       # 轮换型池子：一个入口展开成 3 个出口 ⇒ 容量 3×3=9
```

| 语义 | 行为 |
|---|---|
| 留空 | 单一直连出口，**与加代理之前逐字一致** |
| 一个出口 = 一个客户端 | 每个出口独立 HTTP 客户端（独立连接池与 cookie jar ⇒ 彼此隔离） |
| 🔴 **默认全走池子** | `IMAGEFREE_USE_DIRECT=0`（**默认**）⇒ 配了代理时**直连不出现在出口清单里**（不烧本机出口 IP 的额度）。设 1 = 直连作为**第一个出口**，它的名额满了、溢出任务才走池子 |
| 出口按**配置顺序**填 | 先填满前一个出口的名额，再轮到下一个（顺序即优先级） |
| 一个任务一个出口 | 提交时选定并**落库**，之后**所有轮询都走它**（少一层不确定性，好对账） |
| 单出口在途名额 | **3**（`IMAGEFREE_IP_CONCURRENCY`；上游按 IP 记账，实测见 `docs/UPSTREAM.md` §9） |
| 有效容量 | `min(IF_CONCURRENCY, 出口数 × 3)` ⇒ **加出口能直接扩容量**（`IF_CONCURRENCY` 默认 1，会把它压住 —— 想真并行要同时调大它） |
| 🔴 **池子扇出** | `IMAGEFREE_POOL_FANOUT=N` ⇒ 每个**代理**出口复制成 N 个（各自独立客户端 ⇒ 独立 cookie jar）⇒ 容量 `3N`。默认 1（不展开）；直连不参与；上限 16。⚠️ 只对**轮换型池子**有意义（每连接换 IP）；固定出口代理展开只是重复计数 |
| 撞墙（429 类） | 该次**上游什么都没创建** ⇒ 先原地重试（轮换池：新连接≈新 IP）、再换出口，**零额度损失** |
| 出口连不上 | `upstream_egress_unavailable` ⇒ **冷置该出口并换下一条路**（直连坏了也一样，池子顶上），且**不原地重试**同一条坏路 |
| 超时 | **不换出口**（"可能已建成"）—— 换出口重发会真的建出两条任务 |
| 配置写错 | **启动失败**（scheme 不支持 / 标签重复 / 同一代理写两遍）。静默忽略=配了个寂寞 |

边界（**对外响应形状完全不变**）：加代理只影响"用哪个 IP / 哪个身份去提交"，
不新增端点、不改 `degradations`、不改任何状态码。观测面在 `/stats` 的 `egress` 段
与 `/readyz` 的 `upstream.egress`（**代理凭据已打码**）。

⚠️ 出口是**明确知情的取舍**：它用"多 IP/多身份"绕过上游的免费额度记账，
放大的是上游的成本。是否这么做由部署方决定。

### 🔴 启动冒烟必须关协调器

```bash
COORDINATOR_ENABLED=0 gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

协调器默认开启，而**提交任务会消耗上游免费额度**。做"服务能不能起来"的冒烟时
如果不关它，它会代替你向上游提交任务。
