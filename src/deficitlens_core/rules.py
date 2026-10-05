from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import constants as C
from .config import Config
from .normalize import Normalized, fmt_num
from .references import range_text, ref_phrase, references_for
from .schemas import ChecklistItem, Flag, Level1, RuleSignal

@dataclass
class RulesOutcome:
    flags: list[Flag] = field(default_factory=list)
    rule_signals: list[RuleSignal] = field(default_factory=list)
    checklist: list[ChecklistItem] = field(default_factory=list)
    mandatory_tests: list[dict] = field(default_factory=list)
    derived: dict[str, float] = field(default_factory=dict)
    hemolysis: "Hemolysis | None" = None

@dataclass
class Hemolysis:
    level: str = "none"
    parts: dict = field(default_factory=dict)
    signs: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    fallback: bool = False
    default_refs: bool = False

    @property
    def found(self) -> bool:
        return self.level in ("suspected", "strong")

    def names(self, codes) -> str:
        return ", ".join(LAB_REFERENCE_NAMES.get(a, a) for a in codes)

    def detail(self, lead: bool = True) -> str:
        head = {"strong": "сильное подозрение",
                "signs": "анемии нет — вывод о гемолизе не делается"}.get(self.level) if lead else None
        shown = [self.parts[a][1] for a in C.GROUPS["hemolysis"] if self.parts[a][0] not in ("missing",)]
        text = "; ".join(([head] if head else []) + shown)
        if self.missing:
            text += f". Не оценены: {self.names(self.missing)}"
        if self.found or self.level == "signs":
            text += (". Рекомендуется досдать недостающие показатели и сопоставить результат с ОАК и мазком "
                     "периферической крови" if self.missing else
                     ". Рекомендуется сопоставить результат с ОАК и мазком периферической крови")
        if self.fallback:
            text += (". Гаптоглобин оценён по референсу проекта по умолчанию — это не универсальный норматив: "
                     "сверьте с референсом лаборатории")
        return text

HEMOLYSIS_SIGN = {"LDH": "high", "indirect_bilirubin": "high", "haptoglobin": "low", "reticulocytes": "high"}

def assess_hemolysis(norm: Normalized, cfg: Config, refs: Any, anemia: bool) -> Hemolysis:
    h = Hemolysis()
    vals = norm.values
    hct_norm = (norm_value(cfg, "hemolysis", "retic_hct_norm") or {}).get(norm.sex)
    min_signs = int(norm_value(cfg, "hemolysis", "min_signs") or 2)
    for a in C.GROUPS["hemolysis"]:
        plural, name = a in PLURAL_ANALYTES, LAB_REFERENCE_NAMES.get(a) or cfg.short_ru(a)
        if a not in vals:
            h.parts[a] = ("missing", f"{name} — {'не сданы' if plural else 'не сдан'}")
            h.missing.append(a)
            continue
        value = float(vals[a])
        shown = f"{fmt_num(value)} {cfg.unit_ru(a)}"
        ref = refs.get(a)
        if ref is None:
            status = LAB_REFERENCE_ONLY.replace("сдан", "сданы", 1) if plural else LAB_REFERENCE_ONLY
            h.parts[a] = ("no_ref", f"{name} {shown} — {status}")
            h.missing.append(a)
            continue
        if a == "reticulocytes" and anemia and hct_norm:
            if "hematocrit" in vals:
                value = round(value * float(vals["hematocrit"]) / float(hct_norm), 2)
                shown += f" (скорректированные по Hct {fmt_num(vals['hematocrit'])} %: {fmt_num(value)} %)"
            else:
                shown += " (без поправки на гематокрит: Hct не сдан)"
        state = "low" if ref.below(value) else "high" if ref.above(value) else "within"
        phrase = ref_phrase(state, ref, cfg.unit_ru(a))
        if ref.local:
            phrase = phrase.replace("референса", "референса лаборатории", 1)
        h.parts[a] = (state, f"{name} {shown} — {phrase}")
        if not ref.local:
            h.default_refs = True
            if a == "haptoglobin" and ref.fallback:
                h.fallback = True
        if HEMOLYSIS_SIGN[a] == state:
            h.signs.append(a)
    hapto_low = "haptoglobin" in h.signs
    others = [a for a in h.signs if a != "haptoglobin"]
    if anemia:
        if hapto_low and others:
            h.level = "strong"
        elif len(h.signs) >= min_signs:
            h.level = "suspected"
        elif h.parts["haptoglobin"][0] == "within" and h.parts["LDH"][0] == "within":
            h.level = "excluded"
        else:
            h.level = "not_checked"
    elif len(h.signs) >= min_signs:
        h.level = "signs"
    return h

CHECKLIST_TITLES = {
    "iron": "Дефицит железа", "b12": "Дефицит витамина B12", "folate": "Дефицит фолатов",
    "inflammation": "Воспаление", "kidney": "Почки", "thyroid": "Щитовидная железа", "copper": "Дефицит меди",
    "hemolysis": "Гемолиз",
}
FLAG_TITLES = {
    "ferritin_masked_by_inflammation": "Ферритин под маской воспаления",
    "normal_mcv_high_rdw": "Нормальный MCV при высоком RDW",
    "b12_gray_zone": "B12 требует функционального подтверждения",
    "microcytosis_not_iron": "Микроцитоз не от дефицита железа",
    "unexplained_checklist": "Дефицит по сданным анализам не найден — чек-лист исключений",
}
FERRITIN_SIGNALS = {"ferritin_below_who", "ferritin_below_practice", "ferritin_masked_by_inflammation"}
IRON_SIGNALS = FERRITIN_SIGNALS | {"tsat_low", "tsat_low_normal_ferritin", "ret_he_low"}
FUNCTIONAL_IRON_CLARIFY = (("CRP", "СРБ"), ("Ret_He", "Ret-He"), ("sTfR", "sTfR"))
LAB_REFERENCE_ONLY = "сдан, оценка по референсу лаборатории"
LAB_REFERENCE_NAMES = {"LDH": "ЛДГ", "indirect_bilirubin": "непрямой билирубин", "haptoglobin": "гаптоглобин",
                       "copper": "медь",
                       "reticulocytes": "ретикулоциты", "ceruloplasmin": "церулоплазмин"}
