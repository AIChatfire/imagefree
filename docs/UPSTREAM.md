# imagefree.net 上游契约（逆向所得）

> 本文件记录**上游真实形态**，含取证方式与置信度。对外契约在 `docs/INTERFACE.md`。
> 证据来源：2026-09-21 浏览器抓包 2 条 + 线上只读探测 3 次 + Next.js 前端 chunk 静态分析。
> 🔴 **凡标「未取证」的，一律不许当成已知写进代码判断。**

---

## 1. 站点与链路

| 项 | 值 | 取证方式 |
|---|---|---|
| 站点 | `https://imagefree.net`（多语言，中文在 `/zh`） | 抓包 |
| 前端 | Next.js（turbopack 分包，13 个 chunk） | 抓包 + 静态分析 |
| 边缘 | Cloudflare（`server: cloudflare`，`cf-cache-status: DYNAMIC`） | 响应头实测 |
| 后端 | 三个 API：`/api/generate`、`/api/generate/status`、`/api/geo`（另有 `/api/auth` = better-auth 登录，与生成链路无关） | chunk 静态分析 |
| 产物存储 | Cloudflare R2 公开桶 `pub-62e693a7058040f98bba94ed1d6f880b.r2.dev/images/<uuid>.png` | 状态接口实测回包 |
| 计费 | **无**。免费额度制，靠浏览器 ID + IP 双维度限流 | 前端文案 + 错误码语义 |

**没有的东西**（逆向结论里同等重要）：没有登录要求、没有签名、没有 CSRF token、
没有 `Authorization` 头、没有任何计费字段。

---

## 2. 提交生成

```http
POST https://imagefree.net/api/generate
Content-Type: application/json

{"prompt": "cat", "aspect_ratio": "1:1", "turnstile_token": null}
```

**成功响应**（`200`）：—— **实测确认**（2026-09-21 真实出图，见 §8）：
HTTP 200 + UUID 形态的 `taskId`，且**响应头带 `Set-Cookie: imagefree_free_generation_id=…`**。

```json
{ "taskId": "adf689ee-0a7b-4418-8567-c1f0c66317b9" }
```

**失败响应**（HTTP 状态码**未取证**，前端只读 body）：

```json
{ "error": "人类可读信息", "errorCode": "FREE_TASK_IP_ACTIVE" }
```

前端判据（`95ded06a07159e49.js`）：先看 `i.error`，再看 `i.taskId` 是否存在 ——
**`taskId` 缺失即视为失败**（文案 `Failed to get task ID`）。

### 2.1 请求字段（上游只认这三个）

| 字段 | 必需 | 取值 |
|---|---|---|
| `prompt` | 是 | 字符串 |
| `aspect_ratio` | 是（前端恒发） | `1:1` / `3:4` / `4:3` / `9:16` / `16:9`，**共 5 个** |
| `turnstile_token` | 是（键必须在） | 当前**恒为 `null`**，见 §5 |

比例档与像素对应（来自 UI 选项表，**这是上游唯一的尺寸语义**）：

| `aspect_ratio` | UI 标称尺寸 |
|---|---|
| `1:1` | 1024×1024（**前端默认值**） |
| `3:4` | 768×1024 |
| `4:3` | 1024×768 |
| `9:16` | 576×1024 |
| `16:9` | 1024×576 |

⚠️ 上游请求体里**只有比例**，没有宽高字段 ⇒ 实际出图像素只能靠**下载产物量**。
已实测两档（见 §8）：`1:1` → **1024×1024**、`4:3` → **1024×768** ⇒ 与上表标称值一致。
其余三档仍是**标称值**（未下载核对）。

### 2.2 限流：错误码即语义

前端把三个 `errorCode` 映射成三句人话（`95ded06a07159e49.js`）：

| `errorCode` | 前端文案语义 | 触发条件 |
|---|---|---|
| `FREE_TASK_IP_ACTIVE` | networkTaskLimitReached | ✅ **已实测**：同一 IP 上**同时在跑的任务数已达 3** |
| `FREE_GENERATION_ACTIVE` | generationAlreadyInProgress | 🔴 **从未触发**（本仓 10 次真实提交都没见过） |
| `FREE_TASK_BROWSER_ACTIVE` | generationAlreadyInProgress | 🔴 **从未触发** |

