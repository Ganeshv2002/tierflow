"""Hook entry point: python scripts/hook.py <event>. Keeps imports local to the plugin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tierflow.hooks import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv))
