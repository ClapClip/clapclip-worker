"""Internal Python continuation of the native no-argument bootstrap."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediaworker.managed.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["install", *sys.argv[1:]]))