🔴 **实测推翻了本仓最初从错误码名字推出的「在途互斥（同一 IP 只允许 1 个任务）」**（详见 §9）：

- 5 条**并发**提交 → **3 条 `200` 受理 + 2 条 `429 FREE_TASK_IP_ACTIVE`**；
- 被受理的那 3 条**并行执行**、各自出图；
- 在途任务全部终态后**立刻**再打 1 条 → **`200` 受理**（名额已释放）。

⇒ 它是**按 IP 记的并发上限（实测 3）**：既不是"一个 IP 只能跑一个"，也不是"每天 N 张"的日配额。
**加出口（换 IP）就是直接加容量**，一格出口 = 3 个在途名额。

被拒时的完整形态（本仓首次取证）：

```http
HTTP/1.1 429
Retry-After: 7200
{"error": "…", "errorCode": "FREE_TASK_IP_ACTIVE"}
```

🔴 **`Retry-After: 7200`（2 小时）与实测不符**：名额在任务终态后立刻恢复（实测 160s 内）。
⇒ 它只能**如实上报**给调用方，**不能**拿去做"等 2 小时"的内部退避（那会白白丢掉可重试的机会）。
本服务内部用自定的 15/30/60/120s 退避序列（`app/coordinator.py`），
同时把上游原值放进错误信封的 `retry_after` 字段。

### 2.2.1 「加代理」的价值：**实测确认在 IP 维度**

| 维度 | 错误码 | 换出口 IP 有用吗 | 说明 |
|---|---|---|---|
| **IP**（已实测触发） | `FREE_TASK_IP_ACTIVE` | ✅ **有用** | 名额按 IP 记账 ⇒ 加一个出口 = 多 3 个在途名额 |
| 浏览器身份（从未触发） | `FREE_GENERATION_ACTIVE` / `FREE_TASK_BROWSER_ACTIVE` | 未知 | 见下 |

🔴 关于"浏览器身份"这一层，实测**否掉了本仓最初的解释**：

- `imagefree_free_generation_id` **每提交一次就换一个新的 UUID**
  （5 条并发拿到 5 个互不相同的值，`Expires` 一年）⇒ 它**不是**稳定的浏览器身份；
- 上游也**没有**拿它限流：那 5 条并发各带一个新身份，前 3 条全部放行。
- ⇒ 那两个 `*_ACTIVE` 码的真实触发条件**仍未取证**（可能属于登录/账号维度，本链路用不到）。
- ⇒ **不要**把容量建模在"身份"上；把它建模在 **IP × 3** 上。

本服务对应的实现（`app/egress.py`）：一个出口 = 一个 httpx.Client（独立连接池 + 独立 cookie jar），
出口清单来自 `IMAGEFREE_PROXIES`；容量 = `min(IF_CONCURRENCY, 出口数 × 3)`。
独立 cookie jar 现在的作用是**隔离**（不同出口不串 cookie），不再是"身份配额"。

⚠️ 轮换型代理的语义是**每 TCP 连接一个出口 IP**，所以"一个任务全程一个 IP"**不成立**。
对本链路无害且**已实测**：查询接口不带任何 cookie 也回 200（见 §3），限流只作用在**提交**上。
⇒ 提交时选定的出口仍**焊在任务上**（轮询走回它），但理由改成"少一层不确定性 + 查询也好对账"，
而不再是"身份住在那个 jar 里"。

### 2.3 身份 cookie `imagefree_free_generation_id`

- 形态：UUID（抓包值 `d85715f4-2017-4c0d-a553-87299f531191`）。
- ✅ **来源已闭环（2026-09-21 实测）**：由 `POST /api/generate` 的响应 `Set-Cookie` 下发。
  证据：服务启动时该出口没有值（`/readyz` 的 `identity_acquired=false`），提交成功后再查变 `true`。
