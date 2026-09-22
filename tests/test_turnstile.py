#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turnstile token 供给链的**离线**验证（零出网、零额度）。

为什么值得单独测：这条链是"无头 Linux 部署能不能用图生图"的**唯一**开关，
而它的失败形态全是**部署问题**（503）—— 必须与"调用方参数写错"（4xx）严格分开，
否则运维会去改请求体，越改越远。

全程用 `sys.executable -c` 造假的铸造命令，不碰网络、不碰浏览器。
"""
from __future__ import annotations

import sys
from typing import Any

import pytest
from loguru import logger

from app.config import Settings
from app.errors import TurnstileMintFailed, TurnstileRequired
from app.turnstile import mint_token, parse_mint_output, token_for_tool_submit

#: 假 token（故意含一个可搜索的独特片段，用来验证"绝不进日志"）
FAKE_TOKEN = "1.super-secret-token-do-not-log-me.payload.sig"


def _cmd(tmp_path: Any, body: str) -> str:
    """造一条假铸造命令：把 body 写成脚本文件再用当前解释器执行。

    🔴 刻意**不用** `python -c "…"`：body 里难免有引号，嵌进 shell 字符串后
    会被 shlex 拆错（实测踩过：报 SyntaxError，看着像被测代码坏了）。
    """
    script = tmp_path / "fake_mint.py"
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------


def test_parse_json_output() -> None:
    assert parse_mint_output('{"token": "1.abc", "seconds": 3}\n') == "1.abc"


def test_parse_raw_line() -> None:
    assert parse_mint_output("1.abc.def\n") == "1.abc.def"


def test_parse_takes_the_last_line_only() -> None:
    """铸造脚本可能带诊断输出 ⇒ 只认最后一行，别把警告当成 token。"""
    stdout = '⚠️ TZ 未设\n{"token": "1.real"}\n'
    assert parse_mint_output(stdout) == "1.real"


@pytest.mark.parametrize("garbage", ["", "   \n", "{不是 JSON}", '{"no_token": 1}'])
def test_parse_rejects_garbage(garbage: str) -> None:
    assert parse_mint_output(garbage) is None


# ---------------------------------------------------------------------------
# 取 token：三种来源与它们的优先级
# ---------------------------------------------------------------------------


def test_without_any_source_it_is_a_deployment_problem(make_settings: Any) -> None:
    settings = make_settings()
    assert token_for_tool_submit(settings) is None, "什么都没配 ⇒ 交给调用方判 503"
    with pytest.raises(TurnstileRequired) as excinfo:
        mint_token(settings)
    assert "IMAGEFREE_TURNSTILE_MINT_CMD" in str(excinfo.value)
    assert excinfo.value.status_code == 503


def test_mint_command_wins_over_static_token(make_settings: Any, tmp_path: Any) -> None:
    settings = make_settings(
        imagefree_turnstile_token="static-expired-value",
        imagefree_turnstile_mint_cmd=_cmd(tmp_path, f'print(\'{{"token": "{FAKE_TOKEN}"}}\')'),
    )
    assert token_for_tool_submit(settings) == FAKE_TOKEN, "配了铸造命令就该现铸（静态值几分钟就过期）"


def test_static_token_is_the_fallback_when_no_mint_command(make_settings: Any) -> None:
    settings = make_settings(imagefree_turnstile_token="static-value")
    assert token_for_tool_submit(settings) == "static-value"


def test_mint_raw_stdout_form(make_settings: Any, tmp_path: Any) -> None:
    settings = make_settings(imagefree_turnstile_mint_cmd=_cmd(tmp_path, f'print("{FAKE_TOKEN}")'))
    assert mint_token(settings) == FAKE_TOKEN


# ---------------------------------------------------------------------------
# 失败形态：一律 503 + 可行动的原因
# ---------------------------------------------------------------------------


def test_missing_program_is_a_deployment_problem(make_settings: Any) -> None:
    settings = make_settings(imagefree_turnstile_mint_cmd="/nonexistent/mint_turnstile.cjs --headless")
    with pytest.raises(TurnstileRequired) as excinfo:
        mint_token(settings)
    message = str(excinfo.value)
    assert "不可执行" in message and "DEPLOY" in message, "要指到部署文档，别让人猜"


def test_nonzero_exit_reports_the_script_tail(make_settings: Any, tmp_path: Any) -> None:
    settings = make_settings(
        imagefree_turnstile_mint_cmd=_cmd(
            tmp_path, "import sys; sys.stderr.write('TZ 缺失'); sys.exit(3)"
        )
    )
    with pytest.raises(TurnstileRequired) as excinfo:
        mint_token(settings)
    assert "退出码 3" in str(excinfo.value)
    assert "TZ 缺失" in str(excinfo.value), "把铸造脚本的说明带出来，排障才不用猜"


def test_timeout_mentions_the_tz_root_cause(make_settings: Any, tmp_path: Any) -> None:
    """无头环境铸不出来时，头号原因是缺 TZ（挑战变交互式）—— 报错要直接点出来。"""
    settings = make_settings(
        imagefree_turnstile_mint_cmd=_cmd(tmp_path, "import time; time.sleep(5)"),
        imagefree_turnstile_mint_timeout=0.5,
    )
    with pytest.raises(TurnstileRequired) as excinfo:
        mint_token(settings)
    assert "TZ" in str(excinfo.value) and "DEPLOY" in str(excinfo.value)


def test_empty_output_is_a_failure_not_an_empty_token(make_settings: Any, tmp_path: Any) -> None:
    """退出码 0 但没 token（例如挑战变交互式）⇒ 宁可 503，也不能拿空串去提交。"""
    settings = make_settings(imagefree_turnstile_mint_cmd=_cmd(tmp_path, "pass"))
    with pytest.raises(TurnstileRequired):
        mint_token(settings)


# ---------------------------------------------------------------------------
# 泄漏门禁：token 绝不进日志
# ---------------------------------------------------------------------------


def test_mint_failure_is_retryable_but_missing_source_is_not(make_settings: Any, tmp_path: Any) -> None:
    """两类失败的**性质不同**，都要在类型上区分开：

      · 运行期铸失败 ⇒ `retryable=True`（CF 风险分抖动/浏览器慢是常事）⇒ 协调器退避重试几次；
      · 根本没配来源 ⇒ 永久配置错误 ⇒ 直接终态，重试不会变好。

    分不开的后果：一次铸 token 抖动就把调用方的任务判死（实测踩过）。
    """
    expired = make_settings(imagefree_turnstile_mint_cmd=_cmd(tmp_path, "import sys; sys.exit(3)"))
    with pytest.raises(TurnstileMintFailed) as failed:
        mint_token(expired)
    assert failed.value.retryable is True
    assert failed.value.code == "upstream_turnstile_required", "对外语义不变（仍是 503）"
    assert failed.value.status_code == 503

    with pytest.raises(TurnstileRequired) as missing:
        mint_token(make_settings())
    assert missing.value.retryable is False, "没配来源 ⇒ 重试也没用"


def test_every_error_is_non_retryable_by_default() -> None:
    """默认不重试：只有明确标注 `retryable=True` 的错误才会被协调器重试。"""
    from app.errors import (
        AdapterError,
        ContentPolicyError,
        EgressUnavailable,
        InvalidParameterError,
        TurnstileRequired,
        UpstreamRateLimited,
    )

    for cls in (AdapterError, InvalidParameterError, ContentPolicyError, EgressUnavailable, UpstreamRateLimited):
        assert cls.retryable is False, f"{cls.__name__} 不该被默认重试"
    assert TurnstileRequired.retryable is False
    assert TurnstileMintFailed.retryable is True


def test_token_is_never_logged(make_settings: Any, tmp_path: Any) -> None:
    """🔴 日志会被收集/转发 ⇒ 只许报长度与耗时，不许出现 token 本身。"""
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:
        settings = make_settings(imagefree_turnstile_mint_cmd=_cmd(tmp_path, f'print("{FAKE_TOKEN}")'))
        assert mint_token(settings) == FAKE_TOKEN
    finally:
        logger.remove(sink_id)
    assert captured, "至少应该有一条铸造成功的日志"
    assert not any(FAKE_TOKEN in line for line in captured), f"token 泄漏进日志了：{captured}"
    assert any("字符" in line for line in captured), "应该报 token 长度（可观测但不泄漏）"


def test_settings_are_documented_and_read() -> None:
    """接线门禁的补充：这两个旋钮必须有人读、且写进模板（与 test_wiring 的口径一致）。"""
    fields = {"imagefree_turnstile_mint_cmd", "imagefree_turnstile_mint_timeout"}
    assert fields <= set(Settings.model_fields)
