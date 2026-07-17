#!/usr/bin/env python3
"""
scripts/session_start_check.py — session-start safety net (replaces reliance on
unreliable in-process "cron" for dated reminders).

WHY: Hermes cron jobs are an IN-PROCESS 60s ticker thread (InProcessCronScheduler),
NOT Windows Task Scheduler. If the laptop is off at the scheduled time, one-time
jobs SILENTLY NEVER FIRE — there is no OS-level make-up. So dated reminders
(hy3 2026-07-21, laguna-m.1 2026-07-28, future ones) cannot depend on cron.

The RELIABLE trigger is SESSION START: whenever the operator opens Hermes, this
script runs and compares today's date against every dated reminder in
PROJECT_STATUS.md. If today >= a reminder date and it is NOT marked DONE, it
surfaces a TOP-OF-FILE alert — regardless of whether the cron version fired.

This makes cron a nice-to-have for same-day awareness; the real guarantee is
date comparison at session start, which works no matter how irregularly the
laptop is used.

Read-only: prints alerts to stdout. Does NOT edit anything (the operator marks
DONE manually, or Hermes does after the action is taken).
"""
import os
import re
import datetime as dt
import sys

STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "PROJECT_STATUS.md")


def parse_dated_reminders(text):
    """Parse the '## Dated reminders (authoritative)' block.
    Expected lines:
      - 2026-07-21: hy3 retirement (NOT in config — confirm gone) [DONE:2026-07-21]
      - 2026-07-28: laguna-m.1 retirement (IS DELEGATION — benchmark replacement)
    A [DONE:date] suffix means already handled.
    """
    out = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("## Dated reminders (authoritative)"):
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("## "):
                break
            m = re.match(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s*:\s*(.+)$", line)
            if m:
                date_str, desc = m.group(1), m.group(2).strip()
                done = None
                dm = re.search(r"\[DONE:(\d{4}-\d{2}-\d{2})\]", desc)
                if dm:
                    done = dm.group(1)
                    desc = desc[:dm.start()].strip()
                out.append({"date": date_str, "desc": desc, "done": done})
    return out


def main():
    text = open(STATUS_PATH, encoding="utf-8").read()
    reminders = parse_dated_reminders(text)
    today = dt.datetime.now().date()
    overdue = []
    for r in reminders:
        try:
            rd = dt.datetime.fromisoformat(r["date"]).date()
        except ValueError:
            continue
        if r["done"]:
            continue
        if today >= rd:
            overdue.append(r)
    if not overdue:
        print(f"[session_start_check] {today.isoformat()} — no overdue dated reminders. "
              f"({len(reminders)} tracked, all clear or done)")
        sys.exit(0)
    print(f"\n########## SESSION-START ALERT ({today.isoformat()}) ##########")
    for r in overdue:
        print(f"  OVERDUE {r['date']}: {r['desc']}")
    print("  >>> Action required before relying on the model/health system.")
    print("  >>> Cron may NOT have fired (in-process scheduler, laptop was off).")
    print("##################################################\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
