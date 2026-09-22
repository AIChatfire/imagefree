# 部署（Linux 无头）

> 面向"把这套服务跑在一台**没有显示器的 Linux 机器**上"。
> 只做**文生图**（`image-t2i`）时不需要任何浏览器，`docker compose up -d` 就够了；
> 要用**图生图**（`image-i2i`）就必须再解决 **Turnstile**（§3）—— 那是本服务在无头环境下的唯一门槛。

## 1. 分档：按你要的能力决定装什么

| 能力 | 上游 | 需要浏览器吗 | 关键依赖 |
|---|---|---|---|
| `image-t2i`（文生图） | `/api/generate` | ❌ **不需要** | 只要能出网 |
| `image-i2i`（图生图/编辑） | `/api/ai-photo-editor` | ✅ **需要**（铸 Turnstile token） | 见 §3 |
| `image-upscale`（放大 2×/4K） | `/api/image-upscaler` | ✅ 需要（同上） | **尚未接入**本服务 |

⚠️ 上游限流按 **IP** 记账：**单 IP 在途上限 3 个任务**（实测，`docs/UPSTREAM.md` §9）。
⇒ 一台机器 + 一个出口 = 最多 3 个在途；要扩容量就加出口（`IMAGEFREE_PROXIES`）。
**副本数固定 1**：闸门账本是进程内状态，多副本只会把闸门放宽 N 倍。

## 2. 上容器（推荐）

```bash
git clone <repo> && cd imagefree-service
cp .env.example .env          # 空着就能跑文生图（上游免登录）
docker compose up -d --build
curl -s localhost:8400/healthz   # {"status":"ok"}
```

镜像里已经写死 `TZ=Asia/Shanghai`（`Dockerfile` 的 `ENV TZ`）—— **见 §3，TZ 不是可选项**。

### 裸机（不用容器）

```bash
sudo useradd -r -s /usr/sbin/nologin imagefree && sudo mkdir -p /srv/imagefree && cd /srv/imagefree
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
TZ=Asia/Shanghai .venv/bin/gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

systemd 单元（`/etc/systemd/system/imagefree.service`）：

```ini
[Unit]
Description=imagefree-service
After=network-online.target

[Service]
User=imagefree
WorkingDirectory=/srv/imagefree
EnvironmentFile=/srv/imagefree/.env
Environment=TZ=Asia/Shanghai
# 🔴 目标必须是**工厂**（括号不能省）：写 app.main:app 会 Failed to find attribute
ExecStart=/srv/imagefree/.venv/bin/gunicorn -c gunicorn_conf.py "app.main:create_app()"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 3. 🔴 Turnstile（图生图的唯一门槛）

**站点的主生成端点不校验 Turnstile，但工具端点强制校验**（实测 `400 Human verification failed`，
见 `docs/UPSTREAM.md` §10.1）⇒ 图生图/放大必须带一个有效 token。

token 的三个性质决定了部署形态：

| 性质 | 后果 |
|---|---|
| **只有几分钟有效期** | ❌ 不能"启动时注入一个静态值"——重启几分钟后就全废 |
| 需要**真浏览器**（headless 可，但要过 CF 风控） | 机器上要有 Chrome/Chromium + node |
| 绑定 sitekey + 域名，**不绑账号** | 一台 mint 机可服务多个实例 |

### 3.1 推荐：让服务**按需现铸**

```bash
# 机器上准备（一次性）
sudo apt install -y chromium nodejs npm
npm i -g playwright-core            # 或装到某个目录并用 NODE_PATH 指过去

# .env
IMAGEFREE_TURNSTILE_MINT_CMD=node scripts/mint_turnstile.cjs --headless --json
IMAGEFREE_TURNSTILE_MINT_TIMEOUT=60
TZ=Asia/Shanghai                    # 🔴 见 3.2，与出口 IP 地理一致
```

服务会在**每次工具端点提交前**跑这条命令铸一个新鲜 token（实测铸一次 3~15s），
铸不出来就给出 `upstream_turnstile_required`（**503**，部署问题）—— 不会拿过期 token 去提交。

自检（零额度）：

```bash
TZ=Asia/Shanghai node scripts/mint_turnstile.cjs --headless --json
# → {"token":"1.…","seconds":5.2,"tz":"Asia/Shanghai","headless":true}
```

🔴 **无头不保证能过**：实测 macOS 上 `--headless` 下 widget 根本没挂载
（诊断里 `widgetNodes: 0`、`hasTurnstileApi: true`）⇒ 铸不出来。
所以**先跑上面的自检**，不行就上虚拟显示 + 有头模式（服务器上的标准解法）：

```bash
sudo apt install -y xvfb
IMAGEFREE_TURNSTILE_MINT_CMD=xvfb-run -a node scripts/mint_turnstile.cjs --headed --json
```

铸造脚本在失败时会把诊断（`hasTurnstileApi` / `widgetNodes` / 耗时 / TZ）打到 stderr，
服务会把它**原样带进任务错误信息**（`upstream_turnstile_required`），照它排查即可。

### 3.2 🔴 TZ 是硬要求（缺它必失败）

