"""规则库与签署参数装载。"""

from __future__ import annotations

from pathlib import Path

import yaml

from .engine import RuleRepository
from .params import Direction, ParamDef, ParamStore

LIBRARY_DIR = Path(__file__).parent / "library"


def load_rules(library_dir: Path | None = None) -> RuleRepository:
    return RuleRepository(library_dir or LIBRARY_DIR)


def load_params(library_dir: Path | None = None) -> ParamStore:
    doc = yaml.safe_load(((library_dir or LIBRARY_DIR) / "params_signed.yaml").read_text(encoding="utf-8"))
    defs: dict[str, ParamDef] = {}
    signed: dict[str, float] = {}
    for p in doc["params"]:
        defs[p["name"]] = ParamDef(
            name=p["name"],
            default=float(p["default"]),
            direction=Direction(p["direction"]),
            source=p["source"],
            unit=p.get("unit", ""),
        )
        if "signed" in p:
            signed[p["name"]] = float(p["signed"])
    return ParamStore(defs=defs, signed=signed)