PLURAL_ANALYTES = {"reticulocytes"}
B12_FOLATE_SIGNALS = {"b12_low", "b12_gray_zone", "active_b12_low", "folate_low"}
MICRO_CASE_SET = (("vitamin_B6", "B6", "витамин B6"), ("copper", "медь", "медь"),
                  ("ceruloplasmin", "церулоплазмин", "церулоплазмин"))
CASE_SET_THRESHOLDS = {"vitamin_B6": ("vitamin_b6", "deficiency_below"), "copper": ("copper", "deficiency_below")}
FEMININE_ANALYTES = {"copper"}

def case_set_state(code: str, name: str, norm: Normalized, cfg: Config, refs: Any) -> str:
    value, unit = float(norm.values[code]), cfg.unit_ru(code)
    shown = f"{name} {fmt_num(value)} {unit}"
    thr = norm_value(cfg, *CASE_SET_THRESHOLDS[code]) if code in CASE_SET_THRESHOLDS else None
    ref = refs.get(code)
    if ref is None and thr is None:
        return f"{shown} — оценка по референсу лаборатории"
    low = _below(value, thr) or (ref is not None and ref.below(value))
    fem = code in FEMININE_ANALYTES
    word = ("снижена" if fem else "снижен") if low else ("не снижена" if fem else "не снижен")
    basis = f"референс {range_text(ref, unit)}" if ref is not None else f"рабочий порог {fmt_num(thr)} {unit}"
    return f"{shown} {word} ({basis})"

NO_RU_THRESHOLD_TEXT = "числового порога в КР РФ нет"

def use_foreign(cfg: Config) -> bool:
    return bool((cfg.norms.get("policy") or {}).get("use_foreign", False))

def foreign_allowed(cfg: Config) -> set[str]:
    return {str(k) for k in ((cfg.norms.get("policy") or {}).get("foreign_allowed") or [])}

def foreign_blocked(cfg: Config, key: str) -> bool:
    entry = cfg.norms.get(key)
    return (isinstance(entry, dict) and entry.get("origin") == "foreign" and not use_foreign(cfg)
            and key not in foreign_allowed(cfg))

def b12_numeric(cfg: Config) -> bool:
    return norm_value(cfg, "vitamin_b12", "deficiency_below") is not None

def norm_value(cfg: Config, *path: str) -> Any:
    if path and foreign_blocked(cfg, path[0]):
        return None
    node: Any = cfg.norms
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node

def _below(value: float | None, threshold: Any) -> bool:
    return value is not None and threshold is not None and value < float(threshold)

def _above(value: float | None, threshold: Any) -> bool:
    return value is not None and threshold is not None and value > float(threshold)

def _in_zone(value: float | None, zone: Any, inclusive: bool = False) -> bool:
    if value is None or not zone:
        return False
    low, high = float(zone[0]), float(zone[1])
    return low <= value <= high if inclusive else low <= value < high

def mentzer_index(norm: Normalized, cfg: Config) -> float | None:
    mcv, rbc = norm.values.get("MCV"), norm.values.get("RBC")
    if not _below(mcv, norm_value(cfg, "indices", "mcv_micro_below")) or not rbc:
        return None
    return round(mcv / rbc, 1)

def mentzer_phrase(value: float, cfg: Config, iron_excluded: bool = False) -> str:
    thr = norm_value(cfg, "indices", "mentzer_thalassemia_like_below")
    head = f"индекс Ментцера (MCV/RBC) {fmt_num(value)}"
    if thr is None:
        return f"{head}; подтверждение — электрофорез гемоглобина"
    t = fmt_num(thr)
    if value < float(thr):
        return f"{head}: меньше {t} — вероятнее носительство β-талассемии; подтверждение — электрофорез гемоглобина"
    if value == float(thr):
        return f"{head} — ровно {t}: неопределённо; при сомнении — электрофорез гемоглобина"
    if iron_excluded:
        return (f"{head} — не меньше {t}: на носительство β-талассемии не указывает, но и не исключает его; "
                "проверка — электрофорез гемоглобина")
    return (f"{head} — не меньше {t}: скорее дефицит железа, чем носительство β-талассемии "
            f"(меньше {t} — повод для электрофореза гемоглобина)")

def mentzer_thalassemia_like(norm: Normalized, cfg: Config) -> bool:
    value, thr = mentzer_index(norm, cfg), norm_value(cfg, "indices", "mentzer_thalassemia_like_below")
    return value is not None and thr is not None and value < float(thr)

def mentzer_source(cfg: Config) -> str:
    return cfg.source_title(norm_value(cfg, "indices", "mentzer_source") or "mentzer_1973")

def has_inflammation(norm: Normalized, cfg: Config) -> bool | None:
    crp, threshold = norm.values.get("CRP"), norm_value(cfg, "crp", "inflammation_above")
    if crp is None or threshold is None:
        return None
    return crp > float(threshold)

def iron_deficiency_on_inflammation(norm: Normalized, cfg: Config) -> bool:
    if norm.pregnancy_status == "yes" or not has_inflammation(norm, cfg):
        return False
    v = norm.values.get
    return (_below(v("ferritin"), norm_value(cfg, "ferritin", "inflammation_below"))
            or _below(v("TSAT"), norm_value(cfg, "tsat", "low_below"))
            or _below(v("Ret_He"), norm_value(cfg, "ret_he", "low_below")))

@dataclass(frozen=True)
class ThresholdDeficit:
    nutrient: str
    analyte: str
    value: float
    threshold: float
    source_key: str
    crp_missing: bool = False

