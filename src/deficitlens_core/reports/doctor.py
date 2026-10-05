from __future__ import annotations

from .. import constants as C
from ..config import Config
from ..level1 import hemoglobin_rule
from ..normalize import fmt_num
from ..rules import (MICRO_CASE_SET, ThresholdDeficit, foreign_blocked, mentzer_index, mentzer_phrase, mentzer_source,
                     threshold_deficits)
from ..schemas import (ChecklistItem, Explanation, Flag, HiddenDeficiency, Level1, Level2, NextTest, Report,
                       ReportSection, ValueRow)
from ..values_table import short_source
from . import DISCLAIMER, TREATMENT_LINE, Lines, Texts, cap, decap, pct, sentence

DEFAULT_TITLES = {
    "sections.level1.title": "Уровень 1: анемия по ВОЗ",
    "sections.level2.title": "Уровень 2: группа и причина",
    "sections.hidden.title": "Скрытый дефицит",
    "sections.flags.title": "На что обратить внимание",
    "sections.checklist.title": "Чек-лист исключений",
    "sections.next_tests.title": "Что досдать",
    "sections.why.title": "Почему такой вывод",
    "sections.tactics.title": "Тактика",
    "sections.limitations.title": "Ограничения",
}
CHECKLIST_ANALYTE = {"iron": "ferritin", "b12": "vitamin_B12", "folate": "folate", "inflammation": "CRP",
                     "kidney": "eGFR", "thyroid": "TSH", "copper": "copper"}
CAUSES_WITHOUT_NAME = ("undetermined", "none")
CONFIDENCE_CAP_MARK = "Уверенность понижена"
NO_DEFICIT_CLASSES = ("anemia_other",)
THRESHOLD_NAMES = {"ferritin": "ферритин", "vitamin_B12": "витамин B12", "folate": "фолаты"}
THRESHOLD_MEANING = {"iron": "дефицит железа", "b12": "дефицит витамина B12", "folate": "дефицит фолатов"}

def deficit_text(d: ThresholdDeficit, T: Texts, cfg: Config) -> str:
    unit = cfg.unit_ru(d.analyte)
    name = T.t(f"threshold.names.{d.analyte}") or THRESHOLD_NAMES.get(d.analyte, decap(cfg.short_ru(d.analyte)))
    meaning = T.t(f"threshold.meaning.{d.nutrient}") or THRESHOLD_MEANING.get(d.nutrient, "")
    caveat = (T.t("threshold.crp_missing") or "; СРБ не сдан") if d.crp_missing else ""
    source = short_source(cfg, d.source_key)
    if source.endswith(")") and " (" in source:
        source = source[:-1].replace(" (", ", ", 1)
    return (T.t("threshold.item", name=name, value=fmt_num(d.value), unit=unit, thr=fmt_num(d.threshold),
                source=source, meaning=meaning, caveat=caveat)
            or f"{name} {fmt_num(d.value)} {unit} ниже {fmt_num(d.threshold)} {unit} — {meaning}{caveat}")

def open_checklist_items(checklist: list[ChecklistItem], T: Texts) -> str:
    parts = []
    for status in ("suspected", "not_checked"):
        titles = [decap(i.title) for i in checklist if i.status == status]
        if titles:
            parts.append(f"{', '.join(titles)} — {T.t(f'sections.checklist.status.{status}') or status}")
    return "; ".join(parts)

def _iron_not_assessed(norm, hidden: HiddenDeficiency) -> bool:
    screening = hidden.screening
    return "ferritin" not in norm.measured and not (screening is not None and screening.applicable)

def informative_cause(level2: Level2) -> str:
    cause = level2.deficiency_cause
    if not cause or cause in CAUSES_WITHOUT_NAME or level2.completeness == "cbc":
        return ""
    possible = {C.PROFILE_TO_CAUSE[p] for p in C.PROFILES if C.PROFILE_TO_CLASS[p] == level2.anemia_class}
    text = (level2.deficiency_cause_ru or "").strip()
    if len(possible) <= 1 or text.lower() == (level2.anemia_class_ru or "").strip().lower():
        return ""
    return text.replace(": ", " — ")

