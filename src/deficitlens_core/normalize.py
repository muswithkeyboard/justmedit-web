from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .schemas import AnalysisInput, InputError, LabValue, NormalizationNote

@dataclass
class Normalized:
    sex: str
    age_years: int
    pregnancy_status: str
    trimester: int | None
    values: dict[str, float]
    measured: set[str]
    notes: list[NormalizationNote] = field(default_factory=list)
    norms_set: str = "who"
    references: Any = None

    def is_derived(self, analyte: str) -> bool:
        return analyte in self.values and analyte not in self.measured

def fmt_num(x: float, digits: int | None = None) -> str:
    x = float(x)
    if not math.isfinite(x):
        return str(x)
    if digits is None:
        ax = abs(x)
        if ax == 0:
            digits = 0
        elif ax >= 10:
            digits = 1
        elif ax >= 1:
            digits = 2
        else:
            digits = min(6, 2 - int(math.floor(math.log10(ax))))
    s = f"{x:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s.replace(".", ",")

_MICRO = chr(0x00B5)
_MU = chr(0x03BC)

def unit_key(unit: Any) -> str:
    return "".join(str(unit).split()).lower().replace(_MU, _MICRO)

def _name_key(name: Any) -> str:
    return " ".join(str(name).split()).casefold().replace("ё", "е")

_INDEX_CACHE: dict[int, tuple[Any, dict[str, str]]] = {}

def _alias_index(cfg: Config) -> dict[str, str]:
    cached = _INDEX_CACHE.get(id(cfg.analytes))
    if cached is not None and cached[0] is cfg.analytes:
        return cached[1]
    index: dict[str, str] = {}
    for code in cfg.analytes:
        index.setdefault(_name_key(code), code)
    for code, spec in cfg.analytes.items():
        for alias in [*(spec.get("aliases") or []), spec.get("short_ru"), spec.get("name_ru")]:
            if alias:
                index.setdefault(_name_key(alias), code)
    _INDEX_CACHE[id(cfg.analytes)] = (cfg.analytes, index)
    return index

def resolve_analyte(key: Any, cfg: Config) -> str | None:
    if key in cfg.analytes:
        return key
    return _alias_index(cfg).get(_name_key(key))

def _unit_table(spec: dict) -> dict[str, float]:
    table = {unit_key(u): float(m) for u, m in (spec.get("units") or {}).items()}
    for canonical in (spec.get("unit"), spec.get("unit_ru")):
        if canonical:
            table.setdefault(unit_key(canonical), 1.0)
    return table

def _allowed_units(spec: dict) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in (spec.get("units") or {}):
        k = unit_key(u)
        if k not in seen:
            seen.add(k)
            out.append(str(u))
    return out

def _other_units(spec: dict, used_multiplier: float) -> list[tuple[str, float]]:
    by_mult: dict[float, str] = {}
    for u, m in (spec.get("units") or {}).items():
        m = float(m)
        if math.isclose(m, used_multiplier):
            continue
        is_cyrillic = any("а" <= ch <= "я" for ch in str(u).lower())
        if m not in by_mult or is_cyrillic:
            by_mult[m] = str(u)
    return [(u, m) for m, u in by_mult.items()]

def _split_value(raw: Any) -> tuple[Any, Any]:
    if isinstance(raw, LabValue):
        return raw.value, raw.unit
    if isinstance(raw, dict):
        return raw.get("value"), raw.get("unit")
    return raw, None

_CKD_EPI_BASE = 142.0
_CKD_EPI_KAPPA = {"F": 0.7, "M": 0.9}
_CKD_EPI_ALPHA = {"F": -0.241, "M": -0.302}
_CKD_EPI_EXPONENT_ABOVE = -1.200
_CKD_EPI_AGE_FACTOR = 0.9938
_CKD_EPI_FEMALE_FACTOR = 1.012
_UMOL_PER_MGDL_CREATININE = 88.4

