#!/usr/bin/env python3
"""
scripts/check_model_health.py — recurring OpenRouter free-tier model health check.

WHY: Free-tier models on OpenRouter rotate (hy3 expired 2026-07-21, laguna-m.1
expires 2026-07-28 — confirmed pattern). A model configured as DEFAULT/FALLBACK/
DELEGATION can vanish or stop being free without warning. This script catches
that BEFORE the bot relies on a dead model.

WHAT IT DOES:
  1. Reads the configured model chain (DEFAULT, FALLBACK[], DELEGATION) from
     PROJECT_STATUS.md's "Model chain (authoritative)" block — single source of truth.
  2. Fetches OpenRouter live /api/v1/models.
  3. For each configured model: confirm (a) still on live list, (b) still :free,
     (c) pricing prompt+completion == 0, (d) no expiration_date within next 7 days.
  4. ALERTS (writes to TOP of PROJECT_STATUS.md) if any configured model is
     missing / no longer free / expiring within 7 days.
  5. AUTO-PROMOTE: if DEFAULT has already expired (past expiry AND gone from live
     list), promote next benchmarked fallback to DEFAULT immediately, log it,
     and flag the now-empty slot needs a fresh benchmark. It NEVER grabs an
     unbenchmarked :free model — if the fallback chain is empty, it STOPS and
     alerts the operator to run a fresh 3-incident benchmark.
  6. Logs every check (pass/alert/auto-promoted) with timestamp to PROJECT_STATUS.md
     "Model health log" section.

SAFETY: This script only READS OpenRouter + WRITES a status file. It never calls
any model, never edits Hermes config, never substitutes an unbenchmarked model.
Auto-promotion only moves an ALREADY-BENCHMARKED fallback up the chain.

Run: python scripts/check_model_health.py
Env: needs OPENROUTER_API_KEY (reads Hermes .env if present).
"""
import os
import sys
import json
import urllib.request
import re
import datetime as dt

HERMES_ENV = os.path.expanduser("C:/Users/Gionie/AppData/Local/hermes/.env")
STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "PROJECT_STATUS.md")
MODELS_API = "https://openrouter.ai/api/v1/models"
WARN_DAYS = 7  # flag expiring within this many days


def load_api_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if os.path.exists(HERMES_ENV):
        for line in open(HERMES_ENV, encoding="utf-8", errors="replace"):
            if line.strip().startswith("OPENROUTER_API_KEY"):
                return line.strip().split("=", 1)[1].strip().strip('"')
    return None


def fetch_live_models(key):
    req = urllib.request.Request(MODELS_API, headers={"Authorization": f"Bearer {key}"})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    return {m["id"]: m for m in data.get("data", [])}


def parse_chain(status_text):
    """Extract the Model chain block from PROJECT_STATUS.md.
    Expected format (written by this project's setup):
      ## Model chain (authoritative)
      DEFAULT: <id>
      FALLBACK: <id>, <id>, ...
      DELEGATION: <id>
    """
    chain = {"DEFAULT": None, "FALLBACK": [], "DELEGATION": None}
    in_block = False
    for line in status_text.splitlines():
        if line.strip().startswith("## Model chain (authoritative)"):
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("## "):
                break
            m = re.match(r"^\s*(DEFAULT|FALLBACK|DELEGATION):\s*(.*)$", line)
            if m:
                role, val = m.group(1), m.group(2).strip()
                if role == "FALLBACK":
                    chain["FALLBACK"] = [v.strip() for v in val.split(",") if v.strip()]
                else:
                    chain[role] = val or None
    return chain


def write_alert_and_log(status_text, alerts, log_lines, new_chain=None):
    """Rewrite PROJECT_STATUS.md: prepend alerts at top, append/replace log,
    optionally update the Model chain block if auto-promoted."""
    out = status_text
    # 1. Prepend alerts after the first heading line (top, not buried)
    lines = out.splitlines()
    # find title line (first '# ' heading)
    insert_at = 0
    for i, l in enumerate(lines):
        if l.startswith("# "):
            insert_at = i + 1
            break
    alert_block = ""
    if alerts:
        alert_block = "\n## MODEL ALERT\n" + "\n".join(f"- {a}" for a in alerts) + "\n"
    lines.insert(insert_at, alert_block.rstrip("\n"))
    out = "\n".join(lines) + "\n"

    # 2. Update chain block if auto-promoted
    if new_chain is not None:
        out = re.sub(
            r"(## Model chain \(authoritative\).*?)(?=\n## )",
            lambda m: _render_chain(m.group(1), new_chain),
            out, flags=re.S)

    # 3. Append log entry (keep a rolling history)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    log_section = "\n## Model health log\n"
    if log_section not in out:
        out += log_section
    entry = f"- [{ts}] " + "; ".join(log_lines)
    # insert entry right after the log heading
    out = out.replace(log_section, log_section + entry + "\n", 1)
    return out


