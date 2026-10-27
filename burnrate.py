#!/usr/bin/env python3
"""
BurnRate CLI v0.3: see where your AI coding money goes.

Reads usage logs that AI coding tools already store on your machine,
estimates what that usage would cost at API prices, and points out waste.

Privacy: everything runs locally. Only metadata is read (model, token counts,
timestamps, tool names). Prompts, code and responses are never read or sent anywhere.

Usage:
    python3 burnrate.py               # analyze the last 30 days
    python3 burnrate.py --days 7      # last 7 days
    python3 burnrate.py --demo        # try it with sample data
    python3 burnrate.py --html report.html
    python3 burnrate.py --install-statusline   # live context meter in Claude Code (terminal)
    python3 burnrate.py --watch                # live meter in its own window (works with Claude Desktop too)
"""

import argparse
import html as htmllib
import json
import os
import platform
import random
import shutil
import time
import tempfile
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "0.4.3"

# ---------------------------------------------------------------------------
# Prices in USD per 1M tokens. These are ESTIMATES: check them against the
# providers' official pricing pages and edit them here if they are out of date.
# cache_write / cache_read are multipliers of the input price.
# ---------------------------------------------------------------------------
PRICES = {
    # family keyword: (input, output)
    "opus":        (5.00, 25.00),
    "sonnet":      (3.00, 15.00),
    "haiku":       (1.00, 5.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4":     (2.50, 15.00),
    "gpt-5":       (2.50, 15.00),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10

# Cheaper model to suggest for each expensive family.
DOWNGRADE = {"opus": "sonnet", "gpt-5.4": "gpt-5.4-mini", "gpt-5": "gpt-5.4-mini"}

# A response with fewer output tokens than this is treated as a "small task".
SMALL_TASK_OUTPUT_TOKENS = 400

# Share of output tokens that concise-output instructions typically remove.
# This is an ASSUMPTION used only for the estimate; measure your own results.
CONCISE_SAVINGS_SHARE = 0.25

# Context thresholds (tokens) to simulate "start fresh / compact at this size".
CONTEXT_THRESHOLDS = (100_000, 150_000, 200_000, 300_000)
DEFAULT_THRESHOLD = 150_000


def price_for(model):
    m = (model or "").lower()
    for key in sorted(PRICES, key=len, reverse=True):
        if key in m:
            return key, PRICES[key]
    return None, None


def cost_of(model, inp, out, cache_w=0, cache_r=0):
    _, p = price_for(model)
    if not p:
        return 0.0
    pin, pout = p
    return (inp * pin + cache_w * pin * CACHE_WRITE_MULT + cache_r * pin * CACHE_READ_MULT + out * pout) / 1_000_000


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Readers. Each returns a list of "events":
#   {tool, model, ts, session, project, input, output, cache_w, cache_r, tools_used:[...]}
# ---------------------------------------------------------------------------

def read_claude_code(since):
    """Claude Code keeps one JSONL file per session under ~/.claude/projects/."""
    roots = [Path.home() / ".claude" / "projects"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        roots.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects")
    events, seen = [], set()
    for root in roots:
        if not root.is_dir():
            continue
        for f in root.rglob("*.jsonl"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < since:
                    continue
                with f.open(encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("type") != "assistant":
                            continue
                        msg = d.get("message") or {}
                        usage = msg.get("usage") or {}
                        if not usage or msg.get("model") == "<synthetic>":
                            continue
                        # Streaming can write the same message more than once.
                        key = (msg.get("id"), d.get("requestId"))
                        if key != (None, None):
                            if key in seen:
                                continue
                            seen.add(key)
                        ts = parse_ts(d.get("timestamp"))
                        if ts and ts < since:
                            continue
                        tools_used = [c.get("name") for c in (msg.get("content") or [])
                                      if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name")]
                        events.append({
                            "tool": "Claude Code",
                            "model": msg.get("model") or "unknown",
                            "ts": ts,
                            "session": d.get("sessionId") or f.stem,
                            "project": Path(d.get("cwd") or f.parent.name).name,
                            "input": usage.get("input_tokens", 0) or 0,
                            "output": usage.get("output_tokens", 0) or 0,
                            "cache_w": usage.get("cache_creation_input_tokens", 0) or 0,
                            "cache_r": usage.get("cache_read_input_tokens", 0) or 0,
                            "tools_used": tools_used,
                        })
            except OSError:
                continue
    return events


def read_codex(since):
    """OpenAI Codex CLI keeps session logs under ~/.codex/sessions/ (experimental parser)."""
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    events = []
    if not root.is_dir():
        return events
    for f in root.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < since:
                continue
            model, project = "gpt-5", f.parent.name
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    p = d.get("payload") or {}
                    if d.get("type") == "turn_context" and p.get("model"):
                        model = p["model"]
                        if p.get("cwd"):
                            project = Path(p["cwd"]).name
                    if d.get("type") == "event_msg" and p.get("type") == "token_count":
                        last = ((p.get("info") or {}).get("last_token_usage")) or {}
                        if not last:
                            continue
                        ts = parse_ts(d.get("timestamp"))
                        if ts and ts < since:
                            continue
                        cached = last.get("cached_input_tokens", 0) or 0
                        events.append({
                            "tool": "Codex",
                            "model": model,
                            "ts": ts,
                            "session": f.stem,
                            "project": project,
                            "input": max(0, (last.get("input_tokens", 0) or 0) - cached),
                            "output": last.get("output_tokens", 0) or 0,
                            "cache_w": 0,
                            "cache_r": cached,
                            "tools_used": [],
                        })
        except OSError:
            continue
    return events


def configured_mcp_servers():
    """Find MCP servers configured for Claude Code and Claude Desktop."""
    servers = {}
    home = Path.home()
    candidates = [(home / ".claude.json", "Claude Code"), (home / ".claude" / "settings.json", "Claude Code")]
    system = platform.system()
    if system == "Darwin":
        candidates.append((home / "Library/Application Support/Claude/claude_desktop_config.json", "Claude Desktop"))
    elif system == "Windows":
        candidates.append((Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json", "Claude Desktop"))
    else:
        candidates.append((home / ".config/Claude/claude_desktop_config.json", "Claude Desktop"))
    for path, app in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for name in (data.get("mcpServers") or {}):
            servers.setdefault(name, set()).add(app)
        for proj in (data.get("projects") or {}).values():
            if isinstance(proj, dict):
                for name in (proj.get("mcpServers") or {}):
                    servers.setdefault(name, set()).add(app)
    return servers


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

def demo_data(since, days):
    """Sample data shaped like real usage: sessions whose context grows turn by turn."""
    rnd = random.Random(42)
    events = []
    projects = ["web-app", "api-server", "mobile", "scripts"]
    now = datetime.now(timezone.utc)
    for sid in range(90):
        tool = rnd.choices(["Claude Code", "Codex"], [0.8, 0.2])[0]
        if tool == "Claude Code":
            model = rnd.choices(["claude-opus-4-7", "claude-sonnet-4-6"], [0.8, 0.2])[0]
        else:
            model = rnd.choices(["gpt-5.4", "gpt-5.4-mini"], [0.8, 0.2])[0]
        project = rnd.choice(projects)
        start = now - timedelta(minutes=rnd.randint(60, days * 24 * 60))
        base = rnd.randint(15_000, 30_000)          # system prompt, tools, project memory
        ctx = base
        turns = rnd.choice([8, 15, 30, 60, 120])
        for t in range(turns):
            small = rnd.random() < 0.55
            out = rnd.randint(40, 380) if small else rnd.randint(600, 3500)
            added = rnd.randint(500, 9000)          # file reads, tool results, new messages
            tools_used = []
            if tool == "Claude Code" and rnd.random() < 0.25:
                tools_used = [rnd.choice(["mcp__github__get_file_contents", "mcp__filesystem__read_file", "Bash", "Edit"])]
            events.append({
                "tool": tool, "model": model, "ts": start + timedelta(minutes=t * 2),
                "session": f"s{sid}", "project": project,
                "input": rnd.randint(5, 60), "output": out,
                "cache_w": added, "cache_r": ctx,
                "tools_used": tools_used,
            })
            ctx += added + out
    mcp = {"github": {"Claude Code"}, "filesystem": {"Claude Code"}, "slack": {"Claude Desktop"},
           "notion": {"Claude Desktop"}, "puppeteer": {"Claude Code"}}
    return events, mcp


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def context_of(e):
    return e["input"] + e["cache_w"] + e["cache_r"]


def simulate_reset(session_events, threshold):
    """Estimate savings if the session had been cleared/compacted whenever its
    context grew past `threshold`. Returns (usd_saved, resets).

    Model: after a reset, context drops back to the session's starting size
    (system prompt, tools, project memory) and grows again from there. Each
    reset costs a cache write of that starting context. This is an estimate.
    """
    evs = sorted(session_events, key=lambda e: e["ts"] or datetime.min.replace(tzinfo=timezone.utc))
    if not evs:
        return 0.0, 0
    fam, p = price_for(evs[0]["model"])
    if not p:
        return 0.0, 0
    pin = p[0]
    base = context_of(evs[0])
    offset, saved_tokens, resets, penalty = 0, 0, 0, 0.0
    for e in evs:
        ctx = context_of(e)
        if ctx - offset > threshold:
            offset = max(0, ctx - base)
            resets += 1
            penalty += base * pin * (CACHE_WRITE_MULT - CACHE_READ_MULT) / 1_000_000
        effective = max(base, ctx - offset)
        saved_tokens += max(0, ctx - effective)
    usd = saved_tokens * pin * CACHE_READ_MULT / 1_000_000 - penalty
    return max(0.0, usd), resets


def analyze_context(events):
    sessions = defaultdict(list)
    for e in events:
        sessions[(e["tool"], e["session"], e["project"])].append(e)
    total_ctx = sum(context_of(e) for e in events)
    avg_ctx = total_ctx / len(events) if events else 0
    proj = defaultdict(list)
    for e in events:
        proj[e["project"]].append(context_of(e))
    by_project_avg = sorted(((k, sum(v) / len(v)) for k, v in proj.items()), key=lambda x: -x[1])[:8]

    over = sum(1 for e in events if context_of(e) > DEFAULT_THRESHOLD)
    session_rows = []
    for key, evs in sessions.items():
        ctxs = [context_of(e) for e in evs]
        cost = sum(cost_of(e["model"], e["input"], e["output"], e["cache_w"], e["cache_r"]) for e in evs)
        session_rows.append({
            "tool": key[0], "session": key[1], "project": key[2],
            "turns": len(evs), "peak": max(ctxs), "avg": sum(ctxs) / len(ctxs), "cost": cost,
            "over": sum(1 for c in ctxs if c > DEFAULT_THRESHOLD),
        })
    session_rows.sort(key=lambda x: -x["cost"])

    scenarios = []
    for t in CONTEXT_THRESHOLDS:
        usd, resets = 0.0, 0
        for evs in sessions.values():
            u, r_ = simulate_reset(evs, t)
            usd += u
            resets += r_
        scenarios.append({"threshold": t, "saved": usd, "resets": resets})
    return {
        "avg": avg_ctx,
        "by_project_avg": by_project_avg,
        "over_threshold": over,
        "sessions": session_rows[:8],
        "long_sessions": sum(1 for s_ in session_rows if s_["peak"] > DEFAULT_THRESHOLD),
        "session_count": len(session_rows),
        "scenarios": scenarios,
        "best": max(scenarios, key=lambda x: x["saved"])["threshold"],
        "default_saved": max(x["saved"] for x in scenarios),
    }


def analyze(events, mcp_servers, days):
    r = {"days": days, "events": len(events)}
    total = 0.0
    by_tool, by_model, by_project, by_session = defaultdict(float), defaultdict(float), defaultdict(float), defaultdict(float)
    tok = defaultdict(int)
    unknown_models = set()
    routing_savings, small_expensive = 0.0, 0
    output_cost = 0.0
    by_type = defaultdict(float)
    used_servers = defaultdict(int)

    for e in events:
        fam, _ = price_for(e["model"])
        if not fam:
            unknown_models.add(e["model"])
        c = cost_of(e["model"], e["input"], e["output"], e["cache_w"], e["cache_r"])
        total += c
        by_tool[e["tool"]] += c
        by_model[e["model"]] += c
        by_project[e["project"]] += c
        by_session[(e["tool"], e["session"], e["project"])] += c
        for k in ("input", "output", "cache_w", "cache_r"):
            tok[k] += e[k]
        if fam:
            pin, pout = PRICES[fam]
            output_cost += e["output"] * pout / 1_000_000
            by_type["Fresh input"] += e["input"] * pin / 1_000_000
            by_type["Cache writes"] += e["cache_w"] * pin * CACHE_WRITE_MULT / 1_000_000
            by_type["Cache reads (context)"] += e["cache_r"] * pin * CACHE_READ_MULT / 1_000_000
            by_type["Output"] += e["output"] * pout / 1_000_000
        if fam in DOWNGRADE and e["output"] < SMALL_TASK_OUTPUT_TOKENS:
            cheaper = DOWNGRADE[fam]
            alt = cost_of(cheaper, e["input"], e["output"], e["cache_w"], e["cache_r"])
            routing_savings += max(0.0, c - alt)
            small_expensive += 1
        for t in e["tools_used"]:
            if t.startswith("mcp__"):
                parts = t.split("__")
                if len(parts) >= 3:
                    used_servers[parts[1]] += 1

    unused = sorted(s for s in mcp_servers if s not in used_servers)
    concise_savings = output_cost * CONCISE_SAVINGS_SHARE
    ctx = analyze_context(events)
    # Routing savings shrink once context is leaner, so scale them down to avoid double counting.
    ctx_share = min(1.0, ctx["default_saved"] / total) if total else 0.0
    routing_after_ctx = routing_savings * (1 - ctx_share)
    savings = ctx["default_saved"] + routing_after_ctx + concise_savings

    r.update({
        "total": total,
        "monthly": total / max(days, 1) * 30,
        "by_tool": sorted(by_tool.items(), key=lambda x: -x[1]),
        "by_model": sorted(by_model.items(), key=lambda x: -x[1]),
        "by_type": sorted(by_type.items(), key=lambda x: -x[1]),
        "by_project": sorted(by_project.items(), key=lambda x: -x[1])[:8],
        "top_sessions": sorted(by_session.items(), key=lambda x: -x[1])[:5],
        "tokens": dict(tok),
        "unknown_models": sorted(unknown_models),
        "routing_savings": routing_savings,
        "routing_after_ctx": routing_after_ctx,
        "small_expensive": small_expensive,
        "concise_savings": concise_savings,
        "mcp_servers": {k: sorted(v) for k, v in mcp_servers.items()},
        "mcp_used": dict(used_servers),
        "mcp_unused": unused,
        "context": ctx,
        "savings": savings,
        "savings_pct": (savings / total * 100) if total else 0,
    })
    return r


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class C:
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    B = "\033[1m" if on else ""
    D = "\033[2m" if on else ""
    O = "\033[38;5;209m" if on else ""
    G = "\033[38;5;79m" if on else ""
    R = "\033[38;5;204m" if on else ""
    X = "\033[0m" if on else ""


def money(x):
    return f"${x:,.2f}"


def tokens(n):
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def bar(v, mx, width=24):
    n = int(round(width * v / mx)) if mx else 0
    return "█" * n + "░" * (width - n)


def print_report(r, demo):
    p = print
    p()
    p(f"{C.O}{C.B}  🔥 BurnRate{C.X} {C.D}v{VERSION} · last {r['days']} days{' · DEMO DATA' if demo else ''}{C.X}")
    p(f"{C.D}  {'─' * 58}{C.X}")
    if not r["events"]:
        p(f"\n  {C.R}No usage logs found.{C.X}")
        p("  BurnRate v0.1 reads logs from Claude Code and Codex CLI.")
        p("  Claude Desktop chat doesn't store token usage locally, so it can't be analyzed yet.")
        p(f"  Try {C.B}python3 burnrate.py --demo{C.X} to see what a report looks like.\n")
        return
    p(f"\n  {C.B}Estimated spend at API prices{C.X}")
    p(f"  {C.B}{money(r['total'])}{C.X} in {r['days']} days  {C.D}≈ {money(r['monthly'])} / month{C.X}")
    p(f"  {C.D}{r['events']:,} AI responses analyzed{C.X}")

    for title, rows in (("By tool", r["by_tool"]), ("By model", r["by_model"]), ("By token type", r["by_type"]), ("By project", r["by_project"])):
        p(f"\n  {C.B}{title}{C.X}")
        mx = rows[0][1] if rows else 0
        for name, v in rows:
            p(f"  {name[:22]:<22} {C.O}{bar(v, mx)}{C.X} {money(v):>11}")

    p(f"\n  {C.B}Most expensive sessions{C.X}")
    for (tool, sess, proj), v in r["top_sessions"]:
        p(f"  {money(v):>10}  {C.D}{tool} · {proj} · {str(sess)[:12]}{C.X}")

    cx = r["context"]
    p(f"\n  {C.O}{C.B}🧠 Context analysis{C.X}")
    p(f"  {C.D}{'─' * 58}{C.X}")
    p(f"  Average context per response: {C.B}{tokens(cx['avg'])}{C.X} tokens")
    p(f"  {cx['over_threshold']:,} responses ran with more than {tokens(DEFAULT_THRESHOLD)} tokens of context")
    p(f"  {cx['long_sessions']} of {cx['session_count']} sessions grew past {tokens(DEFAULT_THRESHOLD)}")
    p(f"\n  {C.B}Average context by project{C.X}")
    mx = cx["by_project_avg"][0][1] if cx["by_project_avg"] else 0
    for name, v in cx["by_project_avg"]:
        p(f"  {name[:22]:<22} {C.O}{bar(v, mx)}{C.X} {tokens(v):>11}")
    p(f"\n  {C.B}Heaviest sessions{C.X}")
    p(f"  {C.D}{'cost':>10}  {'turns':>5}  {'peak ctx':>9}  {'avg ctx':>8}  project{C.X}")
    for s_ in cx["sessions"][:5]:
        p(f"  {money(s_['cost']):>10}  {s_['turns']:>5}  {tokens(s_['peak']):>9}  {tokens(s_['avg']):>8}  {s_['project']}")
    p(f"\n  {C.B}If you started fresh when context reached...{C.X}")
    for sc in cx["scenarios"]:
        mark = "  ← best for you" if sc["threshold"] == cx["best"] else ""
        p(f"  {tokens(sc['threshold']):>6} tokens  →  save {C.G}~{money(sc['saved'])}{C.X}  {C.D}({sc['resets']} resets){mark}{C.X}")
    p(f"  {C.D}In Claude Code: /clear between unrelated tasks, /compact inside long ones.{C.X}")

    p(f"\n  {C.G}{C.B}💡 Where you could save{C.X}")
    p(f"  {C.D}{'─' * 58}{C.X}")
    p(f"  {C.B}1. Lean context{C.X}           {C.G}~{money(cx['default_saved'])}{C.X}")
    p(f"     {C.D}Start fresh or compact at ~{tokens(cx['best'])} tokens of context.{C.X}")
    p(f"  {C.B}2. Right model per task{C.X}   {C.G}~{money(r['routing_savings'])}{C.X}")
    p(f"     {r['small_expensive']:,} small responses ran on a top-tier model.")
    p(f"     {C.D}Small tasks like these usually run fine on a cheaper model.{C.X}")
    p(f"  {C.B}3. Concise output{C.X}         {C.G}~{money(r['concise_savings'])}{C.X}")
    p(f"     {C.D}Assumes concise instructions cut {int(CONCISE_SAVINGS_SHARE * 100)}% of output tokens (an estimate).{C.X}")
    p(f"  {C.B}4. Unused connectors{C.X}")
    if r["mcp_servers"]:
        if r["mcp_unused"]:
            p(f"     {len(r['mcp_unused'])} of {len(r['mcp_servers'])} configured MCP servers weren't used in this period:")
            for s in r["mcp_unused"]:
                p(f"     {C.R}•{C.X} {s} {C.D}({', '.join(r['mcp_servers'][s])}){C.X}")
            p(f"     {C.D}Their tool definitions still take up context on every request.{C.X}")
            p(f"     {C.D}Usage tracking covers Claude Code only. Desktop servers may be in use there.{C.X}")
        else:
            p("     All configured MCP servers were used. Nice.")
    else:
        p(f"     {C.D}No MCP servers configured.{C.X}")
    p(f"  {C.D}{'─' * 58}{C.X}")
    p(f"  {C.B}Estimated savings: {C.G}{money(r['savings'])} ({r['savings_pct']:.0f}%){C.X}")
    p(f"  {C.D}Total adjusts routing for leaner context, so it's less than the sum. Estimates only.{C.X}")

    if r["unknown_models"]:
        p(f"\n  {C.R}Note:{C.X} no price set for: {', '.join(r['unknown_models'][:5])}. Counted as $0.")
    p(f"\n  {C.D}🔒 Read locally. Nothing was sent anywhere.{C.X}")
    p(f"  {C.D}ℹ  Costs are API-equivalent estimates. If you're on a flat subscription,{C.X}")
    p(f"  {C.D}   this shows the value you're using, not what you're billed.{C.X}\n")


def write_html(r, path, demo):
    def rows(items):
        mx = items[0][1] if items else 1
        return "".join(
            f'<div class="row"><span>{htmllib.escape(str(n))}</span><div class="bar"><i style="width:{v / mx * 100:.1f}%"></i></div><b>{money(v)}</b></div>'
            for n, v in items)
    if not r["mcp_servers"]:
        unused = "<li>No MCP servers configured.</li>"
    else:
        unused = "".join(f"<li>{htmllib.escape(s)} <small>({', '.join(r['mcp_servers'][s])})</small></li>" for s in r["mcp_unused"]) or "<li>None. All configured servers were used 🎉</li>"
    cx = r["context"]
    def trows(items):
        mx = items[0][1] if items else 1
        return "".join(
            f'<div class="row"><span>{htmllib.escape(str(n))}</span><div class="bar"><i style="width:{v / mx * 100:.1f}%"></i></div><b>{tokens(v)}</b></div>'
            for n, v in items)
    sess = "".join(
        f"<tr><td>{money(x['cost'])}</td><td>{x['turns']}</td><td>{tokens(x['peak'])}</td><td>{tokens(x['avg'])}</td><td>{htmllib.escape(str(x['project']))}</td></tr>"
        for x in cx["sessions"][:5])
    scen = "".join(
        f"<tr{' class=rec' if sc['threshold'] == cx['best'] else ''}><td>{tokens(sc['threshold'])} tokens</td><td class=g>~{money(sc['saved'])}</td><td>{sc['resets']}</td></tr>"
        for sc in cx["scenarios"])
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>BurnRate report</title>
<style>body{{font-family:Inter,system-ui,sans-serif;background:#07080c;color:#f2f4f8;max-width:860px;margin:40px auto;padding:0 24px}}
h1{{font-size:1.6rem}}h1 span{{color:#ff6b35}}.big{{font-size:3rem;font-weight:800}}.muted{{color:#8b93a7}}
.card{{background:#11141c;border:1px solid #232a36;border-radius:14px;padding:22px;margin:16px 0}}
.row{{display:grid;grid-template-columns:200px 1fr 110px;gap:12px;align-items:center;margin:8px 0;font-size:.92rem}}
.bar{{height:8px;background:#1b2030;border-radius:6px;overflow:hidden}}.bar i{{display:block;height:100%;background:linear-gradient(90deg,#ff6b35,#ffb547)}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}td,th{{padding:7px 6px;border-bottom:1px solid #232a36;text-align:left}}th{{color:#8b93a7;font-weight:500}}
tr.rec td{{background:#22d39a14}}.ctx{{border-color:#ff6b3566}}
.save{{border-color:#22d39a55}}.g{{color:#22d39a}}b{{text-align:right}}</style></head><body>
<h1>🔥 Burn<span>Rate</span> report</h1><p class="muted">Last {r['days']} days · {r['events']:,} AI responses{' · DEMO DATA' if demo else ''}</p>
<div class="card"><div class="muted">Estimated spend at API prices</div><div class="big">{money(r['total'])}</div>
<div class="muted">≈ {money(r['monthly'])} per month</div></div>
<div class="card"><h3>By tool</h3>{rows(r['by_tool'])}</div>
<div class="card"><h3>By model</h3>{rows(r['by_model'])}</div>
<div class="card"><h3>By token type</h3>{rows(r['by_type'])}</div>
<div class="card"><h3>By project</h3>{rows(r['by_project'])}</div>
<div class="card ctx"><h3>🧠 Context analysis</h3>
<p>Average context per response: <b>{tokens(cx['avg'])}</b> tokens<br>
<span class="muted">{cx['over_threshold']:,} responses ran with more than {tokens(DEFAULT_THRESHOLD)} tokens · {cx['long_sessions']} of {cx['session_count']} sessions grew past it</span></p>
<h4>Average context by project</h4>{trows(cx['by_project_avg'])}
<h4>Heaviest sessions</h4><table><tr><th>Cost</th><th>Turns</th><th>Peak context</th><th>Avg context</th><th>Project</th></tr>{sess}</table>
<h4>If you started fresh when context reached…</h4><table><tr><th>Threshold</th><th>Estimated saving</th><th>Resets</th></tr>{scen}</table>
<p class="muted">In Claude Code: <code>/clear</code> between unrelated tasks, <code>/compact</code> inside long ones.</p></div>
<div class="card save"><h3>💡 Where you could save</h3>
<p><b class="g">~{money(cx['default_saved'])}</b> from leaner context (start fresh or compact at ~{tokens(cx['best'])} tokens)</p>
<p><b class="g">~{money(r['routing_savings'])}</b> from routing {r['small_expensive']:,} small responses to a cheaper model</p>
<p><b class="g">~{money(r['concise_savings'])}</b> from concise output (assumes {int(CONCISE_SAVINGS_SHARE * 100)}% fewer output tokens)</p>
<p>MCP servers not used in this period:</p><ul>{unused}</ul>
<h2 class="g">Estimated savings: {money(r['savings'])} ({r['savings_pct']:.0f}%)</h2>
<p class="muted">The total adjusts routing for leaner context, so it's less than the sum. Estimates only. Measure before and after.</p></div>
<p class="muted">🔒 Generated locally. Costs are API-equivalent estimates. On a flat subscription, this shows the value you use, not what you pay.</p>
</body></html>"""
    Path(path).write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Live status line for Claude Code
# ---------------------------------------------------------------------------

STATUS_WARN = 120_000   # turns yellow
STATUS_ALERT = 200_000  # turns red: time to /compact or /clear


def _session_cost(transcript):
    """Running cost of one session, cached by file offset so it stays fast on big transcripts."""
    cache_file = Path(tempfile.gettempdir()) / "burnrate_cache_v2.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    entry = cache.get(transcript, {"offset": 0, "cost": 0.0, "last_key": None, "ctx": 0, "turns": 0})
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return entry
    if size < entry["offset"]:            # file was rewritten
        entry = {"offset": 0, "cost": 0.0, "last_key": None, "ctx": 0, "turns": 0}
    try:
        with open(transcript, "rb") as fh:
            fh.seek(entry["offset"])
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break                      # partial line, read it next time
                entry["offset"] += len(raw)
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                if msg.get("model") == "<synthetic>":
                    entry["synthetic"] = entry.get("synthetic", 0) + 1
                    continue
                if not u:
                    continue
                key = f"{msg.get('id')}|{d.get('requestId')}"
                if key == entry["last_key"]:
                    continue
                entry["last_key"] = key
                inp, cw, cr, out = (u.get("input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0,
                                    u.get("cache_read_input_tokens", 0) or 0, u.get("output_tokens", 0) or 0)
                entry["cost"] += cost_of(msg.get("model"), inp, out, cw, cr)
                entry["ctx"] = inp + cw + cr
                entry["turns"] += 1
    except OSError:
        pass
    cache[transcript] = entry
    if len(cache) > 50:                    # keep the cache small
        cache = dict(list(cache.items())[-50:])
    try:
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return entry


def statusline():
    """Called by Claude Code with session info as JSON on stdin. Prints one line."""
    # Windows pipes default to a legacy encoding that can't print emoji; force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        info = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        info = {}
    transcript = info.get("transcript_path") or ""
    model = ((info.get("model") or {}).get("display_name")) or ""
    e = _session_cost(transcript) if transcript else {"cost": 0.0, "ctx": 0, "turns": 0}
    ctx = e["ctx"]
    G, Y, R, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
    if ctx >= STATUS_ALERT:
        col, tip = R, f" {R}⚠ big context: /compact or /clear{X}"
    elif ctx >= STATUS_WARN:
        col, tip = Y, f" {D}· consider /compact soon{X}"
    else:
        col, tip = G, ""
    per_msg = ctx * 0.5 / 1_000_000 if ctx else 0   # rough: cache-read cost of resending context
    _, p = price_for(model.lower() or "opus")
    if p:
        per_msg = ctx * p[0] * CACHE_READ_MULT / 1_000_000
    parts = [
        f"🔥 {col}ctx {tokens(ctx)}{X}",
        f"{D}~{money(per_msg)}/msg{X}",
        f"session {money(e['cost'])}",
        f"{D}{e['turns']} turns{X}",
    ]
    if model:
        parts.insert(0, f"{D}{model}{X}")
    print(" │ ".join(parts) + tip)


def _latest_transcript():
    roots = [Path.home() / ".claude" / "projects"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        roots.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects")
    best, best_m = None, 0
    for root in roots:
        if root.is_dir():
            for f in root.rglob("*.jsonl"):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m > best_m:
                    best, best_m = f, m
    return best


def watch():
    """Live meter in its own window. Follows whichever Claude Code session was active last,
    including sessions started from Claude Desktop."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if os.name == "nt":
        os.system("")                      # enable colors in the Windows console
    G, Y, R, D, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
    print(f"{B}🔥 BurnRate live meter{X} {D}v{VERSION} · watching your latest Claude session · Ctrl+C to stop{X}\n")
    last_state, last_file, last_line = None, None, None
    try:
        while True:
            f = _latest_transcript()
            if not f:
                line = f"{D}Waiting for a Claude Code session…{X}"
                state = "none"
            else:
                e = _session_cost(str(f))
                ctx = e["ctx"]
                per_msg = ctx * PRICES["opus"][0] * CACHE_READ_MULT / 1_000_000
                if e["turns"] == 0 and e.get("synthetic"):
                    col, state, tip = Y, "none", f"{Y}No Claude replies in this session yet. Usage limit reached, or an error.{X}"
                elif e["turns"] == 0:
                    col, state, tip = D, "none", f"{D}New session. Waiting for Claude's first reply…{X}"
                elif ctx >= STATUS_ALERT:
                    col, state, tip = R, "alert", f"{R}{B}⚠ Context is big. Type /compact (or /clear for a new task).{X}"
                elif ctx >= STATUS_WARN:
                    col, state, tip = Y, "warn", f"{Y}Getting big. Consider /compact soon.{X}"
                else:
                    col, state, tip = G, "ok", f"{G}All good.{X}"
                proj = f.parent.name.split("-")[-1] or f.parent.name
                line = (f"{col}{B}ctx {tokens(ctx):>5}{X} │ ~{money(per_msg)}/msg │ session {money(e['cost'])} │ "
                        f"{e['turns']} turns │ {D}{proj}{X}  {tip}")
                if f != last_file and last_file is not None:
                    print(f"\n{D}— switched to a new session —{X}")
                last_file = f
            if state == "alert" and last_state != "alert":
                print("\a", end="")               # beep once when crossing into red
            if os.name == "nt":
                os.system(f"title BurnRate - {state.upper()}")
            # Print a new line only when something changes. Overwriting in place breaks
            # when the line is wider than the window, so we don't do that.
            if line != last_line:
                stamp = datetime.now().strftime("%H:%M")
                print(f"{D}{stamp}{X}  {line}", flush=True)
                last_line = line
            last_state = state
            time.sleep(3)
    except KeyboardInterrupt:
        print(f"\n{D}Stopped.{X}")


def diagnose():
    """Print what the live meter sees, to troubleshoot an empty meter."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    roots = [Path.home() / ".claude" / "projects"]
    files = []
    for root in roots:
        print(f"Looking in: {root}  (exists: {root.is_dir()})")
        if root.is_dir():
            files += [f for f in root.rglob("*.jsonl")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    print(f"Session files found: {len(files)}\n")
    for f in files[:3]:
        types, with_usage, models = defaultdict(int), 0, set()
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        types["(bad line)"] += 1
                        continue
                    types[d.get("type", "(none)")] += 1
                    msg = d.get("message") or {}
                    if isinstance(msg, dict) and msg.get("usage"):
                        with_usage += 1
                        models.add(str(msg.get("model")))
        except OSError as err:
            print(f"  can't read {f}: {err}")
            continue
        age = (time.time() - f.stat().st_mtime) / 60
        print(f"{f.parent.name}/{f.name}")
        print(f"  modified {age:.0f} min ago · {f.stat().st_size / 1024:.0f} KB")
        print(f"  line types: {dict(types)}")
        print(f"  responses with usage: {with_usage} · models: {', '.join(sorted(models)) or '-'}\n")


def install_statusline():
    settings = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Couldn't read {settings} (invalid JSON). Fix it first, nothing was changed.")
            return
        backup = settings.with_suffix(".json.burnrate-backup")
        shutil.copy2(settings, backup)
        print(f"Backup saved: {backup}")
    script = str(Path(__file__).resolve())
    data["statusLine"] = {"type": "command", "command": f'"{sys.executable}" "{script}" --statusline', "padding": 0}
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"✓ Status line installed in {settings}")
    print("  Restart Claude Code to see it.")
    print("  To remove it, run: python burnrate.py --uninstall-statusline")


def uninstall_statusline():
    settings = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("No settings found. Nothing to remove.")
        return
    if "statusLine" in data and "burnrate" in str(data["statusLine"]).lower():
        del data["statusLine"]
        settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("✓ BurnRate status line removed. Restart Claude Code.")
    else:
        print("BurnRate status line isn't installed. Nothing changed.")


def main():
    ap = argparse.ArgumentParser(description="BurnRate: see where your AI coding money goes.")
    ap.add_argument("--days", type=int, default=30, help="how many days to analyze (default 30)")
    ap.add_argument("--demo", action="store_true", help="use sample data instead of your logs")
    ap.add_argument("--html", metavar="FILE", help="also write an HTML report")
    ap.add_argument("--json", metavar="FILE", help="also write raw results as JSON")
    ap.add_argument("--statusline", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--install-statusline", action="store_true", help="show a live context meter in Claude Code")
    ap.add_argument("--uninstall-statusline", action="store_true", help="remove the live context meter")
    ap.add_argument("--diagnose", action="store_true", help="troubleshoot the live meter")
    ap.add_argument("--watch", action="store_true", help="live meter in its own window (works with Claude Desktop)")
    args = ap.parse_args()

    if args.statusline:
        return statusline()
    if args.install_statusline:
        return install_statusline()
    if args.uninstall_statusline:
        return uninstall_statusline()
    if args.watch:
        return watch()
    if args.diagnose:
        return diagnose()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    if args.demo:
        events, mcp = demo_data(since, args.days)
    else:
        events = read_claude_code(since) + read_codex(since)
        mcp = configured_mcp_servers()

    r = analyze(events, mcp, args.days)
    print_report(r, args.demo)
    if args.html and r["events"]:
        write_html(r, args.html, args.demo)
        print(f"  📄 HTML report saved to {args.html}\n")
    if args.json:
        Path(args.json).write_text(json.dumps(r, default=str, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