- 🔴 **而且每次提交都换一个新的**：5 条并发拿到 5 个互不相同的 UUID，
  `Set-Cookie: imagefree_free_generation_id=<uuid>; Path=/; Expires=<一年后>`。
  ⇒ 它**不是**稳定的浏览器身份，上游也没拿它限流（见 §2.2.1）。
  **别**把它当账号/设备 id 用，也别指望"固定它"能绕过任何墙。
- 实测：`GET status` **不带任何 cookie 也回 200**（见 §3），所以 cookie 不是查询的必要条件。

---

## 3. 查询状态

```http
GET https://imagefree.net/api/generate/status?taskId=<taskId>
```

**实测回包**（2026-09-21，任务已完成）：

```json
{ "status": "completed",
  "image": "https://pub-62e693a7058040f98bba94ed1d6f880b.r2.dev/images/e34fe638-ae05-43c4-bf7a-11f61460afff.png",
  "progress": 100 }
```

| 字段 | 说明 |
|---|---|
| `status` | `completed` / `failed` / 其它（非终态取值**未取证**，推测 `pending`/`processing`） |
| `image` | 成功时给出，R2 直链 |
| `progress` | 0..100 整数 |
| `error` | 失败时可能给（前端 `if (i.error)` 优先判） |

**实测结论**：
- 不需要任何 cookie / 鉴权头即 200；
- `?taskId=` 是查询参数，大小写敏感（照抄前端写法）。

### 3.1 前端轮询节奏（照抄，做适配时对齐）

间隔序列（毫秒，共 29 轮）：

```
6000, 4000, 10000, 15000, 20000, 30000 ×24
```

⇒ 首轮 6s，其后逐步退避到 30s，总预算约 **775 秒**（≈13 分钟）后判超时。
判超时的动作是前端报 `Generation timeout` —— **任务在上游是否仍会跑完，未取证**。

---

## 4. 前端 chunk 静态分析结论

| 结论 | 证据位置（chunk 偏移） |
|---|---|
| 生成调用：`fetch("/api/generate", {method:"POST", body: {prompt, aspect_ratio, turnstile_token}})` | `95ded06a07159e49.js` @25468 |
| 成功判据 `i.taskId`；缺失即失败 | 同上 @25620 |
| 三个限流 `errorCode` 的文案映射 | 同上 @25500 |
| 轮询间隔序列 | 同上 @26273 |
| 状态判据：`completed`+`image` → 出图；`failed` → 失败；轮数用尽 → 超时 | 同上 @26400 |
| 比例枚举 5 项 + UI 默认 `1:1` | 同上 @29159 |
| Turnstile 组件：`window.turnstile.render(div, {sitekey, size:"normal", callback, "expired-callback"})` | 同上 @1076 |
| 🔴 **Turnstile 开关硬编码关闭**：组件内 `let D=false`（常量），widget 渲染条件 `D && <TurnstileWidget/>`，提交按钮禁用条件含 `D && !R` | 同上 @25100 / @29920 |
| sitekey 常量 | 同上：`0x4AAAAAACE-XLGoQUckKKm_` |
| `/api/geo` → `{"isChina":true/false}`，仅控制中国区推广横幅（推广码 `k475703`） | `7f28c1a9432e819b.js` @31003 |
| `/api/auth` 是 better-auth（登录/注册），与生成无关 | `ee90bdc8df01a3b6.js` @5931 |

---

## 5. Turnstile：**主生成页**是死开关（工具页则是活的）

> ⚠️ 先分清两处：**主生成页**（`/api/generate`）的 Turnstile 是死的，本服务当前就靠这一点运行；
> 而**三个工具页**（`/api/image-upscaler` 等）的 Turnstile 是**强制**的（见 §10）。

- 前端 `D=false` 是**硬编码常量**（不是从接口读的配置）⇒ 当前**所有用户**都不渲染 widget，
  `turnstile_token` 恒发 `null`。
- **服务端实测放行** `null` token：本日 10+ 次真实提交全部带 `"turnstile_token": null`
  并成功出图 ⇒ 主生成端点上服务端**确实没有强制校验**。
