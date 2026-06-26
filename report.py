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
    """Aggregate MCP call stats by server and by bank."""
    by_server = defaultdict(lambda: {"calls": 0, "hits": 0, "misses": 0})
    by_bank = defaultdict(lambda: {"calls": 0, "hits": 0, "misses": 0})
    by_day = defaultdict(lambda: defaultdict(int))

    for entry in entries:
        server = entry.get("server", "unknown")
        by_server[server]["calls"] += 1
        if entry.get("hit"):
            by_server[server]["hits"] += 1
        else:
            by_server[server]["misses"] += 1

        bank = entry.get("bank")
        if bank:
            by_bank[bank]["calls"] += 1
            if entry.get("hit"):
                by_bank[bank]["hits"] += 1
            else:
                by_bank[bank]["misses"] += 1

        day = entry.get("ts", "")[:10]
        by_day[day][server] += 1

    for stats in by_server.values():
        total = stats["calls"]
        stats["hit_rate"] = round(stats["hits"] / total, 3) if total > 0 else 0.0

    for stats in by_bank.values():
        total = stats["calls"]
        stats["hit_rate"] = round(stats["hits"] / total, 3) if total > 0 else 0.0

    return {"by_server": dict(by_server), "by_bank": dict(by_bank), "by_day": dict(by_day)}


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

    # Aggregate proactive recall stats
    total_proactive = 0
    total_sessions = 0
    total_turns = 0
    total_turns_with_recall = 0
    for entry in entries:
        pr = entry.get("proactive_recall", {})
        total_proactive += pr.get("sessions_with_proactive_recall", 0)
        total_sessions += pr.get("total_sessions", 0)
        total_turns += pr.get("total_agent_turns", 0)
        total_turns_with_recall += pr.get("agent_turns_with_recall", 0)

    # Aggregate exploration efficiency from the most recent entry
    exploration_data = {}
    for entry in reversed(entries):
        ee = entry.get("exploration_efficiency", {})
        if ee.get("with_recall", {}).get("sessions", 0) > 0:
            exploration_data = ee
            break

    # Aggregate session distribution from the most recent entry
    session_distribution = {}
    for entry in reversed(entries):
        sd = entry.get("session_distribution", {})
        if sd:
            session_distribution = sd
            break

    # Aggregate recall session stats from the most recent entry
    recall_session_stats = {}
    for entry in reversed(entries):
        rs = entry.get("recall_session_stats", {})
        if rs.get("sessions", 0) > 0:
            recall_session_stats = rs
            break

    # Aggregate weekly trend from the most recent entry
    weekly_trend = []
    for entry in reversed(entries):
        wt = entry.get("weekly_trend", [])
        if wt:
            weekly_trend = wt
            break

    # Epoch info from the most recent entry
    epoch_start_date = None
    for entry in reversed(entries):
        esd = entry.get("epoch_start_date")
        if esd:
            epoch_start_date = esd
            break

    return {
        "total_sessions_with_recall": total_with,
        "total_sessions_without_recall": total_without,
        "avg_corrections_with_recall": avg_corr_with,
        "avg_corrections_without_recall": avg_corr_without,
        "avg_reduction_pct": avg_reduction,
        "proactive_recall_sessions": total_proactive,
        "proactive_recall_pct": round(total_proactive / total_sessions * 100, 1) if total_sessions > 0 else None,
        "recall_adoption_pct": round(total_with / (total_with + total_without) * 100, 1) if (total_with + total_without) > 0 else None,
        "turn_recall_pct": round(total_turns_with_recall / total_turns * 100, 1) if total_turns > 0 else None,
        "exploration_efficiency": exploration_data,
        "session_distribution": session_distribution,
        "recall_session_stats": recall_session_stats,
        "weekly_trend": weekly_trend,
        "epoch_start_date": epoch_start_date,
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


def collect_ingestion_coverage() -> dict:
    """Check how much of each source is actually indexed."""
    import subprocess
    import urllib.request

    coverage = {}

    # Issues + PRs: compare gh CLI counts with bank document count
    for kind, cmd in [("issues", ["gh", "issue", "list"]), ("prs", ["gh", "pr", "list"])]:
        try:
            result = subprocess.run(
                cmd + ["--repo", "jordigilh/kubernaut", "--state", "all", "--limit", "10000",
                       "--json", "number", "--jq", "length"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                coverage[kind] = {"total": int(result.stdout.strip())}
        except Exception:
            pass

    # Bank document counts from Hindsight API
    for bank_id, cov_key in [("kubernaut-issues", "issues_indexed"), ("kubernaut-docs", "docs_indexed")]:
        try:
            url = f"http://localhost:8888/v1/default/banks/{bank_id}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            coverage[cov_key] = data.get("total_documents", 0)
        except Exception:
            pass

    # Docs: count markdown files on disk
    docs_dir = os.environ.get("ENGRAM_DOCS_DIR", os.path.expanduser("~/go/src/github.com/jordigilh/kubernaut-docs/docs"))
    code_docs_dir = os.environ.get("ENGRAM_CODE_DOCS_DIR", os.path.expanduser("~/go/src/github.com/jordigilh/kubernaut/docs"))
    try:
        published = len(list(Path(docs_dir).rglob("*.md")))
        coverage["docs_published"] = {"total": published}
    except Exception:
        pass
    try:
        repo_docs = len(list(Path(code_docs_dir).rglob("*.md")))
        coverage["docs_repo"] = {"total": repo_docs}
    except Exception:
        pass

    # Code index: row count from pgvector table
    try:
        result = subprocess.run(
            ["psql", "-h", "localhost", "-p", "5432", "-U", "hindsight", "-d", "hindsight",
             "-t", "-c", "SELECT count(*) FROM code_embeddings;"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PGPASSWORD": "hindsight"},
        )
        if result.returncode == 0:
            coverage["code_chunks"] = int(result.stdout.strip())
    except Exception:
        pass

    return coverage


def collect_freshness_stats() -> dict:
    """Compute data freshness by checking CocoIndex logs for last successful sync."""
    freshness = {}
    stderr_log = Path.home() / ".hindsight" / "logs" / "cocoindex-stderr.log"

    if not stderr_log.exists():
        return freshness

    now = datetime.now()

    # Parse the log for last successful completion timestamps per flow
    last_timestamps = {}
    try:
        with open(stderr_log) as f:
            for line in f:
                if "Issues poll: complete" in line:
                    ts_str = line[:23]
                    try:
                        last_timestamps["issues"] = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    except ValueError:
                        pass
                elif "docs-app" in line and ("watching" in line or "complete" in line or "file-watching" in line):
                    ts_str = line[:23]
                    try:
                        last_timestamps["docs"] = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    except ValueError:
                        pass
                elif "code-app" in line and ("watching" in line or "complete" in line or "file-watching" in line):
                    ts_str = line[:23]
                    try:
                        last_timestamps["code"] = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    except ValueError:
                        pass
                elif "transcript" in line.lower() and ("watching" in line or "complete" in line or "file-watching" in line):
                    ts_str = line[:23]
                    try:
                        last_timestamps["transcripts"] = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    except ValueError:
                        pass
    except Exception:
        pass

    for source, ts in last_timestamps.items():
        delta = now - ts
        hours = delta.total_seconds() / 3600
        freshness[source] = {
            "last_sync": ts.isoformat(),
            "staleness_hours": round(hours, 2),
            "staleness_minutes": round(delta.total_seconds() / 60, 1),
        }

    return freshness


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
                  token_stats: dict, days: int,
                  coverage: dict | None = None, freshness: dict | None = None) -> str:
    """Format a human-readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  ENGRAM EFFECTIVENESS REPORT — Last {days} days")
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

    # Proactive Recall
    lines.append("")
    lines.append("  PROACTIVE RECALL (Is the agent using memory without being asked?)")
    lines.append("  " + "-" * 66)
    if effectiveness and effectiveness.get("recall_adoption_pct") is not None:
        lines.append(f"  Recall adoption:     {effectiveness['recall_adoption_pct']:.1f}% of sessions use recall")
        if effectiveness.get("proactive_recall_pct") is not None:
            lines.append(f"  Proactive recall:    {effectiveness['proactive_recall_pct']:.1f}% of sessions recall without user prompting")
        if effectiveness.get("turn_recall_pct") is not None:
            lines.append(f"  Per-turn recall:     {effectiveness['turn_recall_pct']:.1f}% of agent turns include a recall call")
        lines.append("")
        adoption = effectiveness.get("recall_adoption_pct", 0) or 0
        if adoption < 50:
            lines.append("  Warning: Agent is not recalling in most sessions. The alwaysApply")
            lines.append("  rule may not be triggering. Check ~/.cursor/rules/hindsight-memory.mdc")
        elif adoption >= 80:
            lines.append("  Healthy: Agent is proactively recalling in most sessions.")
    else:
        lines.append("  No proactive recall data yet (requires nightly analysis with fixed hook).")

    # Session Distribution
    lines.append("")
    lines.append("  SESSION DISTRIBUTION (How sessions break down by size)")
    lines.append("  " + "-" * 66)
    dist = effectiveness.get("session_distribution", {}) if effectiveness else {}
    if dist:
        bucket_labels = {
            "trivial": "Trivial (<5K)",
            "small": "Small (5-15K)",
            "medium": "Medium (15-100K)",
            "large": "Large (>100K)",
        }
        lines.append(f"  {'Bucket':<20}{'With Recall':>14}{'Without Recall':>16}{'Total':>8}")
        lines.append("  " + "-" * 66)
        for bucket in ("trivial", "small", "medium", "large"):
            if bucket in dist:
                wr = dist[bucket].get("with_recall", 0)
                wor = dist[bucket].get("without_recall", 0)
                lines.append(f"  {bucket_labels[bucket]:<20}{wr:>14}{wor:>16}{wr + wor:>8}")
        lines.append("  " + "-" * 66)
        lines.append("  Trivial sessions are excluded from all trend metrics.")
    else:
        lines.append("  No session distribution data yet.")

    # Recall Session Stats
    lines.append("")
    lines.append("  RECALL SESSION STATS (Non-trivial sessions with recall)")
    lines.append("  " + "-" * 66)
    rs = effectiveness.get("recall_session_stats", {}) if effectiveness else {}
    if rs and rs.get("sessions", 0) > 0:
        lines.append(f"  Sessions:               {rs['sessions']:>8}")
        lines.append(f"  Corrections/session:    {rs['avg_corrections']:>8.2f}")
        lines.append(f"  Rework %:               {rs['avg_rework_pct']:>7.1f}%")
        lines.append(f"  Productivity density:   {rs['avg_productivity_density']:>8.4f}  (productive actions per 1K tokens)")
        lines.append(f"  First productive turn:  {rs['avg_first_productive_turn']:>8.1f}")
        lines.append(f"  Avg total tokens:       {rs['avg_total_tokens']:>8,.0f}")
    else:
        lines.append("  No recall session data yet.")

    # Weekly Trend
    lines.append("")
    lines.append("  WEEKLY TREND (Recall sessions only, from epoch)")
    lines.append("  " + "-" * 66)
    epoch_date = effectiveness.get("epoch_start_date") if effectiveness else None
    weekly = effectiveness.get("weekly_trend", []) if effectiveness else []
    if epoch_date:
        from datetime import date as _date
        days_since = (_date.today() - _date.fromisoformat(epoch_date)).days
        lines.append(f"  Epoch: {epoch_date} ({days_since} days ago)")
        if days_since < 7:
            lines.append(f"  Stabilization window: {7 - days_since} days remaining (no parameter changes)")
        else:
            lines.append("  Stabilization window: complete")
        lines.append("")
    if weekly:
        lines.append(f"  {'Week':<12}{'Sessions':>9}{'Corr/Sess':>11}{'Rework%':>9}{'ProdDensity':>13}{'1st Prod':>10}")
        lines.append("  " + "-" * 66)
        for wk in weekly:
            lines.append(
                f"  {wk['week']:<12}{wk['sessions']:>9}"
                f"{wk['corrections_per_session']:>11.2f}{wk['rework_pct']:>8.1f}%"
                f"{wk['productivity_density']:>13.4f}{wk['first_productive_turn']:>10.1f}"
            )
        lines.append("  " + "-" * 66)
        lines.append("  Corr/Sess = corrections per session (lower is better)")
        lines.append("  Rework% = tokens spent on correction loops (lower is better)")
        lines.append("  ProdDensity = productive actions per 1K tokens (higher is better)")
        lines.append("  1st Prod = avg turn where productive work starts (lower is better)")
    else:
        lines.append("  No weekly trend data yet. Data accumulates from epoch start date.")

    # Exploration Efficiency
    lines.append("")
    lines.append("  EXPLORATION EFFICIENCY (Does recall replace grep/glob searches?)")
    lines.append("  " + "-" * 66)
    ee = effectiveness.get("exploration_efficiency", {}) if effectiveness else {}
    ee_wr = ee.get("with_recall", {})
    ee_wor = ee.get("without_recall", {})
    if ee_wr.get("sessions", 0) > 0 and ee_wor.get("sessions", 0) > 0:
        exp_w = ee_wr.get("avg_exploration_before_productive", 0) or 0
        exp_wo = ee_wor.get("avg_exploration_before_productive", 0) or 0
        exp_delta = round((1 - exp_w / exp_wo) * 100) if exp_wo > 0 else 0
        lines.append(f"  {'':34}{'With Recall':>14}{'Without Recall':>16}{'Delta':>10}")
        lines.append("  " + "-" * 66)
        lines.append(
            f"  {'Avg exploration calls before':<34}{exp_w:>14.1f}{exp_wo:>16.1f}"
            f"{f'-{exp_delta}%' if exp_delta > 0 else f'+{abs(exp_delta)}%':>10}"
        )
        lines.append(f"  {'first productive action':<34}")
        lines.append(f"  {'Sessions':<34}{ee_wr['sessions']:>14}{ee_wor['sessions']:>16}")
        lines.append("  " + "-" * 66)

        ee_coco = ee.get("with_cocoindex", {})
        ee_nococo = ee.get("without_cocoindex", {})
        if ee_coco.get("sessions", 0) > 0:
            coco_exp = ee_coco.get("avg_exploration_before_productive", 0) or 0
            nococo_exp = ee_nococo.get("avg_exploration_before_productive", 0) or 0
            coco_delta = round((1 - coco_exp / nococo_exp) * 100) if nococo_exp > 0 else 0
            lines.append(f"  With CocoIndex code search:{coco_exp:>14.1f}  ({ee_coco['sessions']} sessions)")
            lines.append(f"  Without CocoIndex code search:{nococo_exp:>11.1f}  ({ee_nococo['sessions']} sessions)")
            if nococo_exp > 0:
                saved = nococo_exp - coco_exp
                lines.append(f"  Code search saves ~{saved:.1f} exploration calls/session ({coco_delta}% fewer)")
            lines.append("  " + "-" * 66)

        if exp_wo > 0 and exp_delta > 0:
            saved = exp_wo - exp_w
            lines.append(f"  Verdict: Recall front-loads context, saving ~{saved:.1f} exploration")
            lines.append(f"  calls per session ({exp_delta}% fewer search operations).")
        elif exp_delta <= 0:
            lines.append("  Note: Recall is not yet reducing exploration calls. Content may")
            lines.append("  need more coverage or exploration patterns may differ.")
    elif ee_wr.get("sessions", 0) > 0:
        lines.append("  No sessions without recall for comparison (all sessions use recall).")
        exp_w = ee_wr.get("avg_exploration_before_productive", 0) or 0
        lines.append(f"  Avg exploration calls before first productive action: {exp_w:.1f}")
    else:
        lines.append("  No exploration efficiency data yet. Requires nightly analysis.")

    # (Per-bank K-score section removed — K-score metric retired due to
    # structural selection bias between recall and no-recall cohorts)

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

    # Ingestion Coverage
    lines.append("")
    lines.append("  INGESTION COVERAGE (Is the pipeline indexing everything?)")
    lines.append("  " + "-" * 66)
    if coverage:
        lines.append(f"  {'Source':<25}{'Indexed':>10}{'Total':>10}{'Coverage':>10}")
        lines.append("  " + "-" * 66)
        issues_total = coverage.get("issues", {}).get("total")
        prs_total = coverage.get("prs", {}).get("total")
        issues_indexed = coverage.get("issues_indexed", 0)
        if issues_total is not None:
            lines.append(f"  {'Issues':<25}{'—':>10}{issues_total:>10}{'—':>10}")
        if prs_total is not None:
            lines.append(f"  {'PRs':<25}{'—':>10}{prs_total:>10}{'—':>10}")
        if issues_indexed:
            lines.append(f"  {'Issues+PRs (indexed)':<25}{issues_indexed:>10}{'':>10}{'':>10}")
        docs_pub = coverage.get("docs_published", {}).get("total")
        docs_indexed = coverage.get("docs_indexed", 0)
        if docs_pub is not None:
            lines.append(f"  {'Docs (published)':<25}{'—':>10}{docs_pub:>10}{'—':>10}")
        docs_repo = coverage.get("docs_repo", {}).get("total")
        if docs_repo is not None:
            lines.append(f"  {'Docs (repo)':<25}{'—':>10}{docs_repo:>10}{'—':>10}")
        if docs_indexed:
            lines.append(f"  {'Docs (indexed)':<25}{docs_indexed:>10}{'':>10}{'':>10}")
        code_chunks = coverage.get("code_chunks")
        if code_chunks is not None:
            lines.append(f"  {'Code chunks':<25}{code_chunks:>10}{'—':>10}{'—':>10}")
        lines.append("  " + "-" * 66)
    else:
        lines.append("  Coverage data not available (run with live Hindsight + gh CLI).")

    # Data Freshness
    lines.append("")
    lines.append("  DATA FRESHNESS (Is the data current enough to be useful?)")
    lines.append("  " + "-" * 66)
    if freshness:
        targets = {
            "docs": ("< 1 hr", 1.0),
            "issues": ("< 5 min", 5.0 / 60),
            "code": ("< 5 min", 5.0 / 60),
            "transcripts": ("< 1 hr", 1.0),
        }
        lines.append(f"  {'Source':<20}{'Staleness':>14}{'Target':>10}{'Status':>10}")
        lines.append("  " + "-" * 66)
        for source in ("docs", "issues", "code", "transcripts"):
            if source not in freshness:
                continue
            fs = freshness[source]
            hours = fs["staleness_hours"]
            target_label, target_hours = targets.get(source, ("—", 999))
            if hours < 1:
                staleness_str = f"{fs['staleness_minutes']:.1f} min"
            else:
                staleness_str = f"{hours:.1f} hrs"
            status = "Healthy" if hours <= target_hours else "STALE"
            lines.append(f"  {source.capitalize():<20}{staleness_str:>14}{target_label:>10}{status:>10}")
        lines.append("  " + "-" * 66)
        stale_count = sum(1 for s, fs in freshness.items() if fs["staleness_hours"] > targets.get(s, ("", 999))[1])
        if stale_count == 0:
            lines.append("  All sources within freshness targets.")
        else:
            lines.append(f"  Warning: {stale_count} source(s) exceeding freshness target.")
            lines.append("  Check: launchctl list | grep cocoindex")
    else:
        lines.append("  Freshness data not available (CocoIndex not running or no logs).")

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


def snapshot_metrics(data: dict, path: Path) -> None:
    """Write a baseline snapshot for later comparison."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def compare_baselines(before: dict, after: dict) -> str:
    """Produce a delta report comparing two baselines."""
    lines = []
    before_date = before.get("generated", "unknown")[:10]
    lines.append("")
    lines.append(f"  BEFORE/AFTER COMPARISON (baseline: {before_date})")
    lines.append("  " + "-" * 66)
    lines.append(f"  {'Metric':<35}{'Before':>12}{'After':>12}{'Delta':>12}")
    lines.append("  " + "-" * 66)

    def _row(label, b_val, a_val, fmt="f", suffix=""):
        if b_val is None or a_val is None:
            lines.append(f"  {label:<35}{'n/a':>12}{'n/a':>12}{'':>12}")
            return
        if fmt == "pct":
            b_str = f"{b_val:.1f}%"
            a_str = f"{a_val:.1f}%"
            d_val = a_val - b_val
            d_str = f"{d_val:+.1f}pp"
        elif fmt == "x":
            b_str = f"{b_val:.2f}x"
            a_str = f"{a_val:.2f}x"
            d_val = a_val - b_val
            d_str = f"{d_val:+.2f}"
        else:
            b_str = f"{b_val:.1f}{suffix}"
            a_str = f"{a_val:.1f}{suffix}"
            d_val = a_val - b_val
            d_str = f"{d_val:+.1f}{suffix}"
        lines.append(f"  {label:<35}{b_str:>12}{a_str:>12}{d_str:>12}")

    # Exploration efficiency
    b_ee = before.get("exploration_efficiency", {})
    a_ee = after.get("exploration_efficiency", {})
    _row("Exploration calls (with recall)",
         b_ee.get("with_recall", {}).get("avg_exploration_before_productive"),
         a_ee.get("with_recall", {}).get("avg_exploration_before_productive"))
    _row("Exploration calls (without recall)",
         b_ee.get("without_recall", {}).get("avg_exploration_before_productive"),
         a_ee.get("without_recall", {}).get("avg_exploration_before_productive"))

    # Correction rates
    b_eff = before.get("effectiveness", {})
    a_eff = after.get("effectiveness", {})
    _row("Corrections/session (with recall)",
         b_eff.get("avg_corrections_with_recall"),
         a_eff.get("avg_corrections_with_recall"))

    # Recall session stats
    b_rs = before.get("recall_session_stats", b_eff.get("recall_session_stats", {}))
    a_rs = after.get("recall_session_stats", a_eff.get("recall_session_stats", {}))
    _row("Rework %", b_rs.get("avg_rework_pct"), a_rs.get("avg_rework_pct"), fmt="pct")
    _row("Productivity density", b_rs.get("avg_productivity_density"), a_rs.get("avg_productivity_density"))

    lines.append("  " + "-" * 66)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Recollect effectiveness report")
    parser.add_argument("--days", type=int, default=7, help="Number of days to analyze (default: 7)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    parser.add_argument("--snapshot", action="store_true",
                        help="Save current metrics as a baseline snapshot")
    parser.add_argument("--compare", type=str, metavar="BASELINE",
                        help="Compare current metrics against a baseline snapshot file")
    args = parser.parse_args()

    mcp_calls = load_jsonl(MCP_CALLS_LOG, days=args.days)
    effectiveness_entries = load_jsonl(EFFECTIVENESS_LOG, days=args.days)
    recall_signals = load_jsonl(RECALL_SIGNALS_LOG, days=args.days)

    mcp_stats = aggregate_mcp_calls(mcp_calls)
    effectiveness = aggregate_effectiveness(effectiveness_entries)
    probe_stats = aggregate_recall_probes(recall_signals)
    token_stats = analyze_token_consumption(days=args.days)
    coverage = collect_ingestion_coverage()
    freshness = collect_freshness_stats()

    full_data = {
        "period_days": args.days,
        "generated": datetime.now().isoformat(),
        "mcp_usage": mcp_stats["by_server"],
        "mcp_by_bank": mcp_stats.get("by_bank", {}),
        "daily_trend": mcp_stats["by_day"],
        "effectiveness": effectiveness,
        "recall_probes": probe_stats,
        "token_consumption": token_stats,
        "mental_models": collect_mental_model_stats(),
        "exploration_efficiency": effectiveness.get("exploration_efficiency", {}),
        "session_distribution": effectiveness.get("session_distribution", {}),
        "recall_session_stats": effectiveness.get("recall_session_stats", {}),
        "weekly_trend": effectiveness.get("weekly_trend", []),
        "ingestion_coverage": coverage,
        "data_freshness": freshness,
    }

    if args.snapshot:
        snap_path = LOG_DIR / f"baseline-{date.today().isoformat()}.json"
        snapshot_metrics(full_data, snap_path)
        print(f"Baseline snapshot saved to {snap_path}")
        return

    if args.compare:
        baseline_path = Path(args.compare)
        if not baseline_path.exists():
            print(f"Error: baseline file not found: {baseline_path}")
            sys.exit(1)
        with open(baseline_path) as f:
            before = json.load(f)
        print(compare_baselines(before, full_data))
        return

    if args.json:
        print(json.dumps(full_data, indent=2))
    elif args.csv:
        print(export_csv(mcp_stats, effectiveness))
    else:
        print(format_report(mcp_stats, effectiveness, probe_stats, token_stats, args.days,
                            coverage=coverage, freshness=freshness))


if __name__ == "__main__":
    main()
