"""One installer, offline doctor and foreground start path for all platforms."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediaworker.deployment import main

if __name__ == "__main__":
    main()
