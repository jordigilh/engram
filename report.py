#!/usr/bin/env python3
"""Generate an effectiveness report for Hindsight + Knowledge RAG + gopls MCP.

Reads collected logs and produces a formatted report showing:
- MCP usage stats (calls, hit rates per server)
- Effectiveness correlation (corrections with/without recall)
- Token cost proxy (session length comparison)
- Trend analysis (week-over-week improvement)

Usage:
    python3 report.py                  # last 7 days
    python3 report.py --days 30        # last 30 days
    python3 report.py --json           # machine-readable output
    python3 report.py --csv            # CSV export
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

LOG_DIR = Path.home() / ".hindsight" / "logs"
MCP_CALLS_LOG = LOG_DIR / "mcp-calls.jsonl"
EFFECTIVENESS_LOG = LOG_DIR / "effectiveness-report.jsonl"
RECALL_SIGNALS_LOG = LOG_DIR / "recall-signals.jsonl"


def load_jsonl(path: Path, days: int = 7) -> list[dict]:
    """Load JSONL entries from the last N days."""
    if not path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_str = entry.get("ts") or entry.get("date", "")
                try:
                    if "T" in ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").replace("+00:00", ""))
                    else:
                        ts = datetime.strptime(ts_str, "%Y-%m-%d")
                except (ValueError, TypeError):
                    ts = datetime.now()
                if ts >= cutoff:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


def load_daily_logs(days: int = 7) -> list[dict]:
    """Load nightly JSON logs from the last N days."""
    entries = []
    for i in range(days):
        day = date.today() - timedelta(days=i)
        path = LOG_DIR / f"{day.isoformat()}.json"
        if path.exists():
            with open(path) as f:
                try:
                    entries.append(json.load(f))
                except json.JSONDecodeError:
                    continue
    return entries


def aggregate_mcp_calls(entries: list[dict]) -> dict:
    """Aggregate MCP call stats by server."""
    by_server = defaultdict(lambda: {"calls": 0, "hits": 0, "misses": 0})
    by_day = defaultdict(lambda: defaultdict(int))

    for entry in entries:
        server = entry.get("server", "unknown")
        by_server[server]["calls"] += 1
        if entry.get("hit"):
            by_server[server]["hits"] += 1
        else:
            by_server[server]["misses"] += 1

        day = entry.get("ts", "")[:10]
        by_day[day][server] += 1

    for stats in by_server.values():
        total = stats["calls"]
        stats["hit_rate"] = round(stats["hits"] / total, 3) if total > 0 else 0.0

    return {"by_server": dict(by_server), "by_day": dict(by_day)}


def aggregate_effectiveness(entries: list[dict]) -> dict:
    """Aggregate effectiveness reports."""
    if not entries:
        return {}

    total_with = 0
    total_without = 0
    total_corr_with = 0.0
    total_corr_without = 0.0
    reductions = []

    for entry in entries:
        eff = entry.get("effectiveness", {})
        with_count = eff.get("sessions_with_recall", 0)
        without_count = eff.get("sessions_without_recall", 0)
        total_with += with_count
        total_without += without_count
        total_corr_with += eff.get("corrections_per_session_with_recall", 0) * with_count
        total_corr_without += eff.get("corrections_per_session_without_recall", 0) * without_count
        if eff.get("estimated_reduction_pct") is not None:
            reductions.append(eff["estimated_reduction_pct"])

    avg_corr_with = round(total_corr_with / total_with, 2) if total_with > 0 else 0.0
    avg_corr_without = round(total_corr_without / total_without, 2) if total_without > 0 else 0.0
    avg_reduction = round(sum(reductions) / len(reductions), 1) if reductions else None

    return {
        "total_sessions_with_recall": total_with,
        "total_sessions_without_recall": total_without,
        "avg_corrections_with_recall": avg_corr_with,
        "avg_corrections_without_recall": avg_corr_without,
        "avg_reduction_pct": avg_reduction,
    }


def aggregate_recall_probes(entries: list[dict]) -> dict:
    """Aggregate recall probe latency and quality stats."""
    by_bank = defaultdict(lambda: {"probes": 0, "avg_latency_ms": 0, "avg_results": 0, "total_latency": 0, "total_results": 0})

    for entry in entries:
        if entry.get("type") != "recall_probe":
            continue
        bank = entry.get("bank", "unknown")
        by_bank[bank]["probes"] += 1
        by_bank[bank]["total_latency"] += entry.get("latency_ms", 0)
        by_bank[bank]["total_results"] += entry.get("results", 0)

    for stats in by_bank.values():
        if stats["probes"] > 0:
            stats["avg_latency_ms"] = round(stats["total_latency"] / stats["probes"])
            stats["avg_results"] = round(stats["total_results"] / stats["probes"], 1)
        del stats["total_latency"]
        del stats["total_results"]

    return dict(by_bank)


def format_report(mcp_stats: dict, effectiveness: dict, probe_stats: dict, days: int) -> str:
    """Format a human-readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  RECOLLECT EFFECTIVENESS REPORT — Last {days} days")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 70)

    # MCP Usage
    lines.append("")
    lines.append("  MCP USAGE BY SERVER")
    lines.append("  " + "-" * 66)
    by_server = mcp_stats.get("by_server", {})
    if by_server:
        lines.append(f"  {'Server':<30} {'Calls':>7} {'Hits':>7} {'Misses':>7} {'Hit Rate':>9}")
        lines.append("  " + "-" * 66)
        for server, stats in sorted(by_server.items()):
            lines.append(
                f"  {server:<30} {stats['calls']:>7} {stats['hits']:>7} "
                f"{stats['misses']:>7} {stats['hit_rate']*100:>8.1f}%"
            )
        total_calls = sum(s["calls"] for s in by_server.values())
        total_hits = sum(s["hits"] for s in by_server.values())
        lines.append("  " + "-" * 66)
        overall_rate = (total_hits / total_calls * 100) if total_calls > 0 else 0
        lines.append(f"  {'TOTAL':<30} {total_calls:>7} {total_hits:>7} {total_calls-total_hits:>7} {overall_rate:>8.1f}%")
    else:
        lines.append("  No MCP call data yet. The afterMCPExecution hook will start")
        lines.append("  collecting data from your next Cursor session.")

    # Effectiveness
    lines.append("")
    lines.append("  EFFECTIVENESS (Corrections Reduction)")
    lines.append("  " + "-" * 66)
    if effectiveness:
        lines.append(f"  Sessions with recall:    {effectiveness['total_sessions_with_recall']:>5}  "
                     f"(avg {effectiveness['avg_corrections_with_recall']:.2f} corrections/session)")
        lines.append(f"  Sessions without recall: {effectiveness['total_sessions_without_recall']:>5}  "
                     f"(avg {effectiveness['avg_corrections_without_recall']:.2f} corrections/session)")
        if effectiveness.get("avg_reduction_pct") is not None:
            lines.append(f"  Estimated correction reduction: {effectiveness['avg_reduction_pct']:.1f}%")
        lines.append("")
        if effectiveness["avg_reduction_pct"] and effectiveness["avg_reduction_pct"] > 0:
            lines.append("  Interpretation: Sessions where Hindsight recall was active had fewer")
            lines.append("  user corrections, suggesting the memory system is reducing mistakes.")
        elif effectiveness["total_sessions_with_recall"] < 5:
            lines.append("  Note: Sample size is still small. More sessions needed for")
            lines.append("  statistical significance (recommend 20+ sessions in each group).")
    else:
        lines.append("  No effectiveness data yet. Run the nightly script to generate.")

    # Recall Probe Quality
    lines.append("")
    lines.append("  RECALL PROBE QUALITY (Nightly Health Check)")
    lines.append("  " + "-" * 66)
    if probe_stats:
        lines.append(f"  {'Bank':<30} {'Probes':>7} {'Avg Latency':>12} {'Avg Results':>12}")
        lines.append("  " + "-" * 66)
        for bank, stats in sorted(probe_stats.items()):
            lines.append(
                f"  {bank:<30} {stats['probes']:>7} {stats['avg_latency_ms']:>9}ms {stats['avg_results']:>11.1f}"
            )
    else:
        lines.append("  No probe data yet.")

    # Daily trend
    by_day = mcp_stats.get("by_day", {})
    if by_day:
        lines.append("")
        lines.append("  DAILY TREND")
        lines.append("  " + "-" * 66)
        for day in sorted(by_day.keys())[-7:]:
            servers = by_day[day]
            total = sum(servers.values())
            breakdown = ", ".join(f"{s}:{c}" for s, c in sorted(servers.items()))
            lines.append(f"  {day}  total={total:>3}  ({breakdown})")

    lines.append("")
    lines.append("=" * 70)
    lines.append("  Log files:")
    lines.append(f"    MCP calls:     {MCP_CALLS_LOG}")
    lines.append(f"    Effectiveness: {EFFECTIVENESS_LOG}")
    lines.append(f"    Recall probes: {RECALL_SIGNALS_LOG}")
    lines.append(f"    Daily reports: {LOG_DIR}/<date>.json")
    lines.append("=" * 70)

    return "\n".join(lines)


