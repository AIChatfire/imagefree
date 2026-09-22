#!/usr/bin/env node
// Turnstile token 铸造（现铸现用）：打开站点工具页，等 widget 自己解完，取出 token。
//
// 为什么需要它：站点的**工具端点**（/api/ai-photo-editor、/api/image-upscaler …）强制 Turnstile，
// 而主生成端点没有 ⇒ 只做文生图**不需要**本脚本；要图生图/放大就必须能拿到 token。
//
// 用法：
//   node scripts/mint_turnstile.cjs                        # 本机真 Chrome（有头）
//   node scripts/mint_turnstile.cjs --headless             # 无头（Linux 服务器默认无头）
//   node scripts/mint_turnstile.cjs --json                 # 输出 JSON（含耗时/TZ/诊断），便于脚本消费
//   CHROME_PATH=/usr/bin/chromium node scripts/mint_turnstile.cjs
//
// 🔴 三个必须知道的坑（都实测踩过，部署见 docs/DEPLOY.md）：
//   1. **TZ 必须设对**（与出口 IP 地理一致，如 Asia/Shanghai）。缺 TZ ⇒ Chrome 跑 UTC
//      ⇒ CF 判「时区与 IP 地理不一致」⇒ 挑战升级为**交互式**（要人点），无头环境必失败。
//   2. **token 只有几分钟有效期** ⇒ 现铸现用，别缓存、别提前批量铸。
//   3. 容器里跑 Chrome 要 `--no-sandbox`（Linux 上本脚本自动加），并留足 /dev/shm。
//
// 依赖：playwright-core（`npm i playwright-core`）+ 一个真 Chrome/Chromium 二进制。
// 不需要 `playwright install`（借本机浏览器，不下 playwright 自带的 Chromium）。
const { chromium } = require("playwright-core");
const os = require("os");
const fs = require("fs");

const DEFAULT_URL = "https://imagefree.net/zh/image-upscaler";

function parseArgs(argv) {
  const opts = {
    url: DEFAULT_URL,
    headless: os.platform() === "linux", // 服务器上没显示器 ⇒ 默认无头
    json: false,
    timeout: 45,
    executablePath: process.env.CHROME_PATH || "",
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--headless") opts.headless = true;
    else if (a === "--headed") opts.headless = false;
    else if (a === "--json") opts.json = true;
    else if (a.startsWith("--timeout=")) opts.timeout = parseInt(a.slice(10), 10);
    else if (a.startsWith("--executable-path=")) opts.executablePath = a.slice(18);
    else if (!a.startsWith("--")) opts.url = a;
  }
  return opts;
}

// 找浏览器：显式指定 > 常见安装路径（macOS 用真 Chrome；Linux 常见是 chromium / google-chrome）。
function resolveChrome(explicit) {
  if (explicit) {
    if (!fs.existsSync(explicit)) throw new Error(`指定的浏览器不存在：${explicit}`);
    return explicit;
  }
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
  ];
  for (const path of candidates) if (fs.existsSync(path)) return path;
  throw new Error(
    "找不到 Chrome/Chromium。装一个（apt install chromium / google-chrome），或用 CHROME_PATH= 指定。"
  );
}

// 🔴 缺 TZ 是铸造失败的头号原因（技能档已定案）：CF 比对浏览器时区与出口 IP 地理，
// 不一致就把挑战升级为**交互式**（要人点）⇒ 无头环境必失败。
// Linux/容器上**直接拒绝执行**（fail fast），别浪费 45 秒再给个难懂的失败；
// macOS 用系统时区（Chrome 取系统 TZ），所以只提醒不拦。
function checkTimezone(opts) {
  const tz = process.env.TZ || "";
  const bad = !tz || /^(UTC|Etc\/UTC)$/i.test(tz);
  if (!bad) return tz;
  const detail =
    "   TZ 未设或为 UTC：CF 会判「浏览器时区与出口 IP 地理不一致」并把 Turnstile 挑战升级为" +
    "交互式（无头环境**必失败**，表现是 widget 在但永不出 token）。\n" +
    "   修法：设成与**出口 IP 地理一致**的值，例如 export TZ=Asia/Shanghai（容器里写进 compose 的 environment）。\n";
  if (os.platform() === "linux") {
    process.stderr.write("ERR 拒绝执行（Linux 上 TZ 是硬要求）：\n" + detail);
    process.exit(3);
  }
  process.stderr.write("⚠️ " + detail);
  return "(未设)";
}

(async () => {
  const opts = parseArgs(process.argv);
  const tz = checkTimezone(opts);
  const executablePath = resolveChrome(opts.executablePath);

  const args = ["--disable-blink-features=AutomationControlled"];
  if (os.platform() === "linux") {
    // 容器里没有可用沙箱/共享内存：不加这两条 Chrome 常常直接起不来，且日志很难懂。
    args.push("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu");
  }

  const started = Date.now();
  const browser = await chromium.launch({ executablePath, headless: opts.headless, args });
  const page = await browser.newPage();

  let lastErr = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      // 用 `commit` 而不是 `domcontentloaded`：这个站点偶发 ERR_ABORTED，等 DOM 会误判成导航失败。
      await page.goto(opts.url, { waitUntil: "commit", timeout: 60000 });
      lastErr = null;
      break;
    } catch (e) {
      lastErr = e;
      await page.waitForTimeout(2000);
    }
  }
  if (lastErr) {
    process.stderr.write(`ERR 打开页面失败：${(lastErr && lastErr.message) || lastErr}\n`);
    await browser.close();
    process.exit(1);
  }

  let token = "";
  const budget = opts.timeout * 1000;
  while (Date.now() - started < budget) {
    await page.waitForTimeout(1000);
    try {
      token = await page.evaluate(() => {
        if (!window.turnstile || !window.turnstile.getResponse) return "";
        const r = window.turnstile.getResponse();
        return typeof r === "string" ? r : "";
      });
    } catch (e) {
      /* 页面还在加载/导航，忽略 */
    }
    if (token) break;
  }

  const elapsed = ((Date.now() - started) / 1000).toFixed(1);
  if (!token) {
    let diag = {};
    try {
      diag = await page.evaluate(() => ({
        hasTurnstileApi: !!window.turnstile,
        widgetNodes: document.querySelectorAll(
          ".cf-turnstile, iframe[src*=turnstile], iframe[title*=Widget]"
        ).length,
      }));
    } catch (e) {
      diag = { evalFailed: String(e).slice(0, 120) };
    }
    await browser.close();
    process.stderr.write(
      `TOKEN_EMPTY 耗时 ${elapsed}s headless=${opts.headless} tz=${tz} 诊断=${JSON.stringify(diag)}\n` +
        "   排查顺序：① TZ 是否与出口 IP 地理一致（见上）；② 无头被识别 ⇒ 试 --headed 或换真 Chrome；" +
        "③ 挑战是否变成交互式（widget 在但不出 token）。详见 docs/DEPLOY.md。\n"
    );
    process.exit(2);
  }

  await browser.close();
  if (opts.json) {
    process.stdout.write(
      JSON.stringify({ token, seconds: Number(elapsed), url: opts.url, tz, headless: opts.headless }) + "\n"
    );
  } else {
    process.stdout.write(token + "\n");
  }
})().catch((e) => {
  process.stderr.write(`ERR ${(e && e.message) || e}\n`);
  process.exit(1);
});