- 开关位置已定死：`D` 是组件内的**常量** `false`（`95ded06a07159e49.js`：
  `[R,T]=(0,i.useState)(null),D=false,G=…`），**不是 state、也不是从接口读的配置**。
  渲染条件是 `D&&<TurnstileWidget/>`、按钮禁用条件是 `P||!j.trim()||D&&!R` —— 两处都被 `D=false` 短路。
- 🔴 **风险**：这是一个随时可能被打开的开关（而且**在站点的其它端点上已经打开了**，
  见 §10.1）。一旦本端点也打开，本服务必须能提供 token，否则提交会被拒；
  实测的拒绝形态是 **HTTP 400 + `{"error":"Human verification failed…"}`**
  （**不是** 401/403）。应对写在 `docs/INTERFACE.md` §7 与 `.env.example`
  （`IMAGEFREE_TURNSTILE_TOKEN` 手动注入口 + 可选的自动铸造）。

---

## 6. 能力边界

> ⚠️ 本节只讲**本服务已适配的那一条链路** `/api/generate`。
> 站点**另外还有三个工具**（去背景 / 高清放大 / AI 图片编辑器），各有独立端点与上传步骤，
> 见 §10 —— 「图生图」与「2K/4K」的答案在那边，不在本链路里。

`/api/generate`（文生图）**有**：
- 文生图（t2i），单张，5 个固定比例，免费。

`/api/generate` **没有**（**不许假装有**）：
- **图生图 / 参考图**（提交体里根本没有图片字段）⇒ 但站点有 `/api/ai-photo-editor`（见 §10）；
- **分辨率档位（2K/4K）**（无对应字段；产出固定 ≈1024）⇒ 站点有 `/api/image-upscaler`（见 §10）；
- 一次多张（`quantity` 之类的字段不存在，一次任务=一张图）；
- 质量档位 / 风格 / 水印开关（无对应字段）；
- 取消端点（只有查询，没有 delete/cancel）⇒ 提交后**无法撤回**；
- 任何模型选择（上游未暴露模型名，服务端用哪个模型**未取证**）。

---

## 7. 未取证清单（别当已知）

1. **是否存在按时间窗的速率限额**（如"每小时 M 次提交"）。已知并发上限 = 3/IP，
   且名额随任务终态立刻释放（§9）⇒ **不是**日配额；但本次 3 分钟内只打了 10 次提交，
   更长的窗口**没测到**。
2. ~~身份 cookie 的下发时机~~ ✅ **已闭环**（提交响应 `Set-Cookie`；且**每次提交换新的**，见 §2.3）。
3. 非终态上游 `status` 的确切取值（本服务报 `in_progress`；上游侧只见过 `completed`）。
4. ~~提交失败时的 HTTP 状态码~~ ✅ **已闭环：`429` + `Retry-After`**（见 §2.2）。
5. 产物 URL 的**有效期**；以及 `3:4` / `9:16` / `16:9` 三档的实际像素
   产物的实际像素（`1:1` → 1024×1024、`4:3` → 1024×768 已**下载核对**，见 §8）；
   以及 §10 那三个工具各自的输出形态与放大倍率。
6. 上游实际使用的图像模型。
7. Turnstile 被打开后的校验细节；以及 `FREE_GENERATION_ACTIVE` /
   `FREE_TASK_BROWSER_ACTIVE` 的**真实触发条件**（从未触发过，见 §2.2.1）。
   另：`ai-photo-editor` 的任务会**长期 pending**（10 分钟+，见 §10.4）——
   它最终会不会完成、平均耗时多少，仍**未取证**。
8. 前端 775s 超时后，上游任务是否会继续跑完。
9. **"3"这个数是按 IP、还是按出口的 ASN / IP+端口 记账**（多出口对照实验**没做** ——
   当时代理池凭据被代理侧拒绝，见 `.workbuddy/memory/`）。

---

## 8. 端到端真实出图实测（2026-09-21）

一次真实提交（**消耗 1 次免费额度**；由本服务的协调器发出，出口 = 直连）。
完整证据链（服务日志时间戳）：

| 时刻 | 事件 |
|---|---|
| `11:51:11.960` | 受理（本地落库，`POST /async/v1/images/generations` 只回 `task_id`） |
| `11:51:14.632` | 提交上游成功 → 上游 `taskId` = `03ec1ce8-37df-4616-9030-845c1829f0e0` |
| `11:53:42.070` | 上游回 `completed` → 任务终态 success |