def split_note(note: str | None) -> tuple[str, str]:
    note = (note or "").strip()
    idx = note.find(CONFIDENCE_CAP_MARK)
    if idx < 0:
        return note, ""
    return note[:idx].strip(), note[idx:].strip()

def suspected_items(checklist: list[ChecklistItem], values: list[ValueRow], cfg: Config) -> list[str]:
    rows = {r.analyte: r for r in values}
    out: list[str] = []
    for item in checklist:
        if item.status != "suspected":
            continue
        row = rows.get(item.analyte or CHECKLIST_ANALYTE.get(item.code, ""))
        if row is not None:
            short = f"{decap(cfg.short_ru(row.analyte))} {fmt_num(row.value)} {row.unit}".strip()
        else:
            short = item.detail
            for sep in (" — ", ":", " ("):
                short = short.split(sep)[0]
            short = short.strip()
        title = item.title[:1].lower() + item.title[1:]
        out.append(f"{title} ({short})" if short else title)
    return out

def build_doctor_report(*, norm, level1: Level1, level2: Level2, hidden: HiddenDeficiency, flags: list[Flag],
                        checklist: list[ChecklistItem], next_tests: list[NextTest], explanation: Explanation,
                        values: list[ValueRow], cfg: Config) -> Report:
    T = Texts((cfg.texts or {}).get("doctor"))
    hb, thr = fmt_num(level1.hemoglobin), fmt_num(level1.threshold_g_l)
    rule = hemoglobin_rule(norm, cfg)
    pregnant = norm.pregnancy_status == "yes"
    note_rest, cap_phrase = split_note(explanation.note)
    deficits = threshold_deficits(norm, cfg) if level1.anemia else []
    variant_items = ([(c.code, c.name_ru) for c in level2.plausible]
                     or ([(level2.anemia_class, level2.anemia_class_ru)] if level2.anemia_class_ru else []))
    if deficits:
        variant_items = [(code, name) for code, name in variant_items if code not in NO_DEFICIT_CLASSES]
    variants = [name for _, name in variant_items]
    sections: list[ReportSection] = []

    def section(title_path: str, lines: Lines) -> None:
        if lines:
            sections.append(ReportSection(title=T.t(title_path) or DEFAULT_TITLES.get(title_path, title_path),
                                          lines=list(lines)))

    head = Lines()
    if level1.anemia:
        head.add(T.t("headline.anemia", severity=cap(level1.severity_ru or ""), hb=hb, thr=thr))
        if deficits:
            head.add(T.t("headline.threshold", items="; ".join(deficit_text(d, T, cfg) for d in deficits)))
        if level2.enabled and level2.confidence == "low":
            if not deficits:
                head.add(T.t("headline.level2_low", variants=", ".join(variants)))
            elif variants:
                head.add(T.t("headline.level2_low_threshold", variants=", ".join(variants)))
            else:
                head.add(T.t("headline.level2_low_threshold_plain"))
        elif level2.enabled and deficits and level2.anemia_class in NO_DEFICIT_CLASSES:
            head.add(T.t("headline.level2_conflict", cls=level2.anemia_class_ru or "",
                         confidence=level2.confidence_ru or ""))
        elif level2.enabled:
            cause = informative_cause(level2)
            head.add(T.t("headline.level2_cause" if cause else "headline.level2", cls=level2.anemia_class_ru or "",
                         cause=cause, confidence=level2.confidence_ru or ""))
        head.add(sentence(level2.class_note_ru or ""))
    else:
        head.add(T.t("headline.no_anemia", hb=hb, thr=thr))
        if hidden.applicable:
            screening = hidden.screening
            if hidden.detected and hidden.rule_signals:
                head.add(T.t("headline.hidden_signals"))
            elif hidden.detected and screening is not None and screening.applicable and screening.recommend_ferritin:
                head.add(T.t("headline.hidden_screening", risk=screening.risk_ru or "повышенный"))
            elif hidden.detected:
                head.add(T.t("headline.hidden_model"))
            elif _iron_not_assessed(norm, hidden):
                head.add(T.t("headline.hidden_not_assessed"))
            else:
                head.add(T.t("headline.hidden_none"))
    if pregnant:
        head.add(T.t("headline.pregnancy"))
    if flags:
        head.add(T.t("headline.flags", titles="; ".join(decap(f.title) for f in flags)))
    suspected = suspected_items(checklist, values, cfg)
    if suspected:
        head.add(T.t("headline.suspected", items="; ".join(suspected)))
    headline = " ".join(head) or f"Hb {hb} г/л; порог ВОЗ {thr} г/л."
    if level1.urgent:
        headline = T.t("headline.urgent_prefix") + headline

    s1 = Lines()
    if level1.anemia:
        s1.add(T.t("sections.level1.anemia", hb=hb, thr=thr, context=rule["context"]))
        bands = rule["bands"]
        s1.add(T.t("sections.level1.severity", severity=level1.severity_ru or "",
                   mild_from=fmt_num(bands["mild"][0]), mild_to=fmt_num(bands["mild"][1]),
                   moderate_from=fmt_num(bands["moderate"][0]), moderate_to=fmt_num(bands["moderate"][1]),
                   severe_below=fmt_num(bands["severe_below"])))
    else:
        s1.add(T.t("sections.level1.no_anemia", hb=hb, thr=thr, context=rule["context"]))
    s1.add(T.t("sections.level1.source", source=level1.source))
    if level1.note:
        s1.add(T.t("sections.level1.note", note=level1.note) or level1.note)
    for item in level1.urgent:
        s1.add(T.t("sections.level1.urgent", text=sentence(decap(item.text)), source=item.source))
    section("sections.level1.title", s1)

    s2 = Lines()
    if not level2.enabled:
        s2.add(sentence(T.t("sections.level2.disabled", reason=decap(level2.disabled_reason or "причина не указана"))))
    else:
        cbc_no_class = not level1.anemia and level2.completeness == "cbc"
        cls, cause = level2.anemia_class_ru or "", level2.deficiency_cause_ru or ""
        if level1.anemia:
            if level2.confidence == "low":
                s2.add(T.t("sections.level2.class_low", cls=cls))
            elif level2.case_group_note_ru:
                s2.add(T.t("sections.level2.class_plain", cls=cls, cause=""))
            elif level2.deficiency_cause in CAUSES_WITHOUT_NAME or not cause:
                s2.add(T.t("sections.level2.class_plain", cls=cls, cause=sentence(cap(cause))))
            else:
                s2.add(T.t("sections.level2.class_cause", cls=cls, cause=cause.replace(": ", " — ")))
            s2.add(T.t("sections.level2.group", group=level2.case_group_ru or ""))
            s2.add(sentence(level2.case_group_note_ru or ""))
            s2.add(sentence(level2.class_note_ru or ""))
        elif cbc_no_class:
            s2.add(T.t("sections.level2.cbc_only_no_class"))
            if level2.plausible:
                s2.add(T.t("sections.level2.variants", items="; ".join(variants)))
        else:
            s2.add(T.t("sections.level2.no_anemia_class", group=level2.case_group_ru or "", cls=cls, cause=cause))
        if not level1.anemia and "ferritin" not in norm.measured:
            s2.add(T.t("sections.level2.no_anemia_screening"))
        if not cbc_no_class:
            groups_ru = cfg.class_mapping.get("groups5", {})
            groups = sorted((level2.probabilities or {}).get("groups5", {}).items(), key=lambda kv: -kv[1])
            shown = [f"{groups_ru.get(code, code)} — {pct(p)} %" for code, p in groups if p >= 0.005]
            if shown and level1.anemia:
                s2.add(T.t("sections.level2.probabilities", items="; ".join(shown)))
            if level2.plausible:
                items = "; ".join(f"{c.name_ru} — {pct(c.p)} %" for c in level2.plausible)
                s2.add(T.t("sections.level2.plausible", items=items))
            if level2.ambiguous:
                s2.add(T.t("sections.level2.ambiguous"))
            s2.add(T.t("sections.level2.confidence", confidence=level2.confidence_ru or "—",
                       completeness=level2.completeness_ru or "—"))
            if level2.confidence == "low":
                key = "low_confidence_threshold" if deficits else "low_confidence"
                s2.add(T.t(f"sections.level2.{key}"))
        else:
            s2.add(T.t("sections.level2.completeness_only", completeness=level2.completeness_ru or "—"))
        if cap_phrase:
            s2.add(sentence(cap_phrase))
        if level2.mode:
            s2.add(T.t(f"sections.level2.mode.{level2.mode}"))
        s2.add(T.t("sections.level2.training_note"))
    section("sections.level2.title", s2)

    sh = Lines()
    if hidden.applicable:
        if hidden.rule_signals:
            sh.add(T.t("sections.hidden.signals_intro") or sentence(hidden.summary_ru))
        else:
            sh.add(sentence(hidden.summary_ru))
        for sig in hidden.rule_signals:
            sh.add(T.t("sections.hidden.signal", text=cap(sig.text.strip().rstrip(".")), source=sig.source))
        screening = hidden.screening
        if screening is not None:
            if screening.applicable:
                sh.add(T.t("sections.hidden.screening", model=screening.model or "модель скрининга",
                           lab_reference=screening.lab_reference or "default", risk=screening.risk_ru or "—",
                           p15=pct(screening.p_ferritin_lt15), p30=pct(screening.p_ferritin_lt30)))
                if screening.explanation_ru:
                    sh.add(sentence(screening.explanation_ru))
            else:
                reason = decap(screening.reason_not_applicable or "")
                sh.add(sentence(T.t("sections.hidden.screening_off", reason=reason)))
        if not hidden.detected and _iron_not_assessed(norm, hidden):
            sh.add(T.t("sections.hidden.not_assessed"))
        if hidden.p_any is not None:
            nutrients_ru = cfg.class_mapping.get("nutrients", {})
            items = ", ".join(f"{nutrients_ru.get(n, n)} — {pct(p)} %" for n, p in (hidden.by_nutrient or {}).items())
            by = T.t("sections.hidden.by_nutrient", items=items) if items else ""
            sh.add(T.t("sections.hidden.case_model", p_any=pct(hidden.p_any), by_nutrient=by))
    section("sections.hidden.title", sh)

    sf = Lines()
    for flag in flags:
        sf.add(T.t("sections.flags.item", title=flag.title, text=sentence(flag.text), source=flag.source))
    section("sections.flags.title", sf)

    sc = Lines()
    for item in checklist:
        status = T.t(f"sections.checklist.status.{item.status}") or item.status
        sc.add(T.t("sections.checklist.item", title=item.title, status=status, detail=item.detail.rstrip(".")))
    section("sections.checklist.title", sc)

    sn = Lines()
    for test in next_tests:
        extra = "" if test.available else " " + T.t("sections.next_tests.unavailable")
        source = T.t("sections.next_tests.source", source=test.source.rstrip(".")) if test.source else ""
        sn.add(T.t("sections.next_tests.item", name=test.name_ru, reason=sentence(decap(test.reason)), extra=extra,
                   source=source))
    if next_tests:
        sn.add(T.t("sections.next_tests.where"))
    else:
        sn.add(T.t("sections.next_tests.none"))
    section("sections.next_tests.title", sn)

    sw = Lines()
    for c in explanation.items_for:
        sw.add(T.t("sections.why.item_for", name=c.name_ru, value=fmt_num(c.value), unit=c.unit, text=c.text,
                   weight=fmt_num(c.weight, 2)))
    for c in explanation.items_against:
        sw.add(T.t("sections.why.item_against", name=c.name_ru, value=fmt_num(c.value), unit=c.unit, text=c.text,
                   weight=fmt_num(c.weight, 2)))
    if note_rest:
        sw.add(sentence(note_rest))
    if not explanation.items_for and not explanation.items_against:
        sw.add(T.t("sections.why.level1_only"))
    deviations = [r for r in values if r.norm.status in ("low", "high", "borderline") and r.analyte != "hemoglobin"]
    if deviations:
        sw.add(T.t("sections.why.deviations_title"))
        for r in deviations:
            sw.add(T.t("sections.why.deviation", name=r.name_ru, value=fmt_num(r.value), unit=r.unit,
                       threshold=r.norm.threshold_text.rstrip(".")))
    else:
        sw.add(T.t("sections.why.no_deviations"))
    mentzer = mentzer_index(norm, cfg)
    if mentzer is not None and not pregnant and not any(f.code == "microcytosis_not_iron" for f in flags):
        sw.add(T.t("sections.why.mentzer", phrase=cap(mentzer_phrase(mentzer, cfg)), source=mentzer_source(cfg)))
    unknown = [r.name_ru for r in values if r.norm.status == "unknown"]
    if unknown:
        sw.add(T.t("sections.why.unknown", names=", ".join(decap(n) for n in unknown)))
    section("sections.why.title", sw)

    st = Lines()
    if level1.urgent:
        reasons = "; ".join(decap(u.text.split(":")[0].strip()) for u in level1.urgent)
        st.add(T.t("sections.tactics.urgent", reasons=reasons))
    if pregnant:
        st.add(T.t("sections.tactics.pregnancy"))
    for flag in flags:
        if flag.code == "unexplained_checklist":
            items = open_checklist_items(checklist, T)
            st.add(T.t("sections.tactics.flags.unexplained_checklist", items=items) if items
                   else T.t("sections.tactics.flags.unexplained_checklist_closed"))
        elif flag.code == "microcytosis_not_iron":
            rest = [short for code, short, _ in MICRO_CASE_SET if code not in norm.values]
            st.add(T.t("sections.tactics.flags.microcytosis_not_iron", rest=("; " + ", ".join(rest)) if rest else ""))
        else:
            st.add(T.t(f"sections.tactics.flags.{flag.code}"))
    if next_tests:
        st.add(T.t("sections.tactics.workup", names="; ".join(decap(t.name_ru) for t in next_tests)))
    elif hidden.applicable and hidden.detected:
        st.add(T.t("sections.tactics.hidden_detected"))
    elif not flags:
        st.add(T.t("sections.tactics.no_workup"))
    st.append(T.t("treatment_line") or TREATMENT_LINE)
    section("sections.tactics.title", st)

    sl = Lines()
    refs = getattr(norm, "references", None)
    if refs is not None and refs.local:
        from ..references import local_warning
        sl.add(local_warning(cfg, refs))
    for line in T.raw("sections.limitations.lines", []) or []:
        sl.add(line)
    foreign_keys = [k for k, e in cfg.norms.items() if isinstance(e, dict) and e.get("origin") == "foreign"]
    if foreign_keys:
        blocked = any(foreign_blocked(cfg, k) for k in foreign_keys)
        sl.add(T.t("sections.limitations.foreign_off" if blocked else "sections.limitations.foreign_on"))
    if pregnant:
        sl.add(T.t("sections.limitations.pregnancy"))
    for note in norm.notes:
        sl.add(T.t("sections.limitations.input_note", text=sentence(note.text)) or sentence(note.text))
    sl.append(T.t("disclaimer") or DISCLAIMER)
    section("sections.limitations.title", sl)

    return Report(headline=headline, sections=sections)
