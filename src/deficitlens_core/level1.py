from __future__ import annotations

from typing import Any

from .config import Config
from .normalize import Normalized, fmt_num
from .schemas import Level1, UrgentItem

SEVERITY_RU = {"mild": "лёгкая", "moderate": "умеренная", "severe": "тяжёлая"}
_TRIMESTER_RU = {1: "I триместр", 2: "II триместр", 3: "III триместр"}
WHO_HB_AGE_MAX = 65

def _by_trimester(table: dict, trimester: int | None) -> Any:
    key: Any = trimester if trimester in (1, 2, 3) else "unknown"
    for k in (key, str(key)):
        if k in table:
            return table[k]
    return table["unknown"]

def hemoglobin_rule(norm: Normalized, cfg: Config) -> dict:
    hb_cfg = cfg.norms["hemoglobin"]
    if norm.pregnancy_status == "yes":
        preg = hb_cfg["pregnancy"]
        trimester = norm.trimester if norm.trimester in (1, 2, 3) else None
        context = f"беременность, {_TRIMESTER_RU[trimester]}" if trimester else "беременность, триместр не указан"
        return {"threshold": float(_by_trimester(preg["anemia_below"], trimester)),
                "bands": _by_trimester(preg["severity"], trimester), "pregnant": True, "context": context}
    return {"threshold": float(hb_cfg["anemia_below"][norm.sex]), "bands": hb_cfg["severity"][norm.sex],
            "pregnant": False, "context": "женщины" if norm.sex == "F" else "мужчины"}

def _severity(hb: float, bands: dict) -> str:
    if hb < float(bands["severe_below"]):
        return "severe"
    if hb < float(bands["moderate"][1]):
        return "moderate"
    return "mild"

def assess_level1(norm: Normalized, cfg: Config) -> Level1:
    hb = float(norm.values["hemoglobin"])
    hb_cfg = cfg.norms["hemoglobin"]
    rule = hemoglobin_rule(norm, cfg)
    threshold = rule["threshold"]
    anemia = hb < threshold
    severity = _severity(hb, rule["bands"]) if anemia else None

    urgent_cfg = cfg.norms["urgent"]
    project_source = cfg.source_title(urgent_cfg.get("source", "project"))
    who_source = cfg.source_title(hb_cfg["source"])
    urgent: list[UrgentItem] = []
    labels = urgent_cfg.get("labels") or {}
    hb_label = labels.get("hemoglobin", "тяжёлая анемия по ВОЗ")
    hb_urgent = float(urgent_cfg["hemoglobin_below"]["pregnant" if rule["pregnant"] else "default"])
    if hb < hb_urgent:
        urgent.append(UrgentItem(
            code="hemoglobin_very_low",
            text=(f"Гемоглобин {fmt_num(hb)} г/л — ниже {fmt_num(hb_urgent)} г/л ({hb_label}): "
                  "нужна срочная очная оценка врача."),
            source=f"{hb_label} — {who_source}; срочность — {project_source}"))
    for analyte, key, code, label_key in (("WBC", "wbc_below", "wbc_very_low", "wbc"),
                                          ("platelets", "platelets_below", "platelets_very_low", "platelets")):
        if analyte in norm.values and norm.values[analyte] < float(urgent_cfg[key]):
            unit = cfg.unit_ru(analyte)
            value, limit = fmt_num(norm.values[analyte]), fmt_num(urgent_cfg[key])
            label = labels.get(label_key, "выбор проекта")
            urgent.append(UrgentItem(
                code=code,
                text=(f"{cfg.name_ru(analyte)} {value} {unit} — ниже {limit} {unit} ({label}): "
                      "нужна срочная очная оценка врача."),
                source=project_source))

    notes: list[str] = []
    if rule["pregnant"]:
        if norm.trimester in (1, 2, 3):
            notes.append(f"Беременность, {_TRIMESTER_RU[norm.trimester]}: применён порог {fmt_num(threshold)} г/л; "
                         "нужна оценка врача.")
        else:
            others = hb_cfg["pregnancy"]["anemia_below"]
            notes.append(
                f"Беременность, триместр не указан: применён порог {fmt_num(threshold)} г/л "
                f"(по триместрам: {fmt_num(_by_trimester(others, 1))} / {fmt_num(_by_trimester(others, 2))} / "
                f"{fmt_num(_by_trimester(others, 3))} г/л); укажите триместр, нужна оценка врача.")
    elif norm.pregnancy_status == "unknown":
        preg = hb_cfg["pregnancy"]["anemia_below"]
        notes.append(
            f"Беременность не уточнена: применён порог для небеременных ({fmt_num(threshold)} г/л). "
            f"При беременности пороги другие ({fmt_num(_by_trimester(preg, 1))} / {fmt_num(_by_trimester(preg, 2))} / "
            f"{fmt_num(_by_trimester(preg, 3))} г/л по триместрам) — нужна оценка врача.")
    if norm.age_years > int(hb_cfg.get("age_max", WHO_HB_AGE_MAX)) and hb_cfg.get("age_note"):
        notes.append(str(hb_cfg["age_note"]))
    if norm.norms_set == "ru" and not rule["pregnant"] and hb_cfg.get("ru_note"):
        notes.append(str(hb_cfg["ru_note"]))
    if urgent:
        notes.append(str(urgent_cfg.get("note") or "Срочность — выбор проекта; требует решения врача команды."))

    return Level1(
        anemia=anemia, hemoglobin=hb, threshold_g_l=threshold,
        severity=severity, severity_ru=SEVERITY_RU[severity] if severity else None,
        source=who_source, note=" ".join(notes) or None, urgent=urgent)