def threshold_deficits(norm: Normalized, cfg: Config) -> list[ThresholdDeficit]:
    if norm.pregnancy_status == "yes":
        return []
    v = norm.values.get
    out: list[ThresholdDeficit] = []
    fer, crp = v("ferritin"), v("CRP")
    fer_who = norm_value(cfg, "ferritin", "deficiency_below", "who")
    if _below(fer, fer_who) and not _above(crp, norm_value(cfg, "crp", "inflammation_above")):
        key = (norm_value(cfg, "ferritin", "sources") or {}).get("who") or "who_ferritin_2020"
        out.append(ThresholdDeficit("iron", "ferritin", float(fer), float(fer_who), key, crp_missing=crp is None))
    b12_thr = norm_value(cfg, "vitamin_b12", "deficiency_below")
    if _below(v("vitamin_B12"), b12_thr):
        out.append(ThresholdDeficit("b12", "vitamin_B12", float(v("vitamin_B12")), float(b12_thr),
                                    norm_value(cfg, "vitamin_b12", "source") or "nice_ng239"))
    fol_thr = norm_value(cfg, "folate", "deficiency_below")
    if _below(v("folate"), fol_thr):
        out.append(ThresholdDeficit("folate", "folate", float(v("folate")), float(fol_thr),
                                    norm_value(cfg, "folate", "source") or "kr_folate_2024"))
    return out

