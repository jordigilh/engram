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
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from glob import glob
from pathlib import Path

LOG_DIR = Path.home() / ".hindsight" / "logs"
MCP_CALLS_LOG = LOG_DIR / "mcp-calls.jsonl"
EFFECTIVENESS_LOG = LOG_DIR / "effectiveness-report.jsonl"
RECALL_SIGNALS_LOG = LOG_DIR / "recall-signals.jsonl"
TRANSCRIPTS_GLOB = os.path.expanduser("~/.cursor/projects/*/agent-transcripts/**/*.jsonl")

CORRECTION_PATTERNS = [
    re.compile(r"\bno[,.]?\s+that'?s\s+(not|wrong|incorrect)", re.I),
    re.compile(r"\bdon'?t\s+do\s+that", re.I),
    re.compile(r"\bI\s+(said|meant)\s+", re.I),
    re.compile(r"\bwrong\s+(file|path|dir|approach|method|function|model|endpoint)", re.I),
    re.compile(r"\bthat\s+broke", re.I),
    re.compile(r"\bundo\s+(that|this|it)", re.I),
    re.compile(r"\bthat'?s\s+not\s+what\s+I", re.I),
    re.compile(r"\byou\s+(shouldn'?t|should\s+not)\s+have", re.I),
    re.compile(r"\bdo\s+not\s+use\b", re.I),
    re.compile(r"\bwe\s+don'?t\s+use\b", re.I),
]


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


def collect_mental_model_stats() -> list[dict]:
    """Collect mental model status from Hindsight API."""
    import urllib.request
    banks = ["cursor-memory", "kubernaut-docs", "kubernaut-issues"]
    results = []
    for bank in banks:
        try:
            url = f"http://localhost:8888/v1/default/banks/{bank}/mental-models"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            for m in data.get("items", []):
                content_len = len(m.get("content", "") or "")
                refreshed = m.get("last_refreshed_at", "")[:10] if m.get("last_refreshed_at") else "never"
                results.append({
                    "bank": bank,
                    "id": m.get("id", "?"),
                    "content_len": content_len,
                    "refreshed": refreshed,
                })
        except Exception:
            pass
    return results


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


