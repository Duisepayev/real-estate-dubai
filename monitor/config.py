import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path=None):
    path = Path(path or os.environ.get("MONITOR_CONFIG", ROOT / "config.yaml"))
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    override = os.environ.get("MONITOR_CONFIG_JSON", "").strip()
    if override:
        _merge(cfg, json.loads(override))
    return cfg


def data_dir():
    d = Path(os.environ.get("MONITOR_DATA_DIR", ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir():
    d = Path(os.environ.get("MONITOR_STATE_DIR", ROOT / "state"))
    d.mkdir(parents=True, exist_ok=True)
    return d
