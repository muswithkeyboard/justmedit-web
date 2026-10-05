from __future__ import annotations

import contextlib

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold

from ..constants import ALLOW, CBC_IDX, GROUP_NAMES, GROUPS, LAB_GROUP, LAB_IDX, LABS, NP_
from .features import LEVEL_NAMES, apply_level, who_anemia

NG = len(GROUP_NAMES)
W_GRID = [1.0, 0.7, 0.5, 0.35, 0.25, 0.0]
T_GRID = [1.0, 0.7, 0.5, 0.35, 0.25]
N_BINS = 5
ALPHA = 1.0
PRIOR_GBM_PARAMS = dict(learning_rate=0.08, max_iter=120, max_leaf_nodes=8, min_samples_leaf=10,
                        l2_regularization=1.0, early_stopping=False)

def make_hgb(seed: int, min_samples_leaf: int = 10) -> HistGradientBoostingClassifier:
    params = dict(PRIOR_GBM_PARAMS, min_samples_leaf=min_samples_leaf)
    return HistGradientBoostingClassifier(random_state=seed, **params)

_OMP_CONTROLLER = None

def single_thread():
    global _OMP_CONTROLLER
    try:
        if _OMP_CONTROLLER is None:
            from threadpoolctl import ThreadpoolController
            _OMP_CONTROLLER = ThreadpoolController()
        return _OMP_CONTROLLER.limit(limits=1, user_api="openmp")
    except Exception:
        return contextlib.nullcontext()

def proba14(model, Xf) -> np.ndarray:
    P = np.zeros((len(Xf), NP_))
    with single_thread():
        P[:, model.classes_] = model.predict_proba(Xf)
    return P

def fit_tables(X, y, n_bins: int = N_BINS, alpha: float = ALPHA) -> list[tuple[np.ndarray, np.ndarray]]:
    qs = np.linspace(0, 1, n_bins + 1)[1:-1]
    tabs = []
    for j in LAB_IDX:
        x = X[:, j]
        m = ~np.isnan(x)
        edges = np.unique(np.quantile(x[m], qs)) if m.sum() >= n_bins else np.array([])
        nb = len(edges) + 1
        cnt = np.zeros((NP_, nb))
        np.add.at(cnt, (y[m], np.searchsorted(edges, x[m], side="right")), 1)
        tabs.append((edges, np.log((cnt + alpha) / (cnt.sum(axis=1, keepdims=True) + alpha * nb))))
    return tabs

def group_loglik(tabs, X) -> np.ndarray:
    LL = np.zeros((len(X), NG, NP_))
    for k, j in enumerate(LAB_IDX):
        edges, logp = tabs[k]
        x = X[:, j]
        m = ~np.isnan(x)
        if m.any():
            LL[m, LAB_GROUP[k], :] += logp[:, np.searchsorted(edges, x[m], side="right")].T
    return LL

def posterior(prior, LL, t, w, anemia) -> np.ndarray:
    logp = t * np.log(np.clip(prior, 1e-12, 1.0)) + np.tensordot(LL, np.asarray(w, dtype=float), axes=([1], [0]))
    logp = np.where(ALLOW[np.asarray(anemia, dtype=int)], logp, -np.inf)
    logp -= logp.max(axis=1, keepdims=True)
    P = np.exp(logp)
    return P / P.sum(axis=1, keepdims=True)