def _render_chain(existing_block, chain):
    """Re-render the Model chain block preserving its header."""
    header = "## Model chain (authoritative)\n"
    body = f"DEFAULT: {chain['DEFAULT']}\n"
    body += f"FALLBACK: {', '.join(chain['FALLBACK'])}\n"
    body += f"DELEGATION: {chain['DELEGATION']}\n"
    return header + body


def main():
    key = load_api_key()
    if not key:
        print("[FAIL] OPENROUTER_API_KEY not found")
        sys.exit(2)
    try:
        live = fetch_live_models(key)
    except Exception as e:
        print(f"[FAIL] could not fetch live models: {e}")
        sys.exit(2)

    status_text = open(STATUS_PATH, encoding="utf-8").read()
    chain = parse_chain(status_text)

    all_configured = []
    if chain["DEFAULT"]:
        all_configured.append(("DEFAULT", chain["DEFAULT"]))
    for fb in chain["FALLBACK"]:
        all_configured.append(("FALLBACK", fb))
    if chain["DELEGATION"]:
        all_configured.append(("DELEGATION", chain["DELEGATION"]))

    alerts = []
    today = dt.datetime.now().date()
    expired_default = None

    for role, mid in all_configured:
        entry = live.get(mid)
        if entry is None:
            # gone from live list
            exp = None  # can't read expiry if gone; treat as missing
            alerts.append(f"MODEL ALERT: {mid} ({role}) is MISSING from live list — needs replacement")
            if role == "DEFAULT":
                expired_default = mid
            continue
        # pricing
        pricing = entry.get("pricing", {})
        try:
            p_p = float(pricing.get("prompt", "0") or 0)
            p_c = float(pricing.get("completion", "0") or 0)
        except (TypeError, ValueError):
            p_p = p_c = -1
        is_free = (p_p == 0 and p_c == 0) and mid.endswith(":free")
        if not is_free:
            alerts.append(f"MODEL ALERT: {mid} ({role}) is no longer free (prompt={p_p} completion={p_c}) — needs replacement")
            if role == "DEFAULT":
                expired_default = mid
            continue
        # expiry
        exp_raw = entry.get("expiration_date")
        if exp_raw:
            try:
                exp_d = dt.datetime.fromisoformat(exp_raw).date()
                days_left = (exp_d - today).days
                if days_left <= 0:
                    alerts.append(f"MODEL ALERT: {mid} ({role}) has ALREADY expired ({exp_raw}) — remove/replace")
                    if role == "DEFAULT":
                        expired_default = mid
                elif days_left <= WARN_DAYS:
                    alerts.append(f"MODEL ALERT: {mid} ({role}) expires {exp_raw} (in {days_left}d) — replace before then")
            except ValueError:
                pass  # unparseable date; ignore

    new_chain = None
    promotion_msg = None
    if expired_default is not None:
        # DEFAULT is dead. Auto-promote next benchmarked fallback.
        remaining = [f for f in chain["FALLBACK"] if f != expired_default]
        if remaining:
            new_default = remaining[0]
            new_fallback = remaining[1:]
            new_chain = {"DEFAULT": new_default, "FALLBACK": new_fallback,
                         "DELEGATION": chain["DELEGATION"]}
            promotion_msg = (f"AUTO-PROMOTED: DEFAULT '{expired_default}' was dead → "
                             f"promoted benchmarked fallback '{new_default}' to DEFAULT. "
                             f"FALLBACK chain now: {new_fallback or '(EMPTY — needs fresh benchmark)'}.")
            alerts.append(promotion_msg)
        else:
            # fallback chain empty — STOP, do not grab unbenchmarked model
            alerts.append(("STOP: DEFAULT dead AND fallback chain EMPTY. "
                           "Do NOT auto-substitute an unbenchmarked :free model. "
                           "Operator must run a fresh 3-incident benchmark to refill."))

    log_lines = []
    if not alerts:
        log_lines.append("PASS — all configured models present, free, no imminent expiry")
    else:
        log_lines.append(f"ALERTS={len(alerts)}; " + " | ".join(alerts[:3]))

    out = write_alert_and_log(status_text, alerts, log_lines, new_chain)
    open(STATUS_PATH, "w", encoding="utf-8").write(out)

    # Console summary (for session-start / cron visibility)
    print(f"[check_model_health] {dt.datetime.now().isoformat()}")
    if not alerts:
        print("  PASS: DEFAULT=%s FALLBACK=%s DELEGATION=%s — all healthy" %
              (chain["DEFAULT"], chain["FALLBACK"], chain["DELEGATION"]))
    else:
        for a in alerts:
            print("  " + a)
    if promotion_msg:
        print("  " + promotion_msg)
    print("  (details written to PROJECT_STATUS.md)")
    sys.exit(1 if alerts else 0)


if __name__ == "__main__":
    main()
