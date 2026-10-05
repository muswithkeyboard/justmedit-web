from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from ..config import config_dir, models_dir
from ..constants import CBC_LABS

VERSION = "screen-v1"

TARGETS: dict[str, float] = {"t15": 15.0, "t30": 30.0}
WHO_HB_G_L: dict[str, float] = {"F": 120.0, "M": 130.0}
ADULT_AGE = 18
GROUPS: tuple[str, ...] = ("F18-49", "F50+", "M")

FEATURE_LABS: dict[str, list[str]] = {"full_cbc": list(CBC_LABS), "hb_only": ["hemoglobin"]}
DEMOGRAPHY = ["age_years", "female"]

TEST_CYCLES: tuple[str, ...] = ("2017-2018", "2021-2023")
THRESHOLD_SHARES: tuple[float, ...] = (0.10, 0.20, 0.30, 0.40, 0.50)
HIGH_SHARE = 0.10

HGB_PARAMS: dict[str, Any] = dict(max_iter=250, learning_rate=0.05, max_leaf_nodes=15, l2_regularization=1.0,
                                  min_samples_leaf=40, random_state=0)
CALIBRATION: dict[str, Any] = dict(method="isotonic", cv=5)

NHANES_COLUMNS = ["age_years", "sex", "pregnant", "nhanes_cycle", "mec_weight", *CBC_LABS, "ferritin_harmonized"]
DEFAULT_REFERENCE = "default"
LAB_REFERENCE_FILE = "lab_reference.yaml"
MIN_ROWS_WARN = 200
MIN_ROWS_SEX = 20

def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def model_file_name(target: str, features: str) -> str:
    return f"{target}_{features}.joblib"

def feature_names(features: str, scaling: str = "z") -> list[str]:
    suffix = "_z" if scaling == "z" else ""
    return [c + suffix for c in FEATURE_LABS[features]] + DEMOGRAPHY

def group_of(sex: str, age_years: float) -> str:
    if sex == "M":
        return "M"
    return "F18-49" if float(age_years) < 50 else "F50+"

def who_anemia(hemoglobin, female) -> np.ndarray:
    hb = np.asarray(hemoglobin, dtype=float)
    fem = np.asarray(female).astype(bool)
    return np.where(fem, hb < WHO_HB_G_L["F"], hb < WHO_HB_G_L["M"]).astype(int)

def wmean(x, w) -> float:
    return float((np.asarray(x, float) * w).sum() / w.sum())

def load_cohort(nhanes_path: str | Path):
    import pandas as pd

    n = pd.read_csv(nhanes_path, usecols=NHANES_COLUMNS, low_memory=False)
    adults = n[(n.age_years >= ADULT_AGE) & (n.pregnant == 0)].dropna(subset=CBC_LABS).copy()
    adults["female"] = (adults.sex == "F").astype(int)
    adults["anemia_who"] = who_anemia(adults.hemoglobin, adults.female)
    g = adults.groupby(["nhanes_cycle", "sex"])
    for c in CBC_LABS:
        med = g[c].transform("median")
        iqr = g[c].transform(lambda s: s.quantile(.75) - s.quantile(.25))
        adults[c + "_z"] = (adults[c] - med) / iqr
    adults["is_test"] = adults.nhanes_cycle.isin(TEST_CYCLES)
    adults["w"] = adults.mec_weight
    adults["grp"] = np.select([(adults.female == 1) & (adults.age_years < 50),
                               (adults.female == 1) & (adults.age_years >= 50)], ["F18-49", "F50+"], "M")

    cohort = adults[adults.ferritin_harmonized.notna()].copy()
    for tgt, cut in TARGETS.items():
        cohort[tgt] = (cohort.ferritin_harmonized < cut).astype(int)

    no_anemia = cohort.anemia_who == 0
    counts = {
        "rows_total": int(len(n)),
        "adults_18plus": int((n.age_years >= ADULT_AGE).sum()),
        "children": int((n.age_years < ADULT_AGE).sum()),
        "adults_nonpregnant_full_cbc": int(len(adults)),
        "with_ferritin": int(len(cohort)),
        "with_ferritin_train_1999_2016": int((~cohort.is_test).sum()),
        "with_ferritin_test_2017_2023": int(cohort.is_test.sum()),
        "no_anemia": int(no_anemia.sum()),
        "no_anemia_train_1999_2016": int((no_anemia & ~cohort.is_test).sum()),
        "no_anemia_test_2017_2023": int((no_anemia & cohort.is_test).sum()),
    }
    return adults, cohort, counts

