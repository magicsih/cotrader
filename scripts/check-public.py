"""Check tracked source only; operator files remain outside the publication boundary."""

import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
private_roots = {"private", "data", "reports", "backtests", "logs", "artifacts", ".venv", "node_modules"}
private_suffixes = {".sql", ".db", ".sqlite", ".csv", ".parquet", ".pem", ".key", ".p12", ".pfx", ".env"}
errors = []
for name in filter(None, paths):
    path = Path(name)
    if private_roots.intersection(path.parts) or (path.suffix in private_suffixes and name != ".env.example"):
        errors.append(name + ": private file type")
    if path.name.startswith(".env") and path.name != ".env.example":
        errors.append(name + ": environment file")
    try:
        content = (root / path).read_text()
    except (UnicodeError, FileNotFoundError):
        continue
    if re.search(r"/Users/[A-Za-z0-9_.-]+/", content):
        errors.append(name + ": local user path")
if errors:
    print("\n".join(errors))
    raise SystemExit(1)
print(f"Public source boundary: {len(list(filter(None, paths)))} tracked files checked")
