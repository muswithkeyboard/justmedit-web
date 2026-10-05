from __future__ import annotations

import hashlib
import json
import platform
import warnings
from pathlib import Path

import joblib
import numpy as np

from .. import constants as C
from ..config import models_dir
from . import ensemble, fusion
from .features import LEVEL_NAMES, N_FEATS, who_anemia

VERSION = "case-v1"
PRIOR_FILE = "prior_gbm.joblib"
BENCH_FILE = "bench_gbm.joblib"
FUSION_FILE = "fusion.json"
METADATA_FILE = "metadata.json"
GOLDEN_FILE = "golden_predictions.json"
MODEL_FILES = (PRIOR_FILE, BENCH_FILE, FUSION_FILE)
MODES = ("clinical", "benchmark")
FUSION_FORMAT = 1

def decode_profiles(P14) -> dict[str, np.ndarray]:
    P14 = np.asarray(P14, dtype=float)
    P12 = P14 @ C.A12
    cls = P12.argmax(axis=1)
    in_class = C.A12[:, cls].T
    top_profile = np.where(in_class > 0, P14, -1.0).argmax(axis=1)
    cause = C.A11[top_profile].argmax(axis=1)
    return {"class12": cls, "cause11": cause, "top_profile": top_profile,
            "confidence": P12[np.arange(len(P12)), cls], "P12": P12}

def p_any_deficiency(P14) -> np.ndarray:
    return 1.0 - np.asarray(P14, dtype=float)[:, C.P_IDX["no_anemia_no_deficiency"]]

def sha256_file(path: str | Path) -> str:
    data = Path(path).read_bytes()
    if str(path).endswith(".json"):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()

def library_versions() -> dict[str, str]:
    import pandas
    import sklearn
    return {"python": platform.python_version(), "scikit-learn": sklearn.__version__, "numpy": np.__version__,
            "pandas": pandas.__version__, "joblib": joblib.__version__}

def fusion_to_json(fus: dict) -> dict:
    return {
        "format": FUSION_FORMAT,
        "note": "Границы бинов выучены по данным набора кейса; это не пороги ВОЗ и не пороги показа.",
        "profiles": C.PROFILES, "labs": C.LABS, "groups": C.GROUP_NAMES, "lab_group": C.LAB_GROUP,
        "n_bins": fus.get("n_bins", fusion.N_BINS), "alpha": fus.get("alpha", fusion.ALPHA),
        "prior_power_t": fus["t"],
        "group_weights": dict(zip(C.GROUP_NAMES, fus["w"])),
        "tables": {a: {"edges": np.asarray(e).tolist(), "log_p": np.asarray(lp).tolist()}
                   for a, (e, lp) in zip(C.LABS, fus["tables"])},
    }

def fusion_from_json(data: dict, prior_model) -> dict:
    if data.get("format") != FUSION_FORMAT:
        raise ValueError(f"fusion.json: неизвестный формат {data.get('format')!r}")
    if data["profiles"] != C.PROFILES or data["labs"] != C.LABS or data["groups"] != C.GROUP_NAMES:
        raise ValueError("fusion.json собран под другой список профилей или анализов — переобучите модель")
    tables = []
    for a in C.LABS:
        tab = data["tables"][a]
        logp = np.asarray(tab["log_p"], dtype=float)
        edges = np.asarray(tab["edges"], dtype=float)
        if logp.shape != (C.NP_, len(edges) + 1):
            raise ValueError(f"fusion.json: таблица анализа {a} имеет неверный размер")
        tables.append((edges, logp))
    return {"prior_model": prior_model, "tables": tables, "t": float(data["prior_power_t"]),
            "w": [float(data["group_weights"][g]) for g in C.GROUP_NAMES],
            "n_bins": data["n_bins"], "alpha": data["alpha"]}

def write_model_files(model_dir: str | Path, fus: dict, bench_gbm) -> dict[str, dict]:
    d = Path(model_dir)
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump(fus["prior_model"], d / PRIOR_FILE, compress=3)
    joblib.dump(bench_gbm, d / BENCH_FILE, compress=3)
    (d / FUSION_FILE).write_text(json.dumps(fusion_to_json(fus), ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
    return {name: file_info(d / name) for name in MODEL_FILES}

def file_info(path: str | Path) -> dict:
    return {"sha256": sha256_file(path), "bytes": Path(path).stat().st_size}

def write_json(path: str | Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=_json_default) + "\n",
                          encoding="utf-8")

def _json_default(o):
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)