def feature_matrix(df, features: str, scaling: str = "z") -> np.ndarray:
    return df[feature_names(features, scaling)].to_numpy(dtype=float)

def fit_model(X: np.ndarray, y: np.ndarray):
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier

    base = HistGradientBoostingClassifier(**HGB_PARAMS)
    return CalibratedClassifierCV(base, **CALIBRATION).fit(X, y)

def referral_table(p, y, w, fracs: Iterable[float], price_rub: float) -> list[dict]:
    p, y, w = np.asarray(p, float), np.asarray(y), np.asarray(w, float)
    o = np.argsort(-p)
    cw = np.cumsum(w[o]) / w.sum()
    out = []
    for f in fracs:
        k = int(np.searchsorted(cw, f)) + 1
        s = o[:k]
        tp = (w[s] * y[s]).sum()
        ref = w[s].sum()
        out.append(dict(
            refer_share=float(f),
            referred_per1000=int(round(1000 * ref / w.sum())),
            cases_per1000=int(round(1000 * (w * y).sum() / w.sum())),
            found_per1000=int(round(1000 * tp / w.sum())),
            sensitivity=round(float(tp / (w * y).sum()), 3),
            ppv=round(float(tp / ref), 3),
            referred_per_case_found=round(float(ref / tp), 2),
            rub_per_case_found=int(round(price_rub * float(ref / tp))),
        ))
    return out

def top_share_threshold(p, w, share: float) -> tuple[float, float]:
    p, w = np.asarray(p, float), np.asarray(w, float)
    values, inverse = np.unique(p, return_inverse=True)
    weight = np.bincount(inverse, weights=w, minlength=len(values))
    at_or_above = np.cumsum(weight[::-1])[::-1] / w.sum()
    dist = np.abs(at_or_above - share)
    j = len(values) - 1 - int(np.argmin(dist[::-1]))
    return float(values[j]), float(at_or_above[j])

_REF_CACHE: dict[str, tuple[int, dict]] = {}

def lab_reference_path(config_path: str | Path | None = None) -> Path:
    return Path(config_path) if config_path else config_dir() / LAB_REFERENCE_FILE

def load_lab_references(config_path: str | Path | None = None) -> dict:
    import yaml

    path = lab_reference_path(config_path)
    mtime = path.stat().st_mtime_ns
    cached = _REF_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    _REF_CACHE[str(path)] = (mtime, doc)
    return doc

def reference_by_sex(reference: Mapping | None) -> Mapping:
    if not isinstance(reference, Mapping):
        return {}
    if isinstance(reference.get("by_sex"), Mapping):
        return reference["by_sex"]
    return reference if ("F" in reference or "M" in reference) else {}

def _reference_ok(by_sex: Mapping, sex: str, labs: list[str]) -> bool:
    row = by_sex.get(sex)
    if not isinstance(row, Mapping):
        return False
    for c in labs:
        r = row.get(c)
        try:
            q25, med, q75 = float(r["q25"]), float(r["median"]), float(r["q75"])
        except (TypeError, KeyError, ValueError):
            return False
        if not (math.isfinite(q25) and math.isfinite(med) and math.isfinite(q75) and q75 > q25):
            return False
    return True

