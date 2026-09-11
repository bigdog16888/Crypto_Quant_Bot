"""Restart launcher for CQB engine — run from repo root: python scripts/restart_engine.py

Uses sys.executable (the interpreter running this script) so the engine boots
with the exact dependency set proven by the test suite, instead of relying on
PATH resolution for a bare 'python' (which resolved to a dep-less interpreter
on 2026-09-09 and crashed on `import pandas`).
"""
import os
import subprocess
import sys

CQB_ROOT = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot"
OUT = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "cqb_watch_10016", "engine_stdout.log")

if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    p = subprocess.Popen(
        [sys.executable, "engine/run_engine.py"],
        cwd=CQB_ROOT,
        stdout=open(OUT, "a"),
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    print(f"started pid={p.pid}")