def ckd_epi_2021(creatinine_umol_l: float, age_years: float, sex: str,
                 umol_per_mgdl: float = _UMOL_PER_MGDL_CREATININE) -> float:
    scr = creatinine_umol_l / umol_per_mgdl
    ratio = scr / _CKD_EPI_KAPPA[sex]
    egfr = (_CKD_EPI_BASE
            * min(ratio, 1.0) ** _CKD_EPI_ALPHA[sex]
            * max(ratio, 1.0) ** _CKD_EPI_EXPONENT_ABOVE
            * _CKD_EPI_AGE_FACTOR ** age_years)
    if sex == "F":
        egfr *= _CKD_EPI_FEMALE_FACTOR
    return egfr

def transferrin_saturation(serum_iron_umol_l: float, tibc_umol_l: float) -> float:
    return serum_iron_umol_l / tibc_umol_l * 100.0

def _in_range(value: float, bounds: Any) -> bool:
    return bool(bounds) and float(bounds[0]) <= value <= float(bounds[1])

def _add_derived(norm: Normalized, cfg: Config) -> None:
    values = norm.values

    if "TSAT" not in values and "serum_iron" in values and "TIBC" in values and values["TIBC"] > 0:
        spec = cfg.analytes.get("TSAT", {})
        tsat = round(transferrin_saturation(values["serum_iron"], values["TIBC"]), 1)
        formula = (f"железо {fmt_num(values['serum_iron'])} {cfg.unit_ru('serum_iron')} / "
                   f"ОЖСС {fmt_num(values['TIBC'])} {cfg.unit_ru('TIBC')} × 100")
        if not _in_range(tsat, spec.get("hard", [0, float("inf")])):
            norm.notes.append(NormalizationNote(
                analyte="TSAT", kind="soft_range",
                text=(f"НТЖ не рассчитано: {formula} = {fmt_num(tsat)} % — вне допустимого диапазона. "
                      "Проверьте значения и единицы железа и ОЖСС.")))
        else:
            values["TSAT"] = tsat
            norm.notes.append(NormalizationNote(
                analyte="TSAT", kind="derived", value=tsat, to_unit=spec.get("unit", "%"),
                text=f"НТЖ рассчитано: {formula} = {fmt_num(tsat)} %."))
            if spec.get("soft") and not _in_range(tsat, spec["soft"]):
                norm.notes.append(NormalizationNote(
                    analyte="TSAT", kind="soft_range", value=tsat,
                    text=f"Расчётное НТЖ {fmt_num(tsat)} % — нетипичное значение: проверьте железо и ОЖСС."))

    if "eGFR" not in values and "creatinine" in values:
        spec = cfg.analytes.get("eGFR", {})
        source = cfg.source_title("ckd_epi_2021")
        if norm.pregnancy_status == "yes":
            norm.notes.append(NormalizationNote(
                analyte="eGFR", kind="derived",
                text="рСКФ не рассчитана: при беременности формула CKD-EPI не применяется."))
            return
        factor = float((cfg.analytes.get("creatinine", {}).get("units") or {}).get("mg/dl", _UMOL_PER_MGDL_CREATININE))
        egfr = round(ckd_epi_2021(values["creatinine"], norm.age_years, norm.sex, factor), 1)
        if not _in_range(egfr, spec.get("hard", [0, float("inf")])):
            norm.notes.append(NormalizationNote(
                analyte="eGFR", kind="soft_range",
                text=(f"рСКФ не рассчитана: по креатинину {fmt_num(values['creatinine'])} {cfg.unit_ru('creatinine')} "
                      "получается значение вне допустимого диапазона. Проверьте значение и единицу креатинина.")))
            return
        values["eGFR"] = egfr
        norm.notes.append(NormalizationNote(
            analyte="eGFR", kind="derived", value=egfr, to_unit=spec.get("unit", ""),
            text=(f"рСКФ рассчитана по креатинину {fmt_num(values['creatinine'])} {cfg.unit_ru('creatinine')}, "
                  f"возрасту и полу: {fmt_num(egfr)} {cfg.unit_ru('eGFR')}. Источник: {source}.")))