Cloudflare 会比对**浏览器时区**与**出口 IP 地理**；不一致就把挑战从"静默通过"升级为
**交互式**（要人点）⇒ 无头环境 100% 铸不出来。表现是：widget 在、但永远拿不到 token。

- **必须设** `TZ`，且与**出口 IP 的地理一致**（出口在国内 ⇒ `Asia/Shanghai`）。
  1 小时量级的偏差（如 `Asia/Tokyo` vs `Asia/Shanghai`）实测**可以**；
  **UTC vs 亚洲**这种量级的不一致必失败。
- Linux/容器默认是 UTC ⇒ **不设就等着踩**。本仓的铸造脚本在 Linux 上检测到
  `TZ` 缺失/为 UTC 会**直接拒绝执行**（退出码 3），而不是浪费 45 秒再给一个难懂的失败。
- 容器里跑 Chrome 还需要 `--no-sandbox`（脚本在 Linux 上自动加）与足够的 `/dev/shm`。

### 3.3 备选：常驻 minter 服务 / 手工 token

- 有多实例需求时，把铸 token 做成**一个常驻小服务**（一台机器服务所有实例，token 绑 sitekey+域名），
  服务侧用 `IMAGEFREE_TURNSTILE_TOKEN` 注入 —— 但注意**静态值会过期**，
  该形态必须配合"定期刷新 + 进程重载"，否则回到 §3.1 按需铸造更省事。
- **不把 Chrome 塞进应用镜像**的形态：铸造命令可以只是一次 HTTP 调用 ——
  另起一个专门装 Chrome 的容器/进程（sidecar），应用侧写：

  ```bash
  IMAGEFREE_TURNSTILE_MINT_CMD=curl -s --max-time 60 http://minter:8899/mint
  ```

  只要那条命令的 stdout 是裸 token 或 `{"token": "…"}`，本服务就认（见 `app/turnstile.py`）。
  这样应用镜像保持精简，浏览器只在需要它的那台机器上。
- 只是临时验证：从浏览器里手工取一个 token 填 `IMAGEFREE_TURNSTILE_TOKEN`，
  **填完立刻用**（几分钟内）。

## 4. 出口与代理由

```bash
IMAGEFREE_PROXIES=socks5h://user:pass@pool.example.com:2088
IMAGEFREE_USE_DIRECT=1        # 直连优先，名额满了才溢出到池子
IMAGEFREE_IP_CONCURRENCY=3    # 单出口在途名额（上游实测上限）
```

- 出口写错 ⇒ **启动失败**（不静默）。`IMAGEFREE_PROXIES` 留空 = 直连。
- 有效容量 = `min(IF_CONCURRENCY, 出口数 × 3)`，启动日志会写明。
- 轮换型池（每连接一个 IP）撞墙后值得**原地重试**（`IMAGEFREE_PROXY_RETRIES=3`）。
- 自检（零额度）：`python scripts/probe.py egress --echo-ip --repeat 3`。

## 5. 验收清单（都在零额度内完成）

```bash
curl -s localhost:8400/healthz      # 存活：零依赖、不触上游
curl -s localhost:8400/readyz       # 就绪：库 + 出口 + 协调器 + token 是否配好
curl -s localhost:8400/stats        # 在途数 / 容量 / 各出口状态
curl -s localhost:8400/capabilities # 能力与"刻意缺席"清单
python scripts/probe.py egress --echo-ip --repeat 3     # 出口可达性 + 真实出口 IP
TZ=Asia/Shanghai node scripts/mint_turnstile.cjs --headless --json   # token 铸造自检
```

🔴 **别用真实提交做冒烟**：提交会消耗上游免费额度。
真要验证出图，用 `scripts/probe.py generate --i-know-this-consumes-quota`（显式开闸）。

## 6. 常见故障速查

| 症状 | 根因 | 处置 |
|---|---|---|
| `upstream_turnstile_required`（503） | 没配 token 来源 / token 过期 | 配 `IMAGEFREE_TURNSTILE_MINT_CMD`（§3.1） |
| 铸造脚本退出码 3 | **TZ 缺失/为 UTC** | `TZ=Asia/Shanghai`（§3.2） |
| 铸造 45s 超时、`TOKEN_EMPTY` 且 `hasTurnstileApi=true` | 挑战变**交互式**（时区或出口信誉） | 先查 TZ；再考虑换出口（机房 IP 分低） |
| `upstream_egress_unavailable`（502） | 某个出口连不上（代理认证失败等） | 看 `/stats` 的 `egress[].cooling`；修代理或去掉该出口 |
| 任务一直 `in_progress` | 上游慢：文生图 ≈150s，**编辑器小时级** | 正常。`TASK_TIMEOUT_I2I=3600` 就是为它留的 |
| 任务停在 `queued`、日志有「提交前置步骤失败（可重试）」 | 铸 token 瞬时失败 | 会自动退避重试 **3 次**（`MINT_RETRY_ATTEMPTS`）；连续失败才终态 ⇒ 若一直重试不过，按上两行排查 |
| 受理后永不提交 | `COORDINATOR_ENABLED=0` | 那是冒烟档，生产必须为 1 |
| 端口暴露到公网 | ⚠️ 端口默认只绑 `127.0.0.1` | 改绑前想清楚：这是在花**本机出口 IP** 的免费额度 |
