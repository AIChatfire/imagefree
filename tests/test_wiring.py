#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接线门禁：**配置、文档、容器、启动目标**之间不许漂。

这些用例的价值不在于"跑通"，而在于**挡住了三类静默故障**：

  1. 加了配置项却没有任何代码读它（等于留了个骗人的旋钮）；
  2. `.env.example` 少写一项（部署时以为配了就生效）；
  3. `gunicorn`/`Dockerfile` 指向了不存在的目标（单测全绿，容器起不来）。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"

#: 这些旋钮**只在 gunicorn_conf.py 里**被读（它是绑定地址的唯一真相）。
#: 把它们排除在 app/ 扫描之外，但要求下一个用例单独断言。
_GUNICORN_ONLY = {"host", "port"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _app_sources() -> dict[str, str]:
    """app/ 下所有源码（**排除 config.py** —— 声明处不算读取处）。"""
    return {
        str(p.relative_to(ROOT)): _read(p)
        for p in sorted(APP_DIR.rglob("*.py"))
        if p.name != "config.py"
    }


def test_every_knob_is_read_somewhere() -> None:
    """🔴 拿不出依据的旋钮不存在：每个配置项都必须有代码真的读它。"""
    sources = _app_sources()
    gunicorn = _read(ROOT / "gunicorn_conf.py")
    missing: list[str] = []
    for name in Settings.model_fields:
        if name in _GUNICORN_ONLY:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if not any(pattern.search(text) for text in sources.values()):
            missing.append(name)
    assert not missing, (
        f"这些配置项没有任何代码读取（要么删掉，要么接上）：{missing}"
    )
    assert "HOST" in gunicorn and "PORT" in gunicorn, (
        "host/port 的唯一读取点是 gunicorn_conf.py 的 bind"
    )


def test_env_example_documents_every_knob() -> None:
    """.env.example 少一项 ⇒ 部署时会以为"配了但没生效"。"""
    example = _read(ROOT / ".env.example")
    missing = [name.upper() for name in Settings.model_fields if name.upper() not in example]
    assert not missing, f".env.example 缺少这些键：{missing}"


def test_env_example_has_no_unknown_knobs() -> None:
    """反向漂移：模板里写了实现里不存在的键，会让人配了个寂寞。"""
    example = _read(ROOT / ".env.example")
    declared = {name.upper() for name in Settings.model_fields}
    found = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if re.match(r"^[A-Z][A-Z0-9_]*=", line.strip())
    }
    assert found <= declared, f".env.example 里有多余的键：{sorted(found - declared)}"


def test_settings_expose_no_credential_values() -> None:
    """`/capabilities` 的配置快照**只准出布尔与数字**，不许把凭据值带出去。"""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_keys="super-secret",
        imagefree_turnstile_token="tok-secret",
        imagefree_free_generation_id="cookie-secret",
    )
    snapshot = settings.documented_defaults()
    blob = repr(snapshot)
    assert "super-secret" not in blob
    assert "tok-secret" not in blob
    assert "cookie-secret" not in blob
    assert snapshot["auth_enabled"] is True
    assert snapshot["turnstile_token_configured"] is True


def test_gunicorn_target_is_a_factory() -> None:
    """`app.main:app` 是不存在的属性 —— 单测全绿，容器也起不来。"""
    import app.main as main_module

    assert hasattr(main_module, "create_app")
    assert not hasattr(main_module, "app"), (
        "模块级 app 对象存在会让人写出 gunicorn app.main:app（那个目标其实无效）"
    )


def test_dockerfile_cmd_target_resolves() -> None:
    dockerfile = _read(ROOT / "Dockerfile")
    match = re.search(r'CMD \[.*?"(app\.main:[^"]+)"', dockerfile)
    assert match, "Dockerfile 里没有找到 gunicorn 的 CMD 目标"
    target = match.group(1)
    assert target == "app.main:create_app()", f"CMD 目标不对：{target}"

    module_name, _, attr = target.partition(":")
    module = __import__(module_name, fromlist=["_"])
    factory = getattr(module, attr.rstrip("()"), None)
    assert callable(factory), f"{target} 解析不到可调用对象"
    # 用内存库真建一次（**不启动协调器**，因此零上游动作）
    built = factory(
        settings=Settings(
            _env_file=None,  # type: ignore[call-arg]
            task_db="sqlite+pysqlite:///:memory:",
            coordinator_enabled=0,
        ),
        start_coordinator=False,
    )
    assert built.title == "imagefree-service"