def normalize_input(inp: AnalysisInput, cfg: Config) -> Normalized:
    sex = inp.sex
    status = inp.pregnancy.status
    trimester = inp.pregnancy.trimester
    if sex == "M" and status == "yes":
        raise InputError("pregnancy_male",
                         "Для мужчины указана беременность. Исправьте пол или уберите отметку о беременности.",
                         field="pregnancy")
    if sex == "M":
        status = "no"
    if status != "yes":
        trimester = None

    norm = Normalized(sex=sex, age_years=int(inp.age_years), pregnancy_status=status, trimester=trimester,
                      values={}, measured=set(), notes=[], norms_set=inp.norms)

    items = sorted((inp.values or {}).items(), key=lambda kv: kv[0] not in cfg.analytes)
    for raw_key, raw in items:
        value, unit = _split_value(raw)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        code = resolve_analyte(raw_key, cfg)
        if code is None:
            norm.notes.append(NormalizationNote(
                analyte=str(raw_key), kind="ignored",
                text=f"Показатель «{raw_key}» не распознан и не учтён."))
            continue
        spec = cfg.analytes[code]
        name, unit_ru = spec.get("name_ru", code), spec.get("unit_ru", "")
        if code in norm.measured:
            norm.notes.append(NormalizationNote(
                analyte=code, kind="ignored",
                text=f"{name} указан дважды: значение из поля «{raw_key}» не использовано."))
            continue
        try:
            original = float(value)
        except (TypeError, ValueError):
            raise InputError("out_of_range", f"{name}: значение «{value}» не является числом.", field=code) from None

        unit_given = unit is not None and str(unit).strip() != ""
        multiplier = 1.0
        if unit_given:
            table = _unit_table(spec)
            key = unit_key(unit)
            if key not in table:
                raise InputError(
                    "unknown_unit",
                    f"{name}: единица «{unit}» не распознана. Допустимые единицы: {', '.join(_allowed_units(spec))}.",
                    field=code)
            multiplier = table[key]
        converted = round(original * multiplier, 6)

        hard = spec.get("hard")
        if not math.isfinite(converted) or (hard and not _in_range(converted, hard)):
            hint = ""
            soft = spec.get("soft") or hard
            candidates = [u for u, m in _other_units(spec, multiplier) if soft and _in_range(original * m, soft)]
            if candidates:
                hint = f" Возможно, значение указано в «{candidates[0]}» — укажите единицу."
            bounds = f"{fmt_num(hard[0])}–{fmt_num(hard[1])} {unit_ru}" if hard else ""
            raise InputError(
                "out_of_range",
                f"{name}: значение {fmt_num(converted)} {unit_ru} вне допустимого диапазона {bounds}. "
                f"Проверьте значение и единицу измерения.{hint}",
                field=code)

        norm.values[code] = converted
        norm.measured.add(code)

        if unit_given and not math.isclose(multiplier, 1.0):
            norm.notes.append(NormalizationNote(
                analyte=code, kind="converted", original=original, value=converted,
                from_unit=str(unit).strip(), to_unit=spec.get("unit"),
                text=f"{name}: {fmt_num(original)} {str(unit).strip()} пересчитано в {fmt_num(converted)} {unit_ru}."))
        if not unit_given and spec.get("unit_ambiguous"):
            others = ", ".join(u for u, _ in _other_units(spec, 1.0))
            norm.notes.append(NormalizationNote(
                analyte=code, kind="unit_assumed", value=converted, to_unit=spec.get("unit"),
                text=(f"{name}: единица не указана, принято {unit_ru}. "
                      f"У этого показателя в ходу и другие единицы ({others}) — проверьте.")))
        soft = spec.get("soft")
        if soft and not _in_range(converted, soft):
            norm.notes.append(NormalizationNote(
                analyte=code, kind="soft_range", value=converted,
                text=(f"{name}: {fmt_num(converted)} {unit_ru} — нетипичное значение "
                      f"(обычно {fmt_num(soft[0])}–{fmt_num(soft[1])} {unit_ru}). Проверьте значение и единицу.")))

    if "hemoglobin" not in norm.values:
        raise InputError("missing_hemoglobin",
                         "Не указан гемоглобин: без него нельзя оценить анемию. Добавьте значение гемоглобина (г/л).",
                         field="hemoglobin")

    _add_derived(norm, cfg)
    from .references import ranges_from_request, resolve
    norm.references = resolve(cfg, sex, status, lab_name=inp.lab_reference,
                              request_ranges=ranges_from_request(inp.reference_ranges, cfg))
    return norm
