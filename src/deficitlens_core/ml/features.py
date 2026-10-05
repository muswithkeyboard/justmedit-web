from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..constants import ALLOW, F_IDX, FEATS, LAB_IDX, LABS, LEVELS

N_FEATS = len(FEATS)
LEVEL_NAMES = list(LEVELS)
LEVEL_DROP_IDX = {lv: [F_IDX[a] for a in LABS if a not in keep] for lv, keep in LEVELS.items()}

WHO_HB_FEMALE = 120.0
WHO_HB_MALE = 130.0

_SEX_CODES = {"f": 0.0, "m": 1.0, "0": 0.0, "1": 1.0}

def _sex_to_num(v) -> float:
    if v is None:
        return math.nan
    if isinstance(v, str):
        return _SEX_CODES.get(v.strip().lower(), math.nan)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f if f in (0.0, 1.0) else math.nan

def _to_num(v) -> float:
    if v is None:
        return math.nan
    if isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return math.nan
        try:
            return float(s)
        except ValueError:
            return math.nan
    if isinstance(v, dict) and "value" in v:
        return _to_num(v["value"])
    value = getattr(v, "value", None)
    return _to_num(value) if value is not None else math.nan

def to_matrix(records) -> np.ndarray:
    if isinstance(records, pd.DataFrame):
        X = np.full((len(records), N_FEATS), np.nan)
        for j, f in enumerate(FEATS):
            if f not in records.columns:
                continue
            col = records[f]
            if f == "sex":
                X[:, j] = [_sex_to_num(v) for v in col.tolist()]
            elif pd.api.types.is_bool_dtype(col):
                continue
            elif pd.api.types.is_numeric_dtype(col):
                X[:, j] = col.to_numpy(dtype=float, na_value=np.nan)
            else:
                X[:, j] = [_to_num(v) for v in col.tolist()]
        return X
    if isinstance(records, dict):
        records = [records]
    records = list(records)
    X = np.full((len(records), N_FEATS), np.nan)
    for i, rec in enumerate(records):
        for key, v in rec.items():
            j = F_IDX.get(key)
            if j is not None:
                X[i, j] = _sex_to_num(v) if key == "sex" else _to_num(v)
    return X

def who_anemia(X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    hb = X[:, F_IDX["hemoglobin"]]
    male = X[:, F_IDX["sex"]] == 1
    with np.errstate(invalid="ignore"):
        return (hb < np.where(male, WHO_HB_MALE, WHO_HB_FEMALE)).astype(int)

def who_lock(P, anemia) -> np.ndarray:
    P = np.asarray(P, dtype=float)
    A = ALLOW[np.asarray(anemia, dtype=int)]
    Q = P * A
    s = Q.sum(axis=1, keepdims=True)
    fallback = A / A.sum(axis=1, keepdims=True)
    return np.where(s > 0, Q / np.where(s > 0, s, 1.0), fallback)

def feats_plus(X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    g = lambda name: X[:, F_IDX[name]]
    with np.errstate(all="ignore"):
        extra = np.column_stack([
            g("MCV") / g("RBC"),
            g("RDW") / g("MCV") * 100,
            g("sTfR") / np.log10(np.clip(g("ferritin"), 1e-3, None)),
            g("serum_iron") / g("TIBC") * 100,
            g("vitamin_B12") / np.clip(g("folate"), 1e-3, None),
            who_anemia(X),
        ])
    extra[~np.isfinite(extra)] = np.nan
    return np.column_stack([X, extra])

def apply_level(X, level: str) -> np.ndarray:
    X = np.array(X, dtype=float, copy=True)
    drop = LEVEL_DROP_IDX[level]
    if drop:
        X[:, drop] = np.nan
    return X

def completeness_many(X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    M = ~np.isnan(X[:, LAB_IDX])
    out = np.full(len(X), "extended", dtype=object)
    for lv in ("standard", "cbc_iron_crp", "cbc"):
        outside = np.array([a not in LEVELS[lv] for a in LABS])
        out[~(M & outside).any(axis=1)] = lv
    return out

def completeness(x_row) -> str:
    return str(completeness_many(np.asarray(x_row, dtype=float).reshape(1, -1))[0])
