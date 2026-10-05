from __future__ import annotations

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from .features import feats_plus, who_anemia, who_lock
from .fusion import predict_fusion, proba14

BENCH_GBM_PARAMS = dict(learning_rate=0.05, max_iter=300, max_leaf_nodes=8, min_samples_leaf=8,
                        l2_regularization=1.0, early_stopping=False)

def make_bench_gbm(seed: int) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(random_state=seed, **BENCH_GBM_PARAMS)

def geo(*Ps, w=None) -> np.ndarray:
    w = w or [1 / len(Ps)] * len(Ps)
    L = sum(wi * np.log(np.clip(P, 1e-9, 1)) for wi, P in zip(w, Ps))
    L = L - L.max(axis=1, keepdims=True)
    P = np.exp(L)
    return P / P.sum(axis=1, keepdims=True)

def fit_bench_gbm(X, y, seed: int = 0) -> HistGradientBoostingClassifier:
    return make_bench_gbm(seed).fit(feats_plus(np.asarray(X, dtype=float)), np.asarray(y, dtype=int))

def gbm_proba_locked(gbm, X) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    return who_lock(proba14(gbm, feats_plus(X)), who_anemia(X))

def combine(P_gbm_locked, P_fusion, anemia) -> np.ndarray:
    return who_lock(geo(P_gbm_locked, P_fusion), anemia)

def predict_ensemble(model: dict, X, P_fusion=None) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    if P_fusion is None:
        P_fusion = predict_fusion(model["fusion"], X)
    return combine(gbm_proba_locked(model["gbm"], X), P_fusion, who_anemia(X))
