"""Pure digest notifications with complete line-based UTF-16 pagination."""

# ruff: noqa: RUF001

from datetime import datetime
from zoneinfo import ZoneInfo

from quant_system.decision.digest import DailyDigest
from quant_system.decision.digest_reports import render_digest_summary


def utf16_units(text):
    return len(text.encode("utf-16-le")) // 2


def paginate_lines(lines, *, title, max_units=3800):
    if not 256 <= max_units <= 4096:
        raise ValueError("Telegram page budget must be 256-4096 UTF-16 units")
    # Reserve the exact header width after the page count converges.
    expected = 1
    while True:
        groups, current = [], []
        for line in lines:
            header = f"{title}｜{len(groups) + 1}/{expected}\n"
            if utf16_units(header + line) > max_units:
                raise ValueError("A complete notification row exceeds the page budget")
            if current and utf16_units(header + "\n".join([*current, line])) > max_units:
                groups.append(current)
                current = []
            current.append(line)
        if current:
            groups.append(current)
        if len(groups) == expected:
            return [
                f"{title}｜{i}/{expected}\n" + "\n".join(group) for i, group in enumerate(groups, 1)
            ]
        expected = len(groups)


def render_digest_notifications(digest, *, label="美股日报", max_units=3800):
    value = DailyDigest.model_validate(digest) if isinstance(digest, dict) else digest
    meta = value.metadata
    run = str(meta.get("run_id", "unknown"))[:8]
    title = f"{label} · {meta['signal_session']} · {run}"
    summary = render_digest_summary(value).splitlines()
    stamp = meta.get("evaluated_at_utc", meta.get("generated_at_utc"))
    if stamp:
        observed = datetime.fromisoformat(stamp).astimezone(ZoneInfo("America/New_York"))
        summary.insert(1, f"评估：{observed:%m-%d %H:%M} 纽约｜状态：{value.status}")
    gate = value.data_health.get("latest_gate", {})
    if gate:
        summary += ["", "本次检查明细（入场资格仍以筛选状态为准）"]
        scores = {s.resolved_symbol: s.technical_score.value for s in value.stocks}
        names = {"KEEP": "检查通过", "DEFER": "待确认", "CANCEL": "取消", "REDUCE": "减仓检查"}
        for row in gate.get("rows", []):
            score = scores.get(row["symbol"])
            points = f"{score:g}/100" if score is not None else "暂不可用"
            summary.append(
                f"{row['symbol']} {points}｜{names.get(row['decision'], row['decision'])}"
                f"｜{row.get('reason', '')}"
            )
    return paginate_lines(summary, title=title, max_units=max_units)


def notification_pages(result, session, *, attempt=1, label="美股日报", max_units=3800):
    if result.get("digest") and result.get("status") not in {"BLOCKED", "FAILED"}:
        digest = DailyDigest.model_validate(result["digest"])
        if digest.schema_version == "2" and digest.metadata.get("signal_session") == session:
            return render_digest_notifications(digest, label=label, max_units=max_units)
    # Legacy/failed runs cannot manufacture market values or entry permission from counts.
    lines = [
        f"状态：{result.get('status', 'FAILED')}",
        "数据异常，暂停入场建议；本次没有可用的推荐报告。",
        f"第 {attempt} 次运行；" + str(result.get("error_type", "digest_unavailable")),
    ]
    lines += [str(reason) for reason in result.get("blocked_reasons", [])]
    return paginate_lines(lines, title=f"{label} · {session}", max_units=max_units)


def redact_pages(pages, settings):
    secrets = (
        settings.telegram_bot_token.get_secret_value(),
        settings.alpha_vantage_api_key,
        settings.sec_user_agent,
        settings.alpaca_api_key.get_secret_value() if settings.alpaca_api_key else None,
        settings.alpaca_secret_key.get_secret_value() if settings.alpaca_secret_key else None,
    )
    return [redact_text(page, secrets) for page in pages]


def redact_text(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