def fit_fusion(X, y, seed: int = 0, n_bins: int = N_BINS, alpha: float = ALPHA, inner_folds: int = 5,
               passes: int = 2, keep_oof: bool = False) -> dict:
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    n = len(X)
    anemia = who_anemia(X)

    prior_oof = np.zeros((n, NP_))
    LL_oof = np.zeros((n, NG, NP_))
    LL_levels = {lv: np.zeros((n, NG, NP_)) for lv in LEVEL_NAMES if lv != "extended"} if keep_oof else {}
    splits = list(StratifiedKFold(inner_folds, shuffle=True, random_state=seed).split(X, y))
    for tr, va in splits:
        m = make_hgb(seed).fit(X[tr][:, CBC_IDX], y[tr])
        prior_oof[va] = proba14(m, X[va][:, CBC_IDX])
        tabs = fit_tables(X[tr], y[tr], n_bins, alpha)
        LL_oof[va] = group_loglik(tabs, X[va])
        for lv in LL_levels:
            LL_levels[lv][va] = group_loglik(tabs, apply_level(X[va], lv))

    def loss(params) -> float:
        P = posterior(prior_oof, LL_oof, params[0], params[1:], anemia)
        return float(-np.mean(np.log(np.clip(P[np.arange(n), y], 1e-12, 1.0))))

    def nearest(v: float) -> float:
        return min(W_GRID, key=lambda g: abs(g - v))

    params = np.array([1.0] + [nearest(1 / np.sqrt(len(GROUPS[g]))) for g in GROUP_NAMES])
    best = loss(params)
    for _ in range(passes):
        for i in range(len(params)):
            for v in (T_GRID if i == 0 else W_GRID):
                trial = params.copy()
                trial[i] = v
                cur = loss(trial)
                if cur < best - 1e-9:
                    best, params = cur, trial

    model = {
        "prior_model": make_hgb(seed).fit(X[:, CBC_IDX], y),
        "tables": fit_tables(X, y, n_bins, alpha),
        "t": float(params[0]), "w": [float(v) for v in params[1:]],
        "n_bins": int(n_bins), "alpha": float(alpha), "seed": int(seed),
        "oof_logloss": float(best),
        "oof_logloss_prior_only": float(-np.mean(np.log(np.clip(prior_oof[np.arange(n), y], 1e-12, 1)))),
    }
    if keep_oof:
        LL_levels["extended"] = LL_oof
        model["oof"] = {
            "splits": splits,
            "posterior": {lv: posterior(prior_oof, LL_levels[lv], model["t"], model["w"], anemia)
                          for lv in LEVEL_NAMES},
        }
    return model

def prior_proba(model: dict, X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    return proba14(model["prior_model"], X[:, CBC_IDX])

def predict_fusion(model: dict, X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    return posterior(prior_proba(model, X), group_loglik(model["tables"], X), model["t"], model["w"], who_anemia(X))

def contributions(model: dict, x_row, top_profile: int, runner_profile: int) -> list[dict]:
    x = np.asarray(x_row, dtype=float).ravel()
    out = []
    for k, j in enumerate(LAB_IDX):
        if np.isnan(x[j]):
            continue
        edges, logp = model["tables"][k]
        b = int(np.searchsorted(edges, x[j], side="right"))
        delta = model["w"][LAB_GROUP[k]] * (logp[top_profile, b] - logp[runner_profile, b])
        out.append({"analyte": LABS[k], "delta": float(delta)})
    out.sort(key=lambda d: -abs(d["delta"]))
    return out

def entropy_bits(p, axis: int = -1) -> np.ndarray:
    p = np.asarray(p, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return -np.sum(np.where(p > 0, p * np.log2(p), 0.0), axis=axis)

def information_gain(model: dict, x_row) -> dict[str, float]:
    x = np.asarray(x_row, dtype=float).reshape(1, -1)
    p = predict_fusion(model, x)[0]
    h0 = float(entropy_bits(p))
    out: dict[str, float] = {}
    for k, j in enumerate(LAB_IDX):
        if not np.isnan(x[0, j]):
            continue
        _edges, logp = model["tables"][k]
        w = model["w"][LAB_GROUP[k]]
        if w == 0:
            out[LABS[k]] = 0.0
            continue
        p_bin = p @ np.exp(logp)
        post = p[:, None] * np.exp(w * logp)
        z = post.sum(axis=0, keepdims=True)
        post = post / np.where(z > 0, z, 1.0)
        gain = h0 - float(p_bin @ entropy_bits(post, axis=0))
        out[LABS[k]] = gain if gain > 1e-12 else 0.0
    return out
