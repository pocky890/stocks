import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    shioaji_api_key: str
    shioaji_secret_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    market_open: str
    market_close: str
    bar_interval_minutes: int
    batch_pacing_seconds: float
    strategy_params: dict
    db_path: str


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    with open(PROJECT_ROOT / "config.json", "r", encoding="utf-8") as f:
        raw = json.load(f)

    return Config(
        shioaji_api_key=os.environ.get("SHIOAJI_API_KEY", ""),
        shioaji_secret_key=os.environ.get("SHIOAJI_SECRET_KEY", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        market_open=raw["market_hours"]["open"],
        market_close=raw["market_hours"]["close"],
        bar_interval_minutes=raw["market_hours"]["bar_interval_minutes"],
        batch_pacing_seconds=raw["batch"]["pacing_seconds"],
        strategy_params=raw["strategy_params"],
        db_path=str(PROJECT_ROOT / "data" / "stocks.db"),
    )