def analyze_token_consumption(days: int = 7) -> dict:
    """Analyze token consumption from transcripts, grouped by recall usage.

    Scans recent transcripts to compute tokens/request, tool_calls/request,
    and correction cost — the key metrics for measuring whether the MCP
    services reduce overall token spend.
    """
    cutoff = datetime.now().timestamp() - (days * 86400)
    paths = []
    for path_str in glob(TRANSCRIPTS_GLOB, recursive=True):
        p = Path(path_str)
        if p.stat().st_mtime >= cutoff:
            paths.append(p)

    with_recall = []
    without_recall = []

    for path in paths:
        total_chars = 0
        user_msgs = 0
        assistant_msgs = 0
        tool_calls = 0
        has_recall = False
        corrections = 0
        correction_cost_chars = 0
        prev_assistant_chars = 0

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        role = obj.get("role", "")
                        msg = obj.get("message", {})
                        content = msg.get("content", [])
                        msg_chars = 0
                        msg_text = ""

                        if isinstance(content, str):
                            msg_chars = len(content)
                            msg_text = content
                        elif isinstance(content, list):
                            for block in content:
                                if not isinstance(block, dict):
                                    continue
                                btype = block.get("type", "")
                                if btype == "text":
                                    t = block.get("text", "")
                                    msg_chars += len(t)
                                    msg_text += t
                                elif btype == "tool_use":
                                    tool_calls += 1
                                    if block.get("name") == "CallMcpTool":
                                        inp = block.get("input", {})
                                        if "recall" in inp.get("toolName", "").lower():
                                            has_recall = True

                        total_chars += msg_chars

                        if role == "user":
                            user_msgs += 1
                            user_text = msg_text[:500]
                            is_correction = any(
                                p.search(user_text) for p in CORRECTION_PATTERNS
                            )
                            if is_correction:
                                corrections += 1
                                correction_cost_chars += prev_assistant_chars + msg_chars
                            prev_assistant_chars = 0
                        elif role == "assistant":
                            assistant_msgs += 1
                            prev_assistant_chars = msg_chars
                    except json.JSONDecodeError:
                        continue
        except (OSError, IOError):
            continue

        if user_msgs < 2:
            continue

        approx_tokens = total_chars // 4
        entry = {
            "tokens": approx_tokens,
            "user_msgs": user_msgs,
            "assistant_msgs": assistant_msgs,
            "tool_calls": tool_calls,
            "corrections": corrections,
            "correction_cost_tokens": correction_cost_chars // 4,
            "tokens_per_request": approx_tokens // max(user_msgs, 1),
            "tool_calls_per_request": round(tool_calls / max(user_msgs, 1), 1),
        }

        if has_recall:
            with_recall.append(entry)
        else:
            without_recall.append(entry)

    def _avg(items, key):
        if not items:
            return 0
        return sum(e[key] for e in items) / len(items)

    result = {
        "sessions_analyzed": len(with_recall) + len(without_recall),
        "with_recall": {
            "sessions": len(with_recall),
            "avg_tokens_per_request": round(_avg(with_recall, "tokens_per_request")),
            "avg_tool_calls_per_request": round(_avg(with_recall, "tool_calls_per_request"), 1),
            "avg_corrections_per_session": round(_avg(with_recall, "corrections"), 2),
            "avg_correction_cost_tokens": round(_avg(with_recall, "correction_cost_tokens")),
            "avg_total_tokens": round(_avg(with_recall, "tokens")),
        },
        "without_recall": {
            "sessions": len(without_recall),
            "avg_tokens_per_request": round(_avg(without_recall, "tokens_per_request")),
            "avg_tool_calls_per_request": round(_avg(without_recall, "tool_calls_per_request"), 1),
            "avg_corrections_per_session": round(_avg(without_recall, "corrections"), 2),
            "avg_correction_cost_tokens": round(_avg(without_recall, "correction_cost_tokens")),
            "avg_total_tokens": round(_avg(without_recall, "tokens")),
        },
    }

    wr = result["with_recall"]
    wor = result["without_recall"]
    if wor["avg_tokens_per_request"] > 0 and wr["sessions"] > 0:
        diff = wor["avg_tokens_per_request"] - wr["avg_tokens_per_request"]
        result["token_efficiency_delta"] = diff
        result["token_efficiency_pct"] = round(diff / wor["avg_tokens_per_request"] * 100, 1)
    else:
        result["token_efficiency_delta"] = None
        result["token_efficiency_pct"] = None

    if wor["avg_tool_calls_per_request"] > 0 and wr["sessions"] > 0:
        diff = wor["avg_tool_calls_per_request"] - wr["avg_tool_calls_per_request"]
        result["tool_call_efficiency_pct"] = round(diff / wor["avg_tool_calls_per_request"] * 100, 1)
    else:
        result["tool_call_efficiency_pct"] = None

    total_correction_cost = (
        sum(e["correction_cost_tokens"] for e in with_recall) +
        sum(e["correction_cost_tokens"] for e in without_recall)
    )
    result["total_correction_cost_tokens"] = total_correction_cost

    return result