def evaluate_rules(norm: Normalized, level1: Level1, cfg: Config) -> RulesOutcome:
    out = RulesOutcome()
    v = norm.values.get
    anemia = bool(level1.anemia)

    def q(code: str) -> str:
        return f"{fmt_num(norm.values[code])} {cfg.unit_ru(code)}"

    def src(key: str | None) -> str:
        return cfg.source_title(key) if key else ""

    seen_tests: set[str] = set()

    def need(analyte: str, reason: str, source_key: str) -> None:
        if analyte in norm.values or analyte in seen_tests or analyte not in cfg.analytes:
            return
        seen_tests.add(analyte)
        out.mandatory_tests.append({"analyte": analyte, "reason": reason, "source": src(source_key)})

    if norm.is_derived("TSAT"):
        out.derived["tsat_calc"] = float(norm.values["TSAT"])
    if norm.is_derived("eGFR"):
        out.derived["egfr_calc"] = float(norm.values["eGFR"])
    mcv, mch, rdw = v("MCV"), v("MCH"), v("RDW")
    mentzer = mentzer_index(norm, cfg)
    if mentzer is not None:
        out.derived["mentzer"] = mentzer

    fer_who = norm_value(cfg, "ferritin", "deficiency_below", "who")
    fer_ru = norm_value(cfg, "ferritin", "deficiency_below", "ru")
    fer_infl = norm_value(cfg, "ferritin", "inflammation_below")
    fer_sources = norm_value(cfg, "ferritin", "sources") or {}
    fer_label = norm_value(cfg, "ferritin", "ru_label") or "порог практики"
    crp_thr = norm_value(cfg, "crp", "inflammation_above")
    tsat_thr = norm_value(cfg, "tsat", "low_below")
    rethe_thr = norm_value(cfg, "ret_he", "low_below")
    b12_thr = norm_value(cfg, "vitamin_b12", "deficiency_below")
    b12_zone = norm_value(cfg, "vitamin_b12", "gray_zone")
    ab12_thr = norm_value(cfg, "active_b12", "deficiency_below")
    ab12_zone = norm_value(cfg, "active_b12", "gray_zone")
    hcy_thr = norm_value(cfg, "homocysteine", "high_above")
    refs = references_for(norm, cfg)
    fol_thr = norm_value(cfg, "folate", "deficiency_below")
    fol_zone = norm_value(cfg, "folate", "borderline")
    b6_thr = norm_value(cfg, "vitamin_b6", "deficiency_below")
    cu_thr = norm_value(cfg, "copper", "deficiency_below")
    egfr_low = norm_value(cfg, "egfr", "reduced_below")
    egfr_severe = norm_value(cfg, "egfr", "severe_below")
    tsh_high = norm_value(cfg, "tsh", "high_above")
    tsh_marked = norm_value(cfg, "tsh", "marked_above")
    mcv_micro = norm_value(cfg, "indices", "mcv_micro_below")
    mcv_macro = norm_value(cfg, "indices", "mcv_macro_above")
    mch_low = norm_value(cfg, "indices", "mch_low_below")
    rdw_high = norm_value(cfg, "indices", "rdw_high_above")
    indices_src = norm_value(cfg, "indices", "source") or "lab_reference"
    b12_blocked = foreign_blocked(cfg, "vitamin_b12")
    ab12_blocked = foreign_blocked(cfg, "active_b12")
    b12_inclusive = bool(norm_value(cfg, "vitamin_b12", "gray_zone_inclusive"))
    ab12_inclusive = bool(norm_value(cfg, "active_b12", "gray_zone_inclusive"))

    fer, crp, tsat, rethe = v("ferritin"), v("CRP"), v("TSAT"), v("Ret_He")
    b12, ab12, hcy, mma, fol = v("vitamin_B12"), v("active_B12"), v("homocysteine"), v("MMA"), v("folate")
    egfr, tsh = v("eGFR"), v("TSH")
    inflamed = _above(crp, crp_thr)
    tsat_mark = " (расчётное)" if norm.is_derived("TSAT") else ""
    egfr_mark = (f" (расчёт по креатинину {q('creatinine')}, CKD-EPI 2021)"
                 if norm.is_derived("eGFR") and "creatinine" in norm.values else "")
    tsat_is_low = _below(tsat, tsat_thr)
    masked = (fer is not None and inflamed and fer_who is not None and fer_infl is not None
              and float(fer_who) <= fer < float(fer_infl))
    practice = norm.norms_set == "ru"
    fer_between = (fer is not None and not practice and not inflamed and fer_who is not None and fer_ru is not None
                   and float(fer_who) <= fer < float(fer_ru))

    pregnant = norm.pregnancy_status == "yes"
    if not pregnant:
        def signal(code: str, text: str, source_key: str | None) -> None:
            out.rule_signals.append(RuleSignal(code=code, text=text, source=src(source_key)))

        if _below(fer, fer_who):
            signal("ferritin_below_who",
                   f"ферритин {q('ferritin')} — ниже {fmt_num(fer_who)} мкг/л (дефицит железа по критерию ВОЗ)",
                   fer_sources.get("who"))
        elif masked:
            signal("ferritin_masked_by_inflammation",
                   f"ферритин {q('ferritin')} при СРБ {q('CRP')} — при воспалении дефицит железа вероятен "
                   f"при ферритине ниже {fmt_num(fer_infl)} мкг/л",
                   fer_sources.get("inflammation"))
        elif fer is not None and not inflamed and fer_who is not None and _below(fer, fer_ru) and practice:
            signal("ferritin_below_practice",
                   f"ферритин {q('ferritin')} — ниже {fmt_num(fer_ru)} мкг/л ({fer_label})",
                   fer_sources.get("ru"))
        ferritin_signal = any(s.code in FERRITIN_SIGNALS for s in out.rule_signals)
        functional_iron = tsat_is_low and fer is not None and not ferritin_signal
        if functional_iron:
            clarify = [name for code, name in FUNCTIONAL_IRON_CLARIFY if code not in norm.values]
            clarify_text = ("; уточнить " + (", ".join(clarify[:-1]) + " или " if len(clarify) > 1 else "")
                            + clarify[-1]) if clarify else ""
            signal("tsat_low_normal_ferritin",
                   f"насыщение трансферрина{tsat_mark} {q('TSAT')} — ниже {fmt_num(tsat_thr)} % при несниженном "
                   f"ферритине {q('ferritin')}: возможен функциональный дефицит железа{clarify_text}; "
                   "показатель колеблется в течение дня, один результат не решает",
                   norm_value(cfg, "tsat", "source"))
        elif tsat_is_low:
            signal("tsat_low",
                   f"насыщение трансферрина{tsat_mark} {q('TSAT')} — ниже {fmt_num(tsat_thr)} % "
                   "(нижняя граница референса КР)",
                   norm_value(cfg, "tsat", "source"))
        if _below(rethe, rethe_thr):
            signal("ret_he_low", f"Ret-He {q('Ret_He')} — ниже {fmt_num(rethe_thr)} пг",
                   norm_value(cfg, "ret_he", "source"))
        if _below(b12, b12_thr):
            signal("b12_low", f"витамин B12 {q('vitamin_B12')} — ниже {fmt_num(b12_thr)} пг/мл (NICE NG239)",
                   norm_value(cfg, "vitamin_b12", "source"))
        elif _in_zone(b12, b12_zone, b12_inclusive):
            signal("b12_gray_zone",
                   f"витамин B12 {q('vitamin_B12')} — серая зона {fmt_num(b12_zone[0])}–{fmt_num(b12_zone[1])} пг/мл "
                   "(NICE NG239), нужен функциональный маркер",
                   norm_value(cfg, "vitamin_b12", "source"))
        if _below(ab12, ab12_thr):
            signal("active_b12_low", f"активный B12 {q('active_B12')} — ниже {fmt_num(ab12_thr)} пмоль/л",
                   norm_value(cfg, "active_b12", "source"))
        if _below(fol, fol_thr):
            signal("folate_low", f"фолаты {q('folate')} — ниже {fmt_num(fol_thr)} нг/мл",
                   norm_value(cfg, "folate", "source"))
        if _below(v("vitamin_B6"), b6_thr):
            signal("b6_low", f"витамин B6 {q('vitamin_B6')} — ниже {fmt_num(b6_thr)} нмоль/л (рабочий порог)",
                   norm_value(cfg, "vitamin_b6", "source"))
        if _below(v("copper"), cu_thr):
            signal("copper_low", f"медь {q('copper')} — ниже {fmt_num(cu_thr)} мкмоль/л (рабочий порог)",
                   norm_value(cfg, "copper", "source"))
        signal_codes = {s.code for s in out.rule_signals}
        iron_signal = bool(signal_codes & IRON_SIGNALS)
        if functional_iron:
            for analyte, _ in FUNCTIONAL_IRON_CLARIFY:
                need(analyte, "НТЖ снижено при несниженном ферритине: уточнить, есть ли функциональный дефицит железа"
                              + (" (оценка по референсу лаборатории)." if analyte == "sTfR" else "."),
                     "who_ferritin_2020" if analyte == "CRP" else ("project" if analyte == "sTfR" else "kr_ida_2024"))
        b12_folate_signal = bool(signal_codes & B12_FOLATE_SIGNALS)

        if masked:
            parts = [f"Ферритин {q('ferritin')} при СРБ {q('CRP')} может быть ложно нормальным: по ВОЗ при воспалении "
                     f"(СРБ выше {fmt_num(crp_thr)} мг/л) дефицит железа вероятен при ферритине "
                     f"ниже {fmt_num(fer_infl)} мкг/л."]
            infl_note = str(norm_value(cfg, "ferritin", "inflammation_note") or "").strip().rstrip(".")
            parts.append((infl_note[:1].upper() + infl_note[1:] + ".") if infl_note else
                         f"Порог {fmt_num(fer_infl)} мкг/л при воспалении — условная рекомендация ВОЗ.")
            if tsat is not None and tsat_thr is not None:
                parts.append(f"НТЖ{tsat_mark} {q('TSAT')} — ниже {fmt_num(tsat_thr)} %: это поддерживает дефицит железа."
                             if tsat_is_low else f"НТЖ{tsat_mark} {q('TSAT')} не снижено.")
            if rethe is not None and rethe_thr is not None:
                parts.append(f"Ret-He {q('Ret_He')} — ниже {fmt_num(rethe_thr)} пг: кроветворению не хватает железа "
                             "(абсолютный или функциональный дефицит)."
                             if _below(rethe, rethe_thr) else f"Ret-He {q('Ret_He')} не снижен.")
            if "sTfR" in norm.values:
                parts.append(f"sTfR {q('sTfR')} — оценка по референсу лаборатории.")
            clarify = (("TSAT", "насыщение трансферрина"), ("sTfR", "sTfR"), ("Ret_He", "Ret-He"))
            missing = [name for code, name in clarify if code not in norm.values]
            if missing:
                head = ", ".join(missing[:-1]) + (" или " if len(missing) > 1 else "")
                parts.append(f"Уточнить: {head}{missing[-1]}.")
            out.flags.append(Flag(code="ferritin_masked_by_inflammation",
                                  title=FLAG_TITLES["ferritin_masked_by_inflammation"],
                                  text=" ".join(parts), source=src(fer_sources.get("inflammation"))))
            need("TSAT", "Показывает, сколько железа доступно для кроветворения; при воспалении ферритин завышается.",
                 "kr_ida_2024")
            need("sTfR", "Мало зависит от воспаления: помогает увидеть дефицит железа под маской воспаления "
                         "(оценка по референсу лаборатории).", "project")
            need("Ret_He", "Показывает, хватает ли железа кроветворению сейчас: снижен и при абсолютном, и при "
                           "функциональном дефиците железа.", "kr_ida_2024")

        mcv_normal = (mcv is not None and mcv_micro is not None and mcv_macro is not None
                      and float(mcv_micro) <= mcv <= float(mcv_macro))
        if anemia and mcv_normal and _above(rdw, rdw_high):
            axes_checked = all(a in norm.values for a in ("ferritin", "vitamin_B12", "folate"))
            both_deficient = iron_signal and b12_folate_signal
            b12_unrated = b12 is not None and b12_blocked
            if not axes_checked or both_deficient or (iron_signal and b12_unrated):
                def axis(code: str, short: str, low_codes: set[str], gray: bool = False, plural: bool = False,
                         unrated: bool = False) -> str:
                    if code not in norm.values:
                        return f"{short} не {'сданы' if plural else 'сдан'}"
                    if unrated:
                        return f"{short} {q(code)} — оценка по референсу лаборатории"
                    if gray:
                        return f"{short} {q(code)} — серая зона"
                    if signal_codes & low_codes:
                        return f"{short} {q(code)} — {'снижены' if plural else 'снижен'}"
                    return f"{short} {q(code)} — не {'снижены' if plural else 'снижен'}"

                state = "; ".join([
                    axis("ferritin", "ферритин", FERRITIN_SIGNALS),
                    axis("vitamin_B12", "B12", {"b12_low"}, gray="b12_gray_zone" in signal_codes,
                         unrated=b12_blocked),
                    axis("folate", "фолаты", {"folate_low"}, plural=True)])
                text = (f"MCV {q('MCV')} в норме ({fmt_num(mcv_micro)}–{fmt_num(mcv_macro)} фл) при RDW {q('RDW')} "
                        f"(выше {fmt_num(rdw_high)} %): так бывает при сочетании дефицита железа с дефицитом B12 "
                        f"или фолатов. Проверить обе оси: железо и B12 с фолатами. Сейчас: {state}.")
                if both_deficient:
                    text += " Сочетанный дефицит поддерживается сданными анализами."
                out.flags.append(Flag(code="normal_mcv_high_rdw", title=FLAG_TITLES["normal_mcv_high_rdw"],
                                      text=text, source=src(indices_src)))
                need("ferritin", "Нормальный MCV при высоком RDW: нужно проверить запас железа.", "kr_ida_2024")
                need("vitamin_B12", "Нормальный MCV при высоком RDW: возможен сочетанный дефицит — нужно проверить B12.",
                     "kr_b12_2024")
                need("folate", "Нормальный MCV при высоком RDW: возможен сочетанный дефицит — нужно проверить фолаты.",
                     "kr_folate_2024")

        if _in_zone(b12, b12_zone, b12_inclusive):
            kidney_reduced = _below(egfr, egfr_low)
            parts = [f"Витамин B12 {q('vitamin_B12')} — в серой зоне NICE NG239 ({fmt_num(b12_zone[0])}–{fmt_num(b12_zone[1])} "
                     "пг/мл; зарубежный источник, в КР РФ числового порога нет): по общему B12 дефицит нельзя ни "
                     "подтвердить, ни исключить. Нужен функциональный маркер — "
                     "активный B12 (голотранскобаламин) или гомоцистеин"
                     + ("." if mma is not None else
                        "; метилмалоновая кислота в рознице Москвы отдельным анализом недоступна.")]
            if ab12 is not None:
                if _below(ab12, ab12_thr):
                    parts.append(f"Активный B12 {q('active_B12')} — ниже {fmt_num(ab12_thr)} пмоль/л: "
                                 "функциональный дефицит подтверждается.")
                elif _in_zone(ab12, ab12_zone, ab12_inclusive):
                    parts.append(f"Активный B12 {q('active_B12')} — тоже в серой зоне "
                                 f"({fmt_num(ab12_zone[0])}–{fmt_num(ab12_zone[1])} пмоль/л).")
                elif ab12_thr is not None:
                    parts.append(f"Активный B12 {q('active_B12')} не снижен.")
            if hcy is not None and hcy_thr is not None:
                parts.append(f"Гомоцистеин {q('homocysteine')} — выше {fmt_num(hcy_thr)} мкмоль/л: поддерживает "
                             "функциональный дефицит B12." if _above(hcy, hcy_thr)
                             else f"Гомоцистеин {q('homocysteine')} не повышен.")
            mma_ref = refs.get("MMA")
            if mma is not None and mma_ref is not None:
                where = "выше референса" if mma_ref.above(mma) else "в пределах референса"
                parts.append(f"ММК {q('MMA')} — {where} ({range_text(mma_ref, cfg.unit_ru('MMA'))}; зависит от метода, "
                             "диагностического порога нет).")
            if kidney_reduced:
                parts.append(f"При рСКФ ниже {fmt_num(egfr_low)} мл/мин/1,73 м² (здесь {fmt_num(egfr)}) гомоцистеин и ММК "
                             "повышаются и без дефицита B12.")
            source = src(norm_value(cfg, "vitamin_b12", "source"))
            hcy_source = src(norm_value(cfg, "homocysteine", "source"))
            if hcy_source and hcy_source != source:
                source = f"{source}; гомоцистеин — {hcy_source}"
            out.flags.append(Flag(code="b12_gray_zone", title=FLAG_TITLES["b12_gray_zone"], text=" ".join(parts),
                                  source=source))
            need("active_B12", "B12 в серой зоне: нужен функциональный маркер дефицита.", "kr_b12_2024")
            need("homocysteine",
                 "B12 в серой зоне: повышается при функциональном дефиците B12."
                 + (" При сниженной рСКФ он повышается и без дефицита B12." if kidney_reduced else ""),
                 "kr_b12_2024")

        micro, hypo = _below(mcv, mcv_micro), _below(mch, mch_low)
        micro_not_iron = ((micro or hypo) and fer is not None and fer_ru is not None and fer >= float(fer_ru)
                          and not masked and not tsat_is_low)
        if micro_not_iron:
            indices = ", ".join(x for x in (f"MCV {q('MCV')}" if micro else "", f"MCH {q('MCH')}" if hypo else "") if x)
            iron_state = (f"ферритин {q('ferritin')}, НТЖ{tsat_mark} {q('TSAT')} — не снижены" if tsat is not None
                          else f"ферритин {q('ferritin')} — не снижен")
            lead = "Микроцитоз" if micro else "Гипохромия"
            unchecked = [short for code, short, _ in MICRO_CASE_SET if code not in norm.values]
            measured = [case_set_state(code, name, norm, cfg, refs) for code, _, name in MICRO_CASE_SET
                        if code in norm.values]
            case_parts = (["проверить " + ", ".join(unchecked)] if unchecked else []) + measured
            case_text = ("; в рамках набора кейса — " + "; ".join(case_parts)) if case_parts else ""
            text = (f"{lead} ({indices}) не объясняется дефицитом железа: {iron_state}. "
                    "Исключить талассемию и другие гемоглобинопатии (электрофорез гемоглобина), "
                    f"анемию хронических заболеваний, сидеробластную анемию{case_text}.")
            if mentzer is not None:
                phrase = mentzer_phrase(mentzer, cfg, iron_excluded=True)
                text += f" {phrase[:1].upper()}{phrase[1:]}."
            elif micro:
                text += " Индекс Ментцера не рассчитан: нет эритроцитов (RBC)."
            if inflamed:
                text += f" СРБ {q('CRP')} повышен: анемия хронических заболеваний вероятна."
            elif crp is None and fer_infl is not None and fer < float(fer_infl):
                text += (f" СРБ не сдан: при воспалении порог ферритина — {fmt_num(fer_infl)} мкг/л, "
                         "дефицит железа тогда не исключён.")
            source = f"пороги MCV и MCH — {src(indices_src)}"
            if mentzer is not None:
                source = f"{mentzer_source(cfg)}; {source}"
            out.flags.append(Flag(code="microcytosis_not_iron", title=FLAG_TITLES["microcytosis_not_iron"], text=text,
                                  source=source))

        if (fer is not None and crp is None and fer_who is not None and fer_infl is not None
                and float(fer_who) <= fer < float(fer_infl) and (anemia or micro_not_iron)):
            reason = (f"Нужен, чтобы правильно прочитать ферритин {q('ferritin')}: при воспалении дефицит железа вероятен "
                      f"при ферритине ниже {fmt_num(fer_infl)} мкг/л.")
            if "CRP" not in seen_tests and "CRP" in cfg.analytes:
                seen_tests.add("CRP")
                out.mandatory_tests.insert(0, {"analyte": "CRP", "reason": reason,
                                               "source": src(fer_sources.get("inflammation"))})
        if micro_not_iron:
            for analyte, name in (("vitamin_B6", "витамин B6"), ("copper", "медь"), ("ceruloplasmin", "церулоплазмин")):
                need(analyte, f"Микроцитоз не объясняется дефицитом железа: в рамках набора кейса проверить {name}.",
                     "project")

    if anemia:
        if "ferritin" not in norm.values:
            need("ferritin", "По клиническим рекомендациям при подозрении на ЖДА исследуют показатели обмена железа; "
                             "ферритин показывает запас железа.", "kr_ida_2024")
            need("CRP", "Нужен, чтобы правильно прочитать ферритин: при воспалении ферритин завышается.",
                 "who_ferritin_2020")
            if "TSAT" not in norm.measured:
                need("serum_iron", "Показатель обмена железа по клиническим рекомендациям; вместе с ОЖСС даёт "
                                   "насыщение трансферрина.", "kr_ida_2024")
                need("TIBC", "Показатель обмена железа по клиническим рекомендациям; вместе с железом сыворотки даёт "
                             "насыщение трансферрина.", "kr_ida_2024")
        if _above(mcv, mcv_macro):
            macro = (f"Анемия с макроцитозом (MCV {q('MCV')} выше {fmt_num(mcv_macro)} фл): "
                     "по клиническим рекомендациям исследуют")
            need("vitamin_B12", f"{macro} витамин B12.", "kr_b12_2024")
            need("folate", f"{macro} фолаты.", "kr_folate_2024")

    if not pregnant:
        out.hemolysis = assess_hemolysis(norm, cfg, refs, anemia)
    signal_set = {s.code for s in out.rule_signals}
    functional_only = signal_set == {"tsat_low_normal_ferritin"}
    if (not pregnant and anemia and (not signal_set or functional_only) and not out.flags
            and any(a in norm.measured for a in C.LABS)):
        items: list[ChecklistItem] = []

        def item(code: str, status: str, detail: str, analyte: str | None = None) -> None:
            items.append(ChecklistItem(code=code, title=CHECKLIST_TITLES[code], status=status, detail=detail,
                                       analyte=analyte))

        def ref_part(a: str) -> tuple[str, str]:
            plural, name = a in PLURAL_ANALYTES, LAB_REFERENCE_NAMES.get(a) or cfg.short_ru(a)
            if a not in norm.values:
                return "missing", f"{name} — {'не сданы' if plural else 'не сдан'}"
            ref = refs.get(a)
            if ref is None:
                status = LAB_REFERENCE_ONLY.replace("сдан", "сданы", 1) if plural else LAB_REFERENCE_ONLY
                return "no_ref", f"{name} {q(a)} — {status}"
            state = "low" if ref.below(norm.values[a]) else "high" if ref.above(norm.values[a]) else "within"
            return state, f"{name} {q(a)} — {ref_phrase(state, ref, cfg.unit_ru(a))}"

        if functional_only:
            text = next(s.text for s in out.rule_signals if s.code == "tsat_low_normal_ferritin")
            item("iron", "suspected", text, analyte="TSAT")
        elif fer is None:
            item("iron", "not_checked", "ферритин не сдан"
                 + (f"; НТЖ{tsat_mark} {q('TSAT')} не снижено" if tsat is not None and tsat_thr is not None else ""))
        elif fer_between:
            item("iron", "not_checked",
                 f"ферритин {q('ferritin')} — не ниже {fmt_num(fer_who)} мкг/л (ВОЗ 2020), но ниже {fmt_num(fer_ru)} "
                 "мкг/л (порог практики РФ): вывод зависит от выбранного порога"
                 + ("; СРБ не сдан" if crp is None else ""), analyte="ferritin")
        elif crp is None and fer_infl is not None and fer < float(fer_infl):
            item("iron", "not_checked",
                 f"по ферритину {q('ferritin')} дефицит не виден, но без СРБ вывод ненадёжен: "
                 f"при воспалении порог {fmt_num(fer_infl)} мкг/л")
        else:
            item("iron", "excluded", f"ферритин {q('ferritin')} не снижен"
                 + (f" (порог при воспалении {fmt_num(fer_infl)} мкг/л)" if inflamed and fer_infl is not None else ""))
        no_ru = f"{NO_RU_THRESHOLD_TEXT} — оценить по референсу лаборатории"
        if b12 is not None and b12_blocked:
            item("b12", "not_checked", f"сдан: {q('vitamin_B12')}; {no_ru}")
        elif b12 is None and ab12 is not None and ab12_blocked:
            item("b12", "not_checked", f"общий B12 не сдан; активный B12 сдан: {q('active_B12')}; {no_ru}")
        elif b12 is None and b12_blocked:
            item("b12", "not_checked", "не сдан")
        elif b12 is not None:
            item("b12", "excluded", f"B12 {q('vitamin_B12')} не снижен")
        elif ab12 is not None and _in_zone(ab12, ab12_zone, ab12_inclusive):
            item("b12", "not_checked",
                 f"активный B12 {q('active_B12')} — в серой зоне ({fmt_num(ab12_zone[0])}–{fmt_num(ab12_zone[1])} пмоль/л); "
                 "общий B12 не сдан")
        elif ab12 is not None and ab12_thr is not None:
            item("b12", "excluded", f"активный B12 {q('active_B12')} не снижен")
        else:
            item("b12", "not_checked", "B12 не сдан")
        if fol is None:
            item("folate", "not_checked", "фолаты не сданы")
        elif _in_zone(fol, fol_zone):
            item("folate", "suspected",
                 f"фолаты {q('folate')} — пограничный уровень ({fmt_num(fol_zone[0])}–{fmt_num(fol_zone[1])} нг/мл): "
                 "дефицит не исключён")
        else:
            item("folate", "excluded", f"фолаты {q('folate')} не снижены")
        if crp is None or crp_thr is None:
            item("inflammation", "not_checked", "СРБ не сдан"
                 + (f"; СОЭ {q('ESR')} — оценка по референсу лаборатории" if "ESR" in norm.values else ""))
        elif inflamed:
            item("inflammation", "suspected", f"СРБ {q('CRP')} выше {fmt_num(crp_thr)} мг/л: возможна анемия воспаления")
        else:
            item("inflammation", "excluded", f"СРБ {q('CRP')} не повышен")
        if egfr is None or egfr_low is None:
            item("kidney", "not_checked",
                 "рСКФ не рассчитана: проверьте креатинин" if "creatinine" in norm.values else "креатинин и рСКФ не сданы")
        elif _below(egfr, egfr_severe):
            item("kidney", "suspected", f"рСКФ {q('eGFR')}{egfr_mark} — ниже {fmt_num(egfr_severe)}: выраженное снижение")
        elif _below(egfr, egfr_low):
            item("kidney", "suspected", f"рСКФ {q('eGFR')}{egfr_mark} — ниже {fmt_num(egfr_low)}")
        else:
            item("kidney", "excluded", f"рСКФ {q('eGFR')}{egfr_mark} не снижена")
        if tsh is None or tsh_high is None:
            item("thyroid", "not_checked", "ТТГ не сдан")
        elif _above(tsh, tsh_marked):
            item("thyroid", "suspected", f"ТТГ {q('TSH')} выше {fmt_num(tsh_marked)} мМЕ/л: выраженное повышение")
        elif _above(tsh, tsh_high):
            item("thyroid", "suspected", f"ТТГ {q('TSH')} выше {fmt_num(tsh_high)} мМЕ/л")
        else:
            item("thyroid", "excluded", f"ТТГ {q('TSH')} не повышен")
        cu = v("copper")
        cu_state, cu_text = ref_part("copper")
        cer_state, cer_text = ref_part("ceruloplasmin")
        cu_low = cu_state == "low" or _below(cu, cu_thr)
        if cu_low and cu_state != "low":
            cu_text = f"медь {q('copper')} — ниже {fmt_num(cu_thr)} мкмоль/л (рабочий порог)"
        cer_low = cer_state == "low"
        cer_ctx = "; повышенный церулоплазмин — контекст воспаления или беременности, не самостоятельный признак" \
            if cer_state == "high" else ""
        if cu_low and cer_low:
            item("copper", "suspected", f"{cu_text}; {cer_text}: поддерживает дефицит меди", analyte="copper")
        elif cu_low:
            why = ("нормальный или повышенный церулоплазмин дефицит меди не исключает (белок острой фазы)"
                   if cer_state in ("within", "high") else "оценить медь вместе с церулоплазмином")
            item("copper", "not_checked", f"{cu_text}; {cer_text}: {why}{cer_ctx}", analyte="copper")
        elif cu is None or (cu_thr is None and cu_state in ("no_ref", "missing")):
            item("copper", "not_checked", f"медь не сдана; {cer_text}"
                 + ("; снижение церулоплазмина оценивается только вместе с медью" if cer_low else cer_ctx))
        elif cer_low:
            item("copper", "not_checked", f"{cu_text}; {cer_text} — при несниженной меди не специфично",
                 analyte="ceruloplasmin")
        else:
            item("copper", "excluded", f"{cu_text}; {cer_text}{cer_ctx}")
        hm = out.hemolysis
        if hm.found:
            item("hemolysis", "suspected", hm.detail(), analyte="haptoglobin" if "haptoglobin" in hm.signs else hm.signs[0])
        elif hm.level == "excluded":
            item("hemolysis", "excluded", "; ".join(t for _, t in hm.parts.values()))
        else:
            item("hemolysis", "not_checked", "; ".join(t for _, t in hm.parts.values()))
        out.checklist = items

        def lc(title: str) -> str:
            return title[:1].lower() + title[1:]

        def listed(status: str) -> str:
            return "; ".join(f"{lc(i.title)} — {i.detail}" for i in items if i.status == status)

        if not functional_only:
            parts = ["По сданным анализам дефицит не найден; причина анемии правилами не установлена."]
            if listed("suspected"):
                parts.append(f"Под подозрением: {listed('suspected')}.")
            not_checked = ", ".join(lc(i.title) for i in items if i.status == "not_checked")
            if not_checked:
                parts.append(f"Не проверено: {not_checked}.")
            excluded = ", ".join(lc(i.title) for i in items if i.status == "excluded")
            if excluded:
                parts.append(f"Исключено: {excluded}.")
            out.flags.append(Flag(code="unexplained_checklist", title=FLAG_TITLES["unexplained_checklist"],
                                  text=" ".join(parts), source=src("project")))

        status = {i.code: i.status for i in items}
        lead = "Чек-лист исключений" if functional_only else "Дефицит по сданным анализам не найден"
        if "ferritin" not in norm.values:
            need("ferritin", f"{lead}: запас железа не проверен.", "kr_ida_2024")
        elif fer_between and "TSAT" not in norm.values:
            for analyte in ("serum_iron", "TIBC"):
                need(analyte, "Ферритин между порогом ВОЗ и порогом практики РФ: насыщение трансферрина "
                              "(железо и ОЖСС) уточнит, есть ли дефицит железа.", "kr_ida_2024")
        if status["b12"] != "excluded":
            need("vitamin_B12", f"{lead}: витамин B12 не сдан.", "kr_b12_2024")
        need("folate", f"{lead}: фолаты не проверены.", "kr_folate_2024")
        need("CRP", "Чтобы исключить анемию воспаления и правильно прочитать ферритин.", "who_ferritin_2020")
        if egfr is None:
            need("creatinine", "Чтобы оценить функцию почек: по креатинину рассчитывается рСКФ (CKD-EPI 2021).",
                 "kr_ida_2024")
        need("TSH", "Чтобы исключить гипотиреоз как причину анемии.", "project")
        if status["hemolysis"] == "not_checked":
            need("LDH", "Чтобы исключить гемолиз: ЛДГ и гаптоглобин сравниваются с референсом.", "project_reference")
            need("haptoglobin", "Чтобы исключить гемолиз: ЛДГ и гаптоглобин сравниваются с референсом.",
                 "project_reference")
        need("reticulocytes", "Чтобы оценить ответ костного мозга и исключить гемолиз.", "kr_ida_2024")

    hm = out.hemolysis
    if hm is not None and (hm.found or hm.level == "signs") and not any(i.code == "hemolysis" for i in out.checklist):
        if hm.found:
            title = "Сильное подозрение на гемолиз" if hm.level == "strong" else "Подозрение на гемолиз"
            text = hm.detail(lead=False)
            out.flags.append(Flag(code="hemolysis_suspected", title=title, text=text[:1].upper() + text[1:] + ".",
                                  source=src("hemolysis_rule")))
        else:
            text = hm.detail()
            out.flags.append(Flag(code="hemolysis_signs", title="Есть признаки, требующие проверки",
                                  text=text[:1].upper() + text[1:] + ".", source=src("hemolysis_rule"),
                                  severity="info"))
    if hm is not None and (hm.found or hm.level == "signs"):
        for a in hm.missing:
            need(a, "Признаки гемолиза: досдать недостающий показатель и сопоставить с ОАК и мазком крови.",
                 "hemolysis_rule")

    return out