**结论**：

1. **出图耗时实测 ≈ 147s**（提交→成图；受理→成图 ≈ 150s）。
   比本仓最初的粗估（20~60s）长得多 ⇒ `POLL_INTERVAL=5`（查询零额度）与
   `TASK_TIMEOUT=780` 都是合理取值（147s ≪ 780s，但预留了 5× 余量）。
2. **上游 `taskId` 是 UUID 形态**，与抓包一致；本服务原样存库并用于轮询。
3. **产物**：`1:1` 档实测 **1024×1024 PNG**（≈1.06 MB），与前端 UI 标称一致；
   落在 Cloudflare R2 公开桶，直链可直接下载（HTTP 200）。
4. **上游完成态原始回包**（零额度复查所得）：

```json
{"status":"completed","image":"https://pub-62e693a7058040f98bba94ed1d6f880b.r2.dev/images/3dd00b9f-03cf-4692-9bee-74a87ae65bcd.png","progress":100}
```

5. **身份 cookie 由提交响应下发**（`/readyz` 的 `identity_acquired` 由 false 变 true）——
   见 §2.3，这条原假设由此从「未取证」变为「已闭环」。
6. 全程**未触发任何限流错误**（`FREE_*_TASK_ACTIVE` 一次都没出现）——
   但只跑了一条任务，所以**不能**据此推断额度上限（见 §7）。

产物留档：`bench/live-1x1-red-apple.png`、`bench/live-4x3-toy-boat.png`（`bench/` 已被 .gitignore 忽略）。

### 8.1 第二轮：非成规尺寸（比例与像素都不在成规表里）

目的：把 `size` 的**就近吸附**在真实链路上跑一遍（不是只跑单测）。

```
POST /async/v1/images/generations
{"prompt": "a wooden toy boat on a table, overhead view", "size": "1500x1000"}
```

| 项 | 实测值 |
|---|---|
| 请求 | `size=1500x1000`（比例 **3:2**，既不等于任何成规比例，像素也不在成规表里） |
| 本服务吸附 | `aspect_ratio=4:3`（按宽高比的**对数距离**：`3:2` 到 `4:3` 比到 `16:9` 更近） |
| 留痕 | 查询响应里写明「实际产出是 1024×768，不是你请求的 1500×1000」 |
| 上游耗时 | 受理→出图 ≈ 85s |
| **产物实测像素** | **1024×768**（730 KB，PNG）—— 与留痕声明**逐字一致** ✅ |

旁证（零额度、本地拒绝）：`aspect_ratio="3:2"` ⇒ `400 invalid_parameter`
（信息里列出 5 档可选值）—— 「非成规**比例**直接拒，非成规**像素**就近吸附」这条边界是刻意的。

---

## 9. 并发压测：单 IP 的频控形态（2026-09-21）

**目的**：搞清"加代理"到底有没有用、容量该怎么建模。
**手段**：`scripts/burst.py` —— **原始 HTTP、不经本服务的闸门**（经闸门就被串行化了，测不到上游本身的墙）。
**代价**：共 10 次提交（8 条被受理、2 条撞墙），如实记录在案。

### 9.1 实验与结果

| 轮次 | 手法 | 结果 |
|---|---|---|
| ① | 3 条**并发**、共用一个身份 | **3/3 全部 `200` 受理**，并行执行、各自出图 |
| ② | 5 条**并发**、共用一个身份 | **3 条 `200`** + **2 条 `429 FREE_TASK_IP_ACTIVE`**（`Retry-After: 7200 / 7199`） |
| ③ | ① 全部终态后**立刻**再打 1 条 | **`200` 受理** ⇒ 名额已恢复 |

②的逐条回包（实测原文，节选）：

```
   1   200   0.97s  ✅ taskId=90f75f42-…
   2   200   0.75s  ✅ taskId=2bc6cc43-…
   3   429   1.06s  ❌ FREE_TASK_IP_ACTIVE   Retry-After=7200
   4   200   0.95s  ✅ taskId=510ef1d2-…
   5   429   1.76s  ❌ FREE_TASK_IP_ACTIVE   Retry-After=7199
```