def standardize(cbc: Mapping[str, float], sex: str, by_sex: Mapping, labs: list[str]) -> list[float]:
    row = by_sex[sex]
    return [(float(cbc[c]) - float(row[c]["median"])) / (float(row[c]["q75"]) - float(row[c]["q25"])) for c in labs]

def _number(v) -> float | None:
    if isinstance(v, Mapping):
        v = v.get("value")
    elif hasattr(v, "value") and not isinstance(v, (int, float, np.generic)):
        v = v.value
    if v is None or isinstance(v, (str, bytes, bool)):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None

class _FastForest:

    def __init__(self, by_target: Mapping[str, Any]):
        feat, thr, left, right, value, roots = [], [], [], [], [], []
        self.segments: list[tuple] = []
        offset, depth = 0, 0
        for tgt, model in by_target.items():
            for cc in model.calibrated_classifiers_:
                est = cc.estimator
                (iso,) = cc.calibrators
                if cc.method != "isotonic" or iso.out_of_bounds != "clip":
                    raise ValueError("неожиданный калибратор")
                start = len(roots)
                for iteration in est._predictors:
                    (tree,) = iteration
                    nodes = tree.nodes
                    if nodes["is_categorical"].any():
                        raise ValueError("категориальные признаки не поддерживаются")
                    n = len(nodes)
                    leaf = nodes["is_leaf"].astype(bool)
                    own = np.arange(n, dtype=np.intp) + offset
                    feat.append(np.where(leaf, 0, nodes["feature_idx"]).astype(np.intp))
                    thr.append(np.where(leaf, np.inf, nodes["num_threshold"]).astype(float))
                    left.append(np.where(leaf, own, nodes["left"].astype(np.intp) + offset))
                    right.append(np.where(leaf, own, nodes["right"].astype(np.intp) + offset))
                    value.append(nodes["value"].astype(float))
                    roots.append(offset)
                    depth = max(depth, int(nodes["depth"].max()))
                    offset += n
                self.segments.append((tgt, start, len(roots), float(np.ravel(est._baseline_prediction)[0]),
                                      np.asarray(iso.X_thresholds_, float), np.asarray(iso.y_thresholds_, float)))
        self.feat, self.thr, self.left, self.right, self.value = map(np.concatenate, (feat, thr, left, right, value))
        self.roots = np.array(roots, dtype=np.intp)
        self.depth = depth
        self.n_folds = {t: sum(1 for seg in self.segments if seg[0] == t) for t in by_target}

    def predict(self, x: np.ndarray) -> dict[str, float]:
        idx = self.roots
        feat, thr, left, right = self.feat, self.thr, self.left, self.right
        for _ in range(self.depth):
            idx = np.where(x[feat[idx]] <= thr[idx], left[idx], right[idx])
        leaves = self.value[idx]
        acc = dict.fromkeys(self.n_folds, 0.0)
        for tgt, a, b, base, iso_x, iso_y in self.segments:
            buf = np.empty(b - a + 1)
            buf[0] = base
            buf[1:] = leaves[a:b]
            acc[tgt] += float(np.interp(np.cumsum(buf)[-1], iso_x, iso_y))
        return {t: acc[t] / self.n_folds[t] for t in acc}

