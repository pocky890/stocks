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
    circuit_breaker_ma_period: int = 20
    circuit_breaker_enter_threshold: float = 0.60
    circuit_breaker_exit_threshold: float = 0.40
    circuit_breaker_own_ma_period: int | None = None  # 2026-08-16研究：「這支股票自己
    # 是否也跌破均線」這道AND條件，原本跟breadth_ma_period共用同一條均線(20日/月線)——
    # 但這條均線也剛好是golden_cross_scaleout/trend_following/long_swing/atr_breakout/
    # breakout這幾支策略進場條件本身要求的均線(要求「站上」才會觸發BUY)，兩者幾乎互斥，
    # 導致斷路器對這幾支策略實測擋下率是0%(見scripts/backtest_circuit_breaker_own_ma.py，
    # 也測過拉長成40/60/120日，擋下率頂多3.5%，一樣沒用)。改成None(拿掉這道AND條件，
    # 純看產業寬度)後，隊長組(半導體設備/封測同產業15檔)2026-05起的實測擋下率從0%升到
    # 38.3%，long_swing從獲利因子0.79(淨虧)翻正到1.39、trend_following虧損減半；全觀察
    # 清單10年整體只犧牲3~4%總報酬、獲利因子幾乎沒變。代價：3711日月光投控2026-04-02
    # 的trend_following訊號(當初加這道AND條件要保護的原始案例)在純寬度版本下會被誤擋，
    # 這筆實際賺了+30.7%——使用者2026-08-16在看過這個具體代價後確認接受，改為預設拿掉
    # AND條件。設成非None的整數(例如20)可以退回「兩條件都要」的舊行為。


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
        circuit_breaker_ma_period=raw["circuit_breaker"]["breadth_ma_period"],
        circuit_breaker_enter_threshold=raw["circuit_breaker"]["enter_threshold"],
        circuit_breaker_exit_threshold=raw["circuit_breaker"]["exit_threshold"],
        circuit_breaker_own_ma_period=raw["circuit_breaker"].get("own_ma_period", raw["circuit_breaker"]["breadth_ma_period"]),
    )
