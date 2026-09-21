# D:/Crypto_Quant_Bot/scripts/snapshot_db.py
import sqlite3, hashlib, os
from datetime import datetime
from pathlib import Path

SRC = Path(r"D:\Crypto_Quant_Bot\crypto_bot.db")
DST_DIR = Path(r"D:\Crypto_Quant_Bot\snapshots")
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
DST = DST_DIR / f"crypto_bot_rule8_{TS}_CONTAMINATED_EVIDENCE.db"
SHA_FILE = DST.with_suffix(".sha256")

os.makedirs(DST_DIR, exist_ok=True)
if DST.exists():
    raise FileExistsError(f"Refusing to overwrite {DST}")

# Open source read-only via file: URI (pathlib handles backslashes)
src_uri = SRC.as_uri() + "?mode=ro"
src = sqlite3.connect(src_uri, uri=True)
dst = sqlite3.connect(str(DST))
try:
    src.backup(dst)  # SQLite backup API -- WAL-safe
finally:
    src.close()
    dst.close()

# Verify integrity
v = sqlite3.connect(DST.as_uri() + "?mode=ro", uri=True)
cur = v.cursor()
cur.execute("PRAGMA integrity_check")
assert cur.fetchone()[0] == "ok", "Integrity check failed"
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
counts = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
v.close()

# SHA256
sha = hashlib.sha256()
with open(DST, "rb") as f:
    for chunk in iter(lambda: f.read(8192), b""): sha.update(chunk)
sha_hex = sha.hexdigest()
SHA_FILE.write_text(f"{sha_hex}  {DST.name}\n")

print(f"Snapshot: {DST}")
print(f"SHA256:   {sha_hex}")
print(f"Tables:   {counts}")
print(f"Manifest: {SHA_FILE}")