class ScreenModel:

    version = VERSION

    def __init__(self, models: dict, metadata: dict, model_dir: str | Path | None = None):
        self._models = models
        self.metadata = metadata
        self.model_dir = Path(model_dir) if model_dir else None
        self.version = metadata.get("version", VERSION)
        self._fast: dict[str, _FastForest] = {}
        for features in FEATURE_LABS:
            try:
                forest = _FastForest({tgt: models[(tgt, features)] for tgt in TARGETS})
                if self._self_check(forest, features):
                    self._fast[features] = forest
            except Exception:
                pass
        self.fast_path = len(self._fast) == len(FEATURE_LABS)

    def _self_check(self, forest: "_FastForest", features: str, n: int = 64, tol: float = 1e-10) -> bool:
        rng = np.random.default_rng(0)
        k = len(FEATURE_LABS[features])
        X = np.column_stack([rng.normal(0.0, 1.5, size=(n, k)), rng.integers(18, 91, n),
                             rng.integers(0, 2, n)]).astype(float)
        ref = self.predict_raw(X, features)
        got = [forest.predict(x) for x in X]
        return all(float(np.max(np.abs(np.array([g[tgt] for g in got]) - ref[i]))) <= tol
                   for i, tgt in enumerate(TARGETS))

    @classmethod
    def load(cls, model_dir: str | Path | None = None) -> "ScreenModel":
        import joblib

        d = Path(model_dir) if model_dir else models_dir() / VERSION
        meta_path = d / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Модель скрининга не найдена: {meta_path}. "
                                    "Обучите её: deficitlens train --what screen")
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        files = metadata.get("files", {})
        models = {}
        for tgt in TARGETS:
            for features in FEATURE_LABS:
                name = model_file_name(tgt, features)
                path = d / name
                expected = files.get(name, {}).get("sha256")
                if expected and sha256_file(path) != expected:
                    raise RuntimeError(f"Файл модели {name} не совпадает с metadata.json (SHA-256): "
                                       "переобучите модель.")
                models[(tgt, features)] = joblib.load(path)
        return cls(models, metadata, d)

    def _reference(self, reference, sex: str, labs: list[str]) -> tuple[Mapping, str | None]:
        default = None
        if reference is None or isinstance(reference, str):
            refs = load_lab_references().get("references", {})
            default = refs.get(DEFAULT_REFERENCE)
            if reference is None or reference == DEFAULT_REFERENCE:
                reference = default
            else:
                if reference not in refs:
                    raise ValueError(f"Справочника лаборатории «{reference}» нет в config/{LAB_REFERENCE_FILE}.")
                reference = refs[reference]
        by_sex = reference_by_sex(reference)
        if _reference_ok(by_sex, sex, labs):
            return by_sex, None
        if default is None:
            default = load_lab_references().get("references", {}).get(DEFAULT_REFERENCE)
        dflt = reference_by_sex(default)
        if not _reference_ok(dflt, sex, labs):
            raise ValueError(f"В справочнике лаборатории нет q25 / median / q75 для пола {sex}: {', '.join(labs)}.")
        return dflt, DEFAULT_REFERENCE

    def threshold(self, features: str, group: str, share: float) -> float:
        rows = self.metadata["thresholds"]["by_features"][features][group]
        shares = np.array([r["share"] for r in rows], dtype=float)
        values = np.array([r["threshold"] for r in rows], dtype=float)
        o = np.argsort(shares)
        return float(np.interp(float(share), shares[o], values[o]))

    def threshold_shares(self) -> list[float]:
        return [float(s) for s in self.metadata["thresholds"]["shares"]]

    def predict_raw(self, X: np.ndarray, features: str) -> tuple[np.ndarray, np.ndarray]:
        X = np.asarray(X, dtype=float)
        p15 = self._models[("t15", features)].predict_proba(X)[:, 1]
        p30 = self._models[("t30", features)].predict_proba(X)[:, 1]
        return p15, p30

    def predict(self, values: Mapping[str, Any], sex: str, age_years: int, reference: Any = None,
                refer_share: float = 0.30, high_share: float = HIGH_SHARE) -> dict:
        sex = str(sex).strip().upper()
        if sex not in WHO_HB_G_L:
            raise ValueError("Пол должен быть 'F' или 'M'.")
        age = _number(age_years)
        if age is None:
            raise ValueError("Не указан возраст (age_years).")
        cbc = {c: x for c in CBC_LABS if (x := _number(values.get(c))) is not None}
        if len(cbc) == len(CBC_LABS):
            features = "full_cbc"
        elif "hemoglobin" in cbc:
            features = "hb_only"
        else:
            raise ValueError("Для скрининга нужен гемоглобин (hemoglobin, г/л).")
        labs = FEATURE_LABS[features]
        by_sex, fallback = self._reference(reference, sex, labs)
        x = np.array([standardize(cbc, sex, by_sex, labs) + [age, 1.0 if sex == "F" else 0.0]], dtype=float)
        forest = self._fast.get(features)
        if forest is not None:
            fast = forest.predict(x[0])
            p15, p30_model = fast["t15"], fast["t30"]
        else:
            raw15, raw30 = self.predict_raw(x, features)
            p15, p30_model = float(raw15[0]), float(raw30[0])
        p30 = max(p30_model, p15)
        group = group_of(sex, age)
        shares = self.threshold_shares()
        share = min(max(float(refer_share), min(shares)), max(shares))
        high = min(max(float(high_share), min(shares)), share)
        out = {
            "p_lt15": p15,
            "p_lt30": p30,
            "features": features,
            "group": group,
            "thresholds": {"refer": self.threshold(features, group, share),
                           "high": self.threshold(features, group, high)},
            "refer_share": share,
        }
        if p30 != p30_model:
            out["p_lt30_model"] = p30_model
        if fallback:
            out["reference_fallback"] = fallback
        return out

