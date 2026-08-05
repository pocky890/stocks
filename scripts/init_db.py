import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.db import init_db

if __name__ == "__main__":
    config = load_config()
    init_db(config.db_path)
    print(f"DB schema initialized at {config.db_path}")
