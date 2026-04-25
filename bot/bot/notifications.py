"""Email notifications via Stalwart SMTP submission.

Configuration (env vars in ./infra/.env):
  SMTP_HOST              e.g. mail.example.com — /etc/hosts maps this
                         hostname to the Tailscale IP (10.0.0.20)
                         because Stalwart binds its submission listener
                         only to the Tailscale interface, not the public
                         IP. The hostname is kept in env so the TLS cert
                         validates correctly.
  SMTP_PORT              587 (STARTTLS)
  SMTP_USER              Stalwart principal name
  SMTP_PASSWORD          Stalwart secret
  SMTP_FROM              From: header (e.g. lca@mail.example.com)
  SMTP_TO                Recipient (e.g. you@example.com)
  NOTIFY_ENABLED         "true" to enable

If any field is missing, every send silently no-ops so the bot never
blocks on a downstream failure.

Rate limit:
  Per `event_class` minimum cooldown enforced in-memory across calls.
  Critical events (auto_kill, circuit_breaker, error) bypass cooldown.
  Trade fills bypass cooldown — every fill is a discrete event worth
  one mail.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any

log = logging.getLogger("bot.notifications")

_HOST = (os.getenv("SMTP_HOST") or "").strip()
_PORT = int(os.getenv("SMTP_PORT") or "587")
_USER = (os.getenv("SMTP_USER") or "").strip()
_PASSWORD = (os.getenv("SMTP_PASSWORD") or "").strip()
_FROM = (os.getenv("SMTP_FROM") or "").strip()
_TO = (os.getenv("SMTP_TO") or "").strip()
_ENABLED = os.getenv("NOTIFY_ENABLED", "").lower() in {"true", "1", "yes"}

_DEFAULT_COOLDOWN_SEC = {
    "trade_fill": 0,
    "circuit_breaker": 0,
    "auto_kill": 0,
    "error": 0,
    "daily_summary": 0,
    "tuning_applied": 60,
    "critical_finding": 0,
    "news_watch_high": 300,
}

_LAST_SENT: dict[str, float] = {}


def _j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields}, default=str)


def _is_configured() -> bool:
    return _ENABLED and all([_HOST, _USER, _PASSWORD, _FROM, _TO])


def _cooldown_for(event_class: str) -> int:
    override = os.getenv(f"NOTIFY_COOLDOWN_{event_class.upper()}_SEC")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return _DEFAULT_COOLDOWN_SEC.get(event_class, 60)


def _within_cooldown(event_class: str) -> bool:
    cd = _cooldown_for(event_class)
    if cd <= 0:
        return False
    last = _LAST_SENT.get(event_class)
    if last is None:
        return False
    return (time.monotonic() - last) < cd


def _mark_sent(event_class: str) -> None:
    _LAST_SENT[event_class] = time.monotonic()


async def _send(subject: str, body_plain: str, body_html: str | None,
                event_class: str) -> None:
    if not _is_configured():
        return
    if _within_cooldown(event_class):
        return
    msg = EmailMessage()
    msg["Subject"] = f"[trader] {subject}"
    msg["From"] = _FROM
    msg["To"] = _TO
    msg.set_content(body_plain)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(_HOST, _PORT, timeout=15) as s:
            s.starttls(context=ctx)
            s.login(_USER, _PASSWORD)
            s.send_message(msg)
        _mark_sent(event_class)
    except Exception as exc:
        log.warning(_j("smtp_notify_failed", event_class=event_class,
                        err=str(exc)))


def _fmt_pnl_html(pnl: float) -> str:
    color = "#2ecc71" if pnl > 0 else "#e74c3c"
    sign = "+" if pnl > 0 else ""
    return f"<span style='color:{color}'>{sign}{pnl:.2f} EUR</span>"


async def notify_trade_fill(
    *,
    symbol: str,
    side: str,
    qty: float,
    fill_price: float,
    pnl: float | None = None,
    slot: int | None = None,
    reason: str | None = None,
    paper: bool = True,
) -> None:
    mode = "PAPER" if paper else "LIVE"
    subject = f"{mode} {side} {qty} {symbol} @ {fill_price:.4f}"
    if pnl is not None:
        subject += f" P&L {pnl:+.2f}"

    plain = (
        f"[{mode}] {side} {qty} {symbol} @ {fill_price:.4f}\n"
        + (f"P&L: {pnl:+.2f} EUR\n" if pnl is not None else "")
        + (f"Slot: {slot}\n" if slot is not None else "")
        + (f"Reason: {reason}\n" if reason else "")
    )
    html = (
        f"<p><b>[{mode}] {side}</b> <code>{symbol}</code></p>"
        f"<p>Qty: <b>{qty}</b> &middot; Fill: <b>{fill_price:.4f}</b></p>"
        + (f"<p>P&amp;L: {_fmt_pnl_html(pnl)}</p>" if pnl is not None else "")
        + (f"<p>Slot: <b>{slot}</b></p>" if slot is not None else "")
        + (f"<p>Reason: <i>{reason}</i></p>" if reason else "")
    )
    await _send(subject, plain, html, event_class="trade_fill")


async def notify_error(
    *,
    title: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    plain = f"[ERROR] {title}: {message}\n"
    if details:
        plain += json.dumps(details, default=str, indent=2)
    html = (
        f"<p><b style='color:#e74c3c'>[ERROR]</b> {title}</p>"
        f"<p>{message}</p>"
    )
    if details:
        html += f"<pre>{json.dumps(details, default=str, indent=2)}</pre>"
    await _send(f"ERROR: {title}", plain, html, event_class="error")


async def notify_auto_kill(reason: str, equity: float | None = None) -> None:
    plain = f"[AUTO-KILL] {reason}\n"
    if equity is not None:
        plain += f"Equity: {equity:,.2f} EUR\n"
    html = (
        f"<p><b style='color:#e74c3c'>[AUTO-KILL]</b></p>"
        f"<p>Reason: <b>{reason}</b></p>"
    )
    if equity is not None:
        html += f"<p>Equity: <b>{equity:,.2f} EUR</b></p>"
    await _send(f"AUTO-KILL: {reason}", plain, html, event_class="auto_kill")


async def notify_circuit_breaker(reason: str, equity: float | None = None) -> None:
    plain = f"[CIRCUIT BREAKER] {reason}\n"
    if equity is not None:
        plain += f"Equity: {equity:,.2f} EUR\n"
    html = (
        f"<p><b style='color:#f39c12'>[CIRCUIT BREAKER]</b></p>"
        f"<p>{reason}</p>"
    )
    if equity is not None:
        html += f"<p>Equity: <b>{equity:,.2f} EUR</b></p>"
    await _send(f"CIRCUIT BREAKER: {reason}", plain, html,
                 event_class="circuit_breaker")


async def notify_daily_summary(
    *,
    date: str,
    n_trades: int,
    wins: int,
    losses: int,
    net_pnl: float,
    summary: str | None = None,
    recommendations: list[dict] | None = None,
) -> None:
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0.0
    subject = f"Daily {date}: {n_trades} trades, {wins}W/{losses}L, {net_pnl:+.2f} EUR"

    plain = (
        f"Daily summary {date}\n"
        f"Trades: {n_trades} | Wins: {wins} | Losses: {losses} "
        f"| Win rate: {win_rate:.1f}%\n"
        f"Net P&L: {net_pnl:+.2f} EUR\n"
    )
    if summary:
        plain += f"\nSummary:\n{summary}\n"
    if recommendations:
        plain += "\nRecommendations:\n"
        for r in recommendations:
            if isinstance(r, dict):
                plain += f"  - {r.get('change')}: {r.get('why')}\n"
            else:
                plain += f"  - {r}\n"

    html = (
        f"<h3>Daily summary {date}</h3>"
        f"<p>Trades: <b>{n_trades}</b> &middot; "
        f"Wins: <span style='color:#2ecc71'><b>{wins}</b></span> &middot; "
        f"Losses: <span style='color:#e74c3c'><b>{losses}</b></span> &middot; "
        f"Win rate: <b>{win_rate:.1f}%</b></p>"
        f"<p>Net P&amp;L: {_fmt_pnl_html(net_pnl)}</p>"
    )
    if summary:
        html += f"<p><b>Summary:</b><br/>{summary}</p>"
    if recommendations:
        html += "<p><b>Recommendations:</b></p><ul>"
        for r in recommendations:
            if isinstance(r, dict):
                html += (f"<li><b>{r.get('change')}</b>: "
                         f"<i>{r.get('why')}</i></li>")
            else:
                html += f"<li>{r}</li>"
        html += "</ul>"

    await _send(subject, plain, html, event_class="daily_summary")


async def notify_tuning_applied(applied_keys: dict[str, float]) -> None:
    """One mail per auto-apply tick covering all keys actually changed.
    `applied_keys` is {key: new_value} after the per-row dedupe upstream."""
    if not applied_keys:
        return
    keys_str = ", ".join(f"{k}={v}" for k, v in applied_keys.items())
    plain = (
        f"[TUNING] auto-applied {len(applied_keys)} config change(s):\n"
        + "\n".join(f"  {k} = {v}" for k, v in applied_keys.items())
    )
    html = (
        f"<p><b>[TUNING] auto-applied {len(applied_keys)} config change(s)</b></p>"
        "<ul>"
        + "".join(f"<li><code>{k}</code> = <b>{v}</b></li>"
                   for k, v in applied_keys.items())
        + "</ul>"
    )
    await _send(f"TUNING applied: {keys_str}", plain, html,
                 event_class="tuning_applied")


async def notify_critical_finding(
    *,
    detector: str,
    subject: str,
    body: str | None = None,
) -> None:
    plain = f"[CRITICAL/{detector}] {subject}\n"
    if body:
        plain += f"\n{body}\n"
    html = (
        f"<p><b style='color:#e74c3c'>[CRITICAL/{detector}]</b> {subject}</p>"
    )
    if body:
        html += f"<p>{body}</p>"
    await _send(f"CRITICAL/{detector}: {subject}", plain, html,
                 event_class="critical_finding")


async def notify_news_watch_high(
    *,
    symbol: str,
    headline: str,
    action: str,
    reasoning: str | None = None,
) -> None:
    subject = f"NEWS {symbol}: {headline[:60]}"
    plain = (
        f"[NEWS-WATCH HIGH] {symbol}\n"
        f"Headline: {headline}\n"
        f"Bot action: {action}\n"
    )
    if reasoning:
        plain += f"Reasoning: {reasoning}\n"
    html = (
        f"<p><b style='color:#e74c3c'>[NEWS-WATCH HIGH]</b> "
        f"<code>{symbol}</code></p>"
        f"<p><b>Headline:</b> {headline}</p>"
        f"<p><b>Bot action:</b> {action}</p>"
    )
    if reasoning:
        html += f"<p><i>{reasoning}</i></p>"
    await _send(subject, plain, html, event_class="news_watch_high")