class CaseModel:

    version: str = VERSION

    def __init__(self, fus: dict, bench_gbm, metadata: dict | None = None):
        self._fusion = fus
        self._bench = bench_gbm
        self.metadata: dict = dict(metadata or {})
        self.version = str(self.metadata.get("version", VERSION))

    @classmethod
    def load(cls, model_dir: str | Path | None = None) -> "CaseModel":
        d = Path(model_dir) if model_dir is not None else models_dir() / VERSION
        meta_path = d / METADATA_FILE
        if not meta_path.exists():
            raise FileNotFoundError(f"нет {meta_path}: обучите модель командой `deficitlens train --what case`")
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        for name in MODEL_FILES:
            if not (d / name).exists():
                raise FileNotFoundError(f"нет файла модели {d / name}")
            expected = metadata.get("files", {}).get(name, {}).get("sha256")
            if expected and sha256_file(d / name) != expected:
                raise ValueError(f"контрольная сумма {name} не совпадает с metadata.json — файл модели повреждён "
                                 "или заменён; переобучите модель")
        import sklearn
        trained_with = metadata.get("versions", {}).get("scikit-learn")
        if trained_with and trained_with != sklearn.__version__:
            warnings.warn(f"модель {VERSION} обучена на scikit-learn {trained_with}, установлена "
                          f"{sklearn.__version__}: предсказания могут отличаться", stacklevel=2)
        prior = joblib.load(d / PRIOR_FILE)
        bench = joblib.load(d / BENCH_FILE)
        fus = fusion_from_json(json.loads((d / FUSION_FILE).read_text(encoding="utf-8")), prior)
        return cls(fus, bench, metadata)

    @staticmethod
    def _matrix(X) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if X.ndim != 2 or X.shape[1] != N_FEATS:
            raise ValueError(f"ожидается матрица (n, {N_FEATS}) в порядке constants.FEATS, получено {X.shape}")
        return X

    def predict_profiles(self, X: np.ndarray, mode: str = "clinical") -> np.ndarray:
        if mode not in MODES:
            raise ValueError(f"mode должен быть 'clinical' или 'benchmark', получено {mode!r}")
        X = self._matrix(X)
        if len(X) == 0:
            return np.zeros((0, C.NP_))
        P_clin = fusion.predict_fusion(self._fusion, X)
        if mode == "clinical":
            return P_clin
        return ensemble.predict_ensemble({"gbm": self._bench, "fusion": self._fusion}, X, P_fusion=P_clin)

    def predict_both(self, X: np.ndarray) -> dict[str, np.ndarray]:
        X = self._matrix(X)
        if len(X) == 0:
            return {m: np.zeros((0, C.NP_)) for m in MODES}
        P_clin = fusion.predict_fusion(self._fusion, X)
        P_bench = ensemble.predict_ensemble({"gbm": self._bench, "fusion": self._fusion}, X, P_fusion=P_clin)
        return {"clinical": P_clin, "benchmark": P_bench}

    def prior(self, X: np.ndarray) -> np.ndarray:
        return fusion.prior_proba(self._fusion, self._matrix(X))

    @property
    def fusion_params(self) -> dict:
        return {"prior_power_t": self._fusion["t"], "group_weights": dict(zip(C.GROUP_NAMES, self._fusion["w"]))}

    def contributions(self, x_row: np.ndarray, top_profile: int, runner_profile: int) -> list[dict]:
        return fusion.contributions(self._fusion, self._matrix(x_row)[0], int(top_profile), int(runner_profile))

    def information_gain(self, x_row: np.ndarray) -> dict[str, float]:
        return fusion.information_gain(self._fusion, self._matrix(x_row)[0])

    def hidden_threshold(self, level: str, mode: str = "clinical") -> float:
        if level not in LEVEL_NAMES:
            raise ValueError(f"неизвестный уровень полноты {level!r}; допустимо: {LEVEL_NAMES}")
        key = "hidden_tau_benchmark" if mode == "benchmark" and "hidden_tau_benchmark" in self.metadata else "hidden_tau"
        return float(self.metadata[key][level])

    def anemia(self, X: np.ndarray) -> np.ndarray:
        return who_anemia(self._matrix(X))