def format_report(mcp_stats: dict, effectiveness: dict, probe_stats: dict,
                  token_stats: dict, days: int) -> str:
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

    # Mental Models
    lines.append("")
    lines.append("  MENTAL MODELS")
    lines.append("  " + "-" * 66)
    mm_stats = collect_mental_model_stats()
    if mm_stats:
        lines.append(f"  {'Bank':<25} {'Model':<25} {'Content':>8} {'Refreshed':>12}")
        lines.append("  " + "-" * 66)
        for mm in mm_stats:
            lines.append(
                f"  {mm['bank']:<25} {mm['id']:<25} {mm['content_len']:>6} ch {mm['refreshed']:>12}"
            )
        total_content = sum(m["content_len"] for m in mm_stats)
        lines.append("  " + "-" * 66)
        lines.append(f"  Total synthesized knowledge: {total_content:,} characters across {len(mm_stats)} models")
    else:
        lines.append("  No mental models configured. Run create-mental-models.py to set up.")

    # Token Cost Analysis
    lines.append("")
    lines.append("  TOKEN COST ANALYSIS")
    lines.append("  " + "-" * 66)
    if token_stats and token_stats.get("sessions_analyzed", 0) > 0:
        wr = token_stats["with_recall"]
        wor = token_stats["without_recall"]

        lines.append(f"  {'Metric':<35} {'With Recall':>14} {'Without':>14}")
        lines.append("  " + "-" * 66)
        lines.append(f"  {'Sessions analyzed':<35} {wr['sessions']:>14} {wor['sessions']:>14}")
        lines.append(f"  {'Avg tokens/user request':<35} {wr['avg_tokens_per_request']:>13,} {wor['avg_tokens_per_request']:>13,}")
        lines.append(f"  {'Avg tool calls/user request':<35} {wr['avg_tool_calls_per_request']:>14.1f} {wor['avg_tool_calls_per_request']:>14.1f}")
        lines.append(f"  {'Avg corrections/session':<35} {wr['avg_corrections_per_session']:>14.2f} {wor['avg_corrections_per_session']:>14.2f}")
        lines.append(f"  {'Avg correction cost (tokens)':<35} {wr['avg_correction_cost_tokens']:>13,} {wor['avg_correction_cost_tokens']:>13,}")
        lines.append(f"  {'Avg total tokens/session':<35} {wr['avg_total_tokens']:>13,} {wor['avg_total_tokens']:>13,}")
        lines.append("  " + "-" * 66)

        if token_stats.get("token_efficiency_pct") is not None:
            pct = token_stats["token_efficiency_pct"]
            delta = token_stats["token_efficiency_delta"]
            direction = "fewer" if delta > 0 else "more"
            lines.append(f"  Token efficiency: {abs(delta):,} {direction} tokens/request ({abs(pct):.1f}% {'saving' if pct > 0 else 'increase'})")

        if token_stats.get("tool_call_efficiency_pct") is not None:
            tc_pct = token_stats["tool_call_efficiency_pct"]
            lines.append(f"  Tool call efficiency: {abs(tc_pct):.1f}% {'fewer' if tc_pct > 0 else 'more'} tool calls with recall")

        total_waste = token_stats.get("total_correction_cost_tokens", 0)
        if total_waste > 0:
            lines.append(f"  Total wasted on corrections: {total_waste:,} tokens")
            cost_usd = total_waste * 3 / 1_000_000
            lines.append(f"    (est. ${cost_usd:.3f} at Sonnet 4.6 input rates)")

        lines.append("")
        if wr["sessions"] < 5:
            lines.append("  Note: Small sample size for recall sessions. Metrics will stabilize")
            lines.append("  after 20+ sessions with recall active (~1 week of normal use).")
    else:
        lines.append("  No transcript data available for token analysis.")

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
    token_stats = analyze_token_consumption(days=args.days)

    if args.json:
        output = {
            "period_days": args.days,
            "generated": datetime.now().isoformat(),
            "mcp_usage": mcp_stats["by_server"],
            "daily_trend": mcp_stats["by_day"],
            "effectiveness": effectiveness,
            "recall_probes": probe_stats,
            "token_consumption": token_stats,
            "mental_models": collect_mental_model_stats(),
        }
        print(json.dumps(output, indent=2))
    elif args.csv:
        print(export_csv(mcp_stats, effectiveness))
    else:
        print(format_report(mcp_stats, effectiveness, probe_stats, token_stats, args.days))


if __name__ == "__main__":
    main()