### 9.2 结论

1. **单 IP 在途上限 = 3**：同刻 5 条里只有 3 条能成，且成功的那 3 条**真的并行在跑**。
2. **是并发数上限，不是日配额**：③ 证明名额随任务终态立刻释放。
3. ⇒ **加代理可直接扩容量**：N 个出口 ⇒ 最多 `3N` 个在途任务。
   这就是"遇到频控可以加代理"的**实测依据**。
4. **`Retry-After: 7200` 不可信**（③ 在 160s 内就能再提交）⇒ 只如实上报，不据此长等。
5. **`imagefree_free_generation_id` 不是限流维度**：5 条并发各带一个新身份，前 3 条全放行。
6. **并发会拉长单条耗时**：同一批里最快 63.9s、最慢 160.7s（单条时约 147s）
   ⇒ 上游是"接了就排队跑"，并发越高单条越慢。容量规划要按"总吞吐"算，不是按单条延迟。

### 9.3 对实现的直接改动

- 新增 `IMAGEFREE_IP_CONCURRENCY=3`（单出口在途名额）。
- 容量公式修正为 `min(IF_CONCURRENCY, 出口数 × 3)`（原来错写成 `min(IF_CONCURRENCY, 出口数)`）。
- 出口选择：**按配置顺序填**（直连排第一位 ⇒ 直连优先，满了才溢出到池子；
  见 `docs/INTERFACE.md` §10）。
- 撞墙（429）仍按"先原地重试 → 再换出口 → 再退避"处理，但**不按 `Retry-After` 长等**。


---

## 10. 站点的其它工具（2026-09-21 逆向；同日 `ai-photo-editor` 已接入本服务）

> 🔴 **接入状态（2026-09-21）**：`ai-photo-editor`（图生图）已接入 —— `model="image-i2i"`
> + `image` 恰 1 条，本服务代取参考图并转存站点存储（三步流如下），部署需
> `IMAGEFREE_TURNSTILE_TOKEN`，任务超时预算独立为 3600s（§10.4）。
> `image-upscaler` / `background-remover` 仍未接入。

发现路径：首页 nav 数据里列着三个入口
（`7f28c1a9432e819b.js`：`/background-remover`、`/image-upscaler`、`/ai-photo-editor`），
三个路由都 HTTP 200，且**各有一套独立端点**（在各自路由的 chunk 里抓到）。

三个工具都是**同一个三步模式**（与"受理→轮询"两段式的差别在开头多了上传）：

```
1) POST /api/<tool>/upload-url   {filename, content_type}   → {uploadUrl, publicUrl}
2) PUT  <uploadUrl>              <二进制文件>                 （直传存储，不走本站 API）
3) POST /api/<tool>              {image_url: publicUrl, ...} → {taskId}
   然后 GET /api/<tool>/status?taskId= 轮询（判据与生成一致：status=completed && image）
```

| 工具 | 建任务请求体 | 上传限额 | 说明 |
|---|---|---|---|
| `/api/ai-photo-editor` | `{image_url, prompt, turnstile_token}` | **10 MB** | 🔴 **指令式图生图/编辑** —— 站点里唯一"参考图 + 文字指令"的入口 |
| `/api/image-upscaler` | `{image_url, turnstile_token}` | **1 MB** | 超分放大。⚠️ **没有倍率/尺寸参数** ⇒ 倍率由服务端定；文案宣称"几秒钟达到 **4K** 效果"（**营销话术，未取证**） |
| `/api/background-remover` | `{image_url, …}`（未细看） | （未细看） | 去背景 |

共同点（与生成链路一致，可复用已有结论）：
- **无鉴权**；限流是同一套 `FREE_TASK_IP_ACTIVE` / `FREE_TASK_BROWSER_ACTIVE`
  （`ai-photo-editor` 的 chunk 里逐个映射，与首页同款）；
