"""Copy only the integration workflow into an existing, confirmed fork checkout."""
import argparse
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("fork", type=Path)
args = parser.parse_args()
root = args.fork.expanduser().resolve()
remote = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], check=True, capture_output=True, text=True).stdout.strip()
if remote not in ("https://github.com/metalice80/outline_t.git", "git@github.com:metalice80/outline_t.git", "https://github.com/metalice80/outline_t"):
    raise SystemExit("Refusing: origin must be metalice80/outline_t, never upstream Outline.")
target = root / ".github/workflows/ai-review.yml"
source = Path(__file__).with_name("ai-review.yml")
if target.exists() and target.read_bytes() != source.read_bytes():
    raise SystemExit("Existing workflow differs; inspect it and merge manually. No files changed.")
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, target)
print(f"Prepared {target}; no commit or push performed.")
