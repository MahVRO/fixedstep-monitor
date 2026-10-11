from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any

from .timeutil import parse_utc


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def equity_with_drawdown(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    peak: float | None = None
    for source in samples:
        item = {key: value for key, value in source.items() if value is not None}
        equity = _finite_number(source.get("equity"))
        if equity is not None:
            peak = equity if peak is None else max(peak, equity)
            drawdown_abs = peak - equity
            item["drawdown_abs"] = drawdown_abs
            if peak > 0:
                item["drawdown_pct"] = drawdown_abs / peak * 100.0
        output.append(item)
    return output


def drawdown_metrics(samples: list[dict[str, Any]]) -> dict[str, float]:
    enriched = equity_with_drawdown(samples)
    values = [row for row in enriched if "drawdown_abs" in row]
    if not values:
        return {}
    current = values[-1]
    maximum_abs = max(float(row["drawdown_abs"]) for row in values)
    result: dict[str, float] = {
        "current_abs": float(current["drawdown_abs"]),
        "max_abs": maximum_abs,
        "peak_equity": max(float(row["equity"]) for row in values),
    }
    if "drawdown_pct" in current:
        result["current_pct"] = float(current["drawdown_pct"])
    percentages = [float(row["drawdown_pct"]) for row in values if "drawdown_pct" in row]
    if percentages:
        result["max_pct"] = max(percentages)
    return result


def _period_boundaries(now: datetime) -> dict[str, datetime | None]:
    current = now.astimezone(UTC)
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return {"daily": day, "weekly": week, "monthly": month, "all_time": None}


def period_realized_pnl(
    trades: list[dict[str, Any]], *, now: datetime
) -> dict[str, dict[str, float | int]]:
    periods = _period_boundaries(now)
    buckets: dict[str, list[float]] = {name: [] for name in periods}
    for trade in trades:
        net = _finite_number(trade.get("net_profit"))
        if net is None:
            continue
        buckets["all_time"].append(net)
        exit_time = trade.get("exit_time")
        if not isinstance(exit_time, str):
            continue
        try:
            timestamp = parse_utc(exit_time)
        except (ValueError, TypeError):
            continue
        for name, start in periods.items():
            if start is not None and timestamp >= start:
                buckets[name].append(net)
    return {
        name: {"realized_pnl": sum(values), "closed_trades": len(values)}
        for name, values in buckets.items()
    }


def trade_statistics(trades: list[dict[str, Any]]) -> dict[str, float | int]:
    ordered = sorted(
        trades,
        key=lambda trade: (str(trade.get("exit_time") or ""), str(trade.get("deal_id") or "")),
    )
    outcomes = [
        value
        for value in (_finite_number(trade.get("net_profit")) for trade in ordered)
        if value is not None
    ]
    result: dict[str, float | int] = {
        "closed_trades": len(trades),
        "trades_with_net_profit": len(outcomes),
    }
    if not outcomes:
        return result

    wins = [value for value in outcomes if value > 0]
    losses = [value for value in outcomes if value < 0]
    breakeven = [value for value in outcomes if value == 0]
    result.update(
        {
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "best_trade": max(outcomes),
            "worst_trade": min(outcomes),
        }
    )
    decisive = len(wins) + len(losses)
    if decisive:
        result["win_rate"] = len(wins) / decisive * 100.0
    if wins:
        result["avg_win"] = fmean(wins)
    if losses:
        result["avg_loss"] = fmean(losses)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            result["profit_factor"] = gross_profit / gross_loss

    current_wins = 0
    current_losses = 0
    max_wins = 0
    max_losses = 0
    running_wins = 0
    running_losses = 0
    for value in outcomes:
        if value > 0:
            running_wins += 1
            running_losses = 0
        elif value < 0:
            running_losses += 1
            running_wins = 0
        else:
            running_wins = 0
            running_losses = 0
        max_wins = max(max_wins, running_wins)
        max_losses = max(max_losses, running_losses)
    if outcomes[-1] > 0:
        current_wins = running_wins
    elif outcomes[-1] < 0:
        current_losses = running_losses
    result.update(
        {
            "consecutive_wins": current_wins,
            "consecutive_losses": current_losses,
            "max_consecutive_wins": max_wins,
            "max_consecutive_losses": max_losses,
        }
    )

    durations = [
        value
        for value in (_finite_number(trade.get("duration_seconds")) for trade in trades)
        if value is not None and value >= 0
    ]
    if durations:
        result["avg_duration_seconds"] = fmean(durations)
    return result


def account_summary(
    latest: dict[str, Any] | None,
    *,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    if not latest:
        return {}
    allowed = ("currency", "balance", "equity", "unrealized_pnl", "margin", "free_margin", "margin_level")
    summary = {key: latest[key] for key in allowed if key in latest and latest[key] is not None}
    initial = _finite_number(initial_capital)
    equity = _finite_number(latest.get("equity"))
    if initial is not None:
        summary["initial_capital"] = initial
        if equity is not None:
            net_return = equity - initial
            summary["net_return"] = net_return
            if initial != 0:
                summary["net_return_pct"] = net_return / initial * 100.0
    return summary