- 轮询判据同样是 `completed && image`；`image-upscaler` 前端给 **30 轮 × 5s = 150s** 预算；
- 前端一律**先 `PUT` 直传存储、再拿 `publicUrl` 建任务**
  ⇒ 上游只接受"它自己存储里的 URL"，不接受任意外链。

### 10.1 🔴 这三个工具**强制 Turnstile**（实测，2026-09-21）

上传段已经跑通（**零额度**，因为卡在建任务那一步）：

```
  上传：live-4x3-toy-boat.png（image/png，730 KB）
  upload-url → HTTP 200：{"uploadUrl": "https://aitools99.cd9f97a96f0ec7d68e41c244a5b28f03.r2.cloudflarestorage.com/upscaler/1789964456074-….png?X-Amz-Algorithm=AWS4-HMAC-SHA256&…",
                          "publicUrl": "…"}
  PUT 直传 → HTTP 200                      ← 直传存储，**无需任何鉴权**
  建任务 → HTTP 400：{"error":"Human verification failed. Please complete the challenge and try again."}
```

⇒ **同一站点、不同端点的安全策略不同**：

| 端点 | 前端 widget | 服务端校验 | 证据 |
|---|---|---|---|
| `/api/generate` | **不渲染**（硬编码 `D=false`） | **放行** | 本日 10+ 次 `null` token 真实出图 |
| `/api/image-upscaler` 等三个 | 渲染（同 sitekey `0x4AAAAAACE-XLGoQUckKKm_`） | 🔴 **强制** | 上面那条 400 |

含义（对"要不要接入工具类能力"是决定性的）：
- 接入 `ai-photo-editor` / `image-upscaler` **不是"加个上传段"就完**，
  还必须能**提供 Turnstile token**；本服务的 `IMAGEFREE_TURNSTILE_TOKEN` 正是为此留的口子
  （见 `docs/INTERFACE.md` §7），但**自动铸造**需要一个 headless 浏览器
  （可用技能 `cf-turnstile-minting`）。
- 上传细节（已取证）：`upload-url` 返回的是 **R2 预签名 PUT**（域名形如
  `aitools99.<account>.r2.cloudflarestorage.com`，路径前缀 = 工具名 `upscaler/…`）；
  `PUT` 成功但**这不代表任务被创建** —— 额度只在建任务成功时消耗。

### 10.2 ✅ 铸 token 后跑通：**放大是固定 2 倍**（实测，2026-09-21）

铸 token 的办法（配方已落盘：`scripts/mint_turnstile.cjs`）：
**本机真 Chrome**（非 headless、`--disable-blink-features=AutomationControlled`）打开工具页，
等 widget 自己解完（**实测 ~3s**），`window.turnstile.getResponse()` 取出 token（816 字符）。
⇒ token **有效期只有几分钟**，必须"现铸现用"（铸完立刻建任务）。

带 token 重跑放大工具（消耗 1 次额度）：

```
  上传：live-4x3-toy-boat.png（1024×768，730 KB）
  upload-url → HTTP 200 → PUT 直传 HTTP 200
  建任务 → HTTP 200：{"taskId":"6134ee01-…","status":"pending"}     ← 上一轮的 400 就是缺 token
  [+7.7s] {"status":"completed","progress":100,"image":"https://pub-efef6010….r2.dev/images/d485d23a-….png"}
  下载 → HTTP 200，2799 KB
  🔴 产物真实尺寸：2048×1536 PNG
```

| 问题 | 实测答案 |
|---|---|
| 放大倍率 | **固定 2×**（1024→2048 长边）。接口没有倍率参数 ⇒ 服务端定死，无法选 4× |
| "4K 效果" | 营销话术：实际是 **2K**（2048 长边）。要 4096 得把 2× 的产物再喂一遍（未试） |
| 耗时 | **7.7s**（生成 ≈150s，放大只要几秒 —— 它是纯超分，不是重画） |
| 产物落点 | **另一个 R2 公开桶**（`pub-efef6010…`；生成产物在 `pub-62e693a7…`） |
| 响应字段 | 建任务 `{taskId, status:"pending"}`；轮询 `{status, progress, image}` —— 与生成链路同形 |

### 10.3 ✅ 4K 可达：**两次 2× 链式放大**（实测，2026-09-21）