def test_gunicorn_conf_defaults_to_one_worker() -> None:
    """副本数默认 1 是**架构约束**（上游在途互斥），不是可随手调的参数。"""
    import os

    import gunicorn_conf

    assert os.environ.get("WORKERS") is None, "本用例必须在未设置 WORKERS 的环境下跑"
    assert gunicorn_conf.workers == 1
    assert "8400" in gunicorn_conf.bind, "默认端口必须是 8400（与 Settings.port 一致）"


def test_worker_class_is_the_asgi_one() -> None:
    import gunicorn_conf

    assert gunicorn_conf.worker_class == "uvicorn.workers.UvicornWorker"


def test_compose_binds_only_loopback() -> None:
    """上游按 IP 记账 ⇒ 端口不许暴露到公网。"""
    compose = _read(ROOT / "docker-compose.yml")
    assert "127.0.0.1:${APP_PORT:-8400}:8400" in compose, (
        "docker-compose 的应用端口必须只绑回环（上游按 IP 记账）"
    )
    assert "ports:" in compose


def test_pytest_basetemp_is_pinned() -> None:
    """沙箱的 mkdir shim 会把默认临时目录搞坏 ⇒ 必须显式钉住。"""
    ini = _read(ROOT / "pytest.ini")
    assert "--basetemp=" in ini


def test_contract_docs_exist_and_are_cross_referenced() -> None:
    interface = _read(ROOT / "docs" / "INTERFACE.md")
    upstream = _read(ROOT / "docs" / "UPSTREAM.md")
    assert "docs/UPSTREAM.md" in interface
    assert "docs/INTERFACE.md" in upstream
    assert "冻结" in interface


def test_docs_do_not_claim_unknown_facts_as_known() -> None:
    """上游取证文档必须留一份"未取证"清单 —— 否则后人会把猜测当事实。"""
    upstream = _read(ROOT / "docs" / "UPSTREAM.md")
    assert "未取证" in upstream
    assert "0x4AAAAAACE-XLGoQUckKKm_" in upstream, "Turnstile sitekey 必须留档（备用）"
    for endpoint in ("/api/generate", "/api/generate/status"):
        assert endpoint in upstream


def test_deploy_doc_covers_the_headless_traps() -> None:
    """Linux 无头部署文档必须写到那几个「踩过才知道」的点。

    漏掉任一条，运维就会在无头机器上花几十分钟找一个其实写在文档里的原因
    （尤其 TZ：缺它 ⇒ 挑战变交互式 ⇒ 无头必失败）。
    """
    deploy = _read(ROOT / "docs" / "DEPLOY.md")
    for must in (
        "TZ",                       # 头号坑：时区与出口 IP 不一致 ⇒ 交互式挑战
        "--headless",               # 无头形态
        "--no-sandbox",             # 容器里跑 Chrome
        "IMAGEFREE_TURNSTILE_MINT_CMD",  # 按需铸造（静态 token 几分钟就过期）
        "upstream_turnstile_required",
        "min(IF_CONCURRENCY, 出口数 × 3)",
        "127.0.0.1",                # 端口只绑回环
        "create_app()",             # gunicorn 目标必须带括号
    ):
        assert must in deploy, f"docs/DEPLOY.md 缺少 {must!r}"
    assert "零额度" in deploy, "验收清单要说明哪些动作不消耗额度"


def test_readme_documents_the_smoke_trap() -> None:
    """冒烟必须关协调器，否则它会代替你向上游提交任务。"""
    readme = _read(ROOT / "README.md")
    assert "COORDINATOR_ENABLED=0" in readme
