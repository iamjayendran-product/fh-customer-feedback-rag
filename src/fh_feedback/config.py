"""Load config.yaml once and expose it as a plain dict, plus small path helpers."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load_config()


def window_start() -> dt.date:
    """First date in scope, based on config.yaml's window.months, anchored to today."""
    months = CFG["window"]["months"]
    today = dt.date.today()
    year = today.year
    month = today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, 28)  # avoid month-length overflow
    return dt.date(year, month, day)


def raw_dir(source: str, run_date: str | None = None) -> Path:
    run_date = run_date or dt.date.today().isoformat()
    p = ROOT / CFG["paths"]["raw_dir"] / source / run_date
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    p = ROOT / CFG["paths"]["db_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def path_for(key: str) -> Path:
    p = ROOT / CFG["paths"][key]
    p.mkdir(parents=True, exist_ok=True)
    return p