def export_csv(mcp_stats: dict, effectiveness: dict) -> str:
    """Export metrics as CSV for spreadsheet import."""
    lines = ["date,server,calls,hits,misses,hit_rate"]
    for day, servers in sorted(mcp_stats.get("by_day", {}).items()):
        for server, count in sorted(servers.items()):
            lines.append(f"{day},{server},{count},,,")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Recollect effectiveness report")
    parser.add_argument("--days", type=int, default=7, help="Number of days to analyze (default: 7)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    args = parser.parse_args()

    mcp_calls = load_jsonl(MCP_CALLS_LOG, days=args.days)
    effectiveness_entries = load_jsonl(EFFECTIVENESS_LOG, days=args.days)
    recall_signals = load_jsonl(RECALL_SIGNALS_LOG, days=args.days)

    mcp_stats = aggregate_mcp_calls(mcp_calls)
    effectiveness = aggregate_effectiveness(effectiveness_entries)
    probe_stats = aggregate_recall_probes(recall_signals)

    if args.json:
        output = {
            "period_days": args.days,
            "generated": datetime.now().isoformat(),
            "mcp_usage": mcp_stats["by_server"],
            "daily_trend": mcp_stats["by_day"],
            "effectiveness": effectiveness,
            "recall_probes": probe_stats,
        }
        print(json.dumps(output, indent=2))
    elif args.csv:
        print(export_csv(mcp_stats, effectiveness))
    else:
        print(format_report(mcp_stats, effectiveness, probe_stats, args.days))


if __name__ == "__main__":
    main()
