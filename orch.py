"""CLI entry point: python orch.py <command> ...  (see `python orch.py --help`)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierflow.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