_SEX_MAP = {"f": "F", "female": "F", "w": "F", "ж": "F", "жен": "F", "женский": "F", "женщина": "F",
            "m": "M", "male": "M", "м": "M", "муж": "M", "мужской": "M", "мужчина": "M"}
_SEX_COLUMNS = ("sex", "gender", "пол")
_AGE_COLUMNS = ("age_years", "age", "возраст")
_PREGNANT_COLUMNS = ("pregnant",)

def _column_aliases() -> dict[str, str]:
    aliases = {c.lower(): c for c in CBC_LABS}
    try:
        import yaml

        with open(config_dir() / "analytes.yaml", encoding="utf-8") as f:
            analytes = (yaml.safe_load(f) or {}).get("analytes", {})
        for c in CBC_LABS:
            for a in analytes.get(c, {}).get("aliases", []) or []:
                aliases.setdefault(str(a).strip().lower(), c)
    except Exception:
        pass
    return aliases

def _read_table(path: Path):
    import pandas as pd

    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return pd.read_excel(path)
    try:
        return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig")
    except Exception:
        return pd.read_csv(path, encoding="utf-8-sig")

def _to_float(series):
    import pandas as pd

    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    s = series.astype(str).str.replace(" ", "", regex=False).str.replace(" ", "", regex=False)
    return pd.to_numeric(s.str.replace(",", ".", regex=False), errors="coerce")