把 §10.2 的 2048×1536 产物转成 JPEG（272 KB，**必须 <1MB 才能再上传**）再喂一次放大：

```
  上传：upscale_2048.jpg（JPEG，272 KB）
  建任务 → HTTP 200 {taskId}
  [+12.4s] completed → 产物 4096×3072 JPEG（1216 KB）
```

| 问题 | 实测答案 |
|---|---|
| 4K（4096）可达吗 | ✅ **可达，但要跑两次放大**：1024 → 2048 → 4096（每步固定 2×，无参数可选） |
| 中转格式 | 第二次的输入是 2048×1536 PNG = 2.7MB **超过 1MB 上限** ⇒ 必须转 JPEG（82 质量只有 272KB） |
| 输出格式 | **跟随输入**：PNG 进 → PNG 出；JPEG 进 → JPEG 出 |
| 耗时 | 7.7s / 12.4s —— 两次合计约 20s，仍远快于生成 |

### 10.4 ⚠️ 编辑器（ai-photo-editor）**极慢，但最终会完成**（实测，2026-09-21）

> **最终结果（4 小时后复查）**：`{"status":"completed","progress":100,"image":"…e3b32952-….png"}`
> ⇒ **它会完成，只是极慢**（18 分钟时仍 pending/50，4 小时内出图）。产物下载后实测
> **1184×896**（输入是 1024×768）⇒ **不保留原图尺寸**，比例也从 1.333 变成 1.321。
> 接入侧的超时预算 `TASK_TIMEOUT_I2I=3600s` 因此是合理取值（对应实测的「小时级」）。

带 token 建任务成功（taskId=`c65ccf6f-…`，**额度已消耗**），但：

- 轮询 300s+ 一直 `{"status":"pending","progress":50}`；
- **10 分钟后**零额度复查仍是 `pending/50`。

⇒ 接入编辑器时的两条硬结论：**超时预算必须远大于 300s**（生成 150s、放大 20s 都不适用）；
以及「任务长期 pending」是**真实存在的形态**，不是我们轮询写错了。

🔴 仍未取证：编辑器**最终**会不会完成（任务还在上游挂着，稍后可零额度复查）、
产物是否保留原图尺寸、它与生成/放大的配额**是否共用**同一个 IP 桶（推测共用）。
⇒ 本服务**仍未接入**工具类能力；技术路径已全部打通（铸 token + 上传 + 三步流 + 链式放大）。

⚠️ 将来若接入：本服务要新增"**代取调用方图片 → 再上传到站点存储**"的一段
（调用方给的外链不能直接交给上游），该部分按 `skills/user-controlled-egress-guard`
的纪律做（体积上限、超时、拒绝内网目标）。

---

### 10.5 ✅ 图生图（image-i2i）端到端实测（经本服务，2026-09-21）

本服务于当日下午接入 `/api/ai-photo-editor`（`model="image-i2i"`），随后做了**真实端到端验证**
（消耗 1 次额度）：

```
17:01:53  服务启动（IMAGEFREE_TURNSTILE_TOKEN = 现铸的 816 字符 token）
17:01:59  已受理：imagefree_55b2…（model=image-i2i，参考图 = 一张公开的 R2 图片 URL）
17:02:06  已提交上游 → dc003ca0-b560-4d3b-8608-2755826007ce（出口 direct）
```

⇒ 受理到提交上游 **约 7 秒**；这 7 秒里完成了：**代取参考图（≈1MB）→ 转存到上游存储
（`upload-url` + `PUT`）→ 带 Turnstile token 建任务**。三点都得到了真实网络的验证：

1. **Turnstile token 有效**：没有再出现 `400 Human verification failed`（§10.1 那个坑）；
2. **代取 + 转存可行**：上游只吃自己存储里的 URL ⇒ 本服务先下载外链再转存，链路成立；
3. **留痕正确**：查询响应同时给出「模型说明」与「比例对图生图无效（产物跟随参考图）」两条降级说明。

⚠️ 编辑器很慢（见 §10.4，小时级）⇒ **"提交成功"与"出图"必须分开看**：前者 7 秒可验，后者要等。