def build_lab_reference(csv_path: str, name: str, write: bool = True, config_path: str | Path | None = None) -> dict:
    import pandas as pd

    name = str(name).strip()
    if not re.fullmatch(r"[\w.\-]{1,64}", name):
        raise ValueError("Имя справочника: 1–64 символа — буквы, цифры, '_', '-', '.'.")
    if name == DEFAULT_REFERENCE:
        raise ValueError("Имя 'default' занято справочником NHANES 2017–2023: выберите другое имя.")

    src = Path(csv_path)
    df = _read_table(src)
    n_rows = int(len(df))
    lower = {str(c).strip().lower(): c for c in df.columns}
    aliases = _column_aliases()
    columns: dict[str, Any] = {}
    for low, original in lower.items():
        code = aliases.get(low)
        if code and code not in columns:
            columns[code] = original
    sex_col = next((lower[c] for c in _SEX_COLUMNS if c in lower), None)
    missing = [c for c in CBC_LABS if c not in columns] + ([] if sex_col is not None else ["sex"])
    if missing:
        raise ValueError("В выгрузке нет столбцов: " + ", ".join(missing))

    warnings_: list[str] = []
    data = pd.DataFrame({c: _to_float(df[columns[c]]) for c in CBC_LABS})
    data["sex"] = df[sex_col].astype(str).str.strip().str.lower().map(_SEX_MAP)
    keep = data.sex.notna()
    age_col = next((lower[c] for c in _AGE_COLUMNS if c in lower), None)
    if age_col is not None:
        keep &= ~(_to_float(df[age_col]) < ADULT_AGE)
    preg_col = next((lower[c] for c in _PREGNANT_COLUMNS if c in lower), None)
    if preg_col is not None:
        keep &= ~(_to_float(df[preg_col]) == 1)
    data = data[keep].dropna(subset=CBC_LABS)
    n_used = int(len(data))
    if n_used < n_rows:
        warnings_.append(f"Пропущено строк: {n_rows - n_used} из {n_rows} "
                         "(нет пола, пропуски в ОАК, возраст до 18 или беременность).")

    default = reference_by_sex(load_lab_references(config_path).get("references", {}).get(DEFAULT_REFERENCE))
    by_sex: dict[str, dict] = {}
    n_by_sex: dict[str, int] = {}
    for sex in ("F", "M"):
        part = data[data.sex == sex]
        n_by_sex[sex] = int(len(part))
        if len(part) < MIN_ROWS_SEX:
            warnings_.append(f"Пол {sex}: строк {len(part)} — меньше {MIN_ROWS_SEX}, квартили не посчитаны; "
                             f"для этого пола будет использован справочник '{DEFAULT_REFERENCE}'.")
            continue
        if len(part) < MIN_ROWS_WARN:
            warnings_.append(f"Пол {sex}: строк {len(part)} — меньше {MIN_ROWS_WARN}, квартили неустойчивы.")
        q = part[CBC_LABS].quantile([.25, .5, .75]).round(3)
        row = {c: {"q25": float(q.loc[.25, c]), "median": float(q.loc[.5, c]), "q75": float(q.loc[.75, c])}
               for c in CBC_LABS}
        flat = [c for c in CBC_LABS if not row[c]["q75"] > row[c]["q25"]]
        if flat:
            warnings_.append(f"Пол {sex}: у показателей {', '.join(flat)} q75 = q25 — стандартизация невозможна; "
                             f"для этого пола будет использован справочник '{DEFAULT_REFERENCE}'.")
            continue
        for c in CBC_LABS:
            ref_med = (default.get(sex) or {}).get(c, {}).get("median")
            if ref_med and not (ref_med / 1.5 <= row[c]["median"] <= ref_med * 1.5):
                warnings_.append(f"Пол {sex}, {c}: медиана {row[c]['median']:g} при ожидаемой около {ref_med:g} — "
                                 "проверьте единицы измерения.")
        by_sex[sex] = row
    if not by_sex:
        raise ValueError("Справочник не построен: недостаточно строк с полом и полным ОАК.")

    entry = {
        "source": f"выгрузка ОАК лаборатории: {src.name}, {datetime.now(timezone.utc).date().isoformat()}",
        "n": n_by_sex,
        "by_sex": by_sex,
    }
    result = dict(entry, name=name, n_rows=n_rows, n_used=n_used, n_by_sex=n_by_sex, warnings=warnings_,
                  written=False, path=None)
    if write:
        path = lab_reference_path(config_path)
        result["written"] = True
        result["path"] = str(path)
        if _write_reference(path, name, entry):
            warnings_.append(f"Справочник «{name}» уже был в файле и перезаписан.")
    return result

def _write_reference(path: Path, name: str, entry: dict) -> bool:
    import yaml

    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    refs = doc.setdefault("references", {})
    existed = name in refs
    refs[name] = entry
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _REF_CACHE.pop(str(path), None)
    try:
        from ..config import load_config

        load_config.cache_clear()
    except Exception:
        pass
    return existed
