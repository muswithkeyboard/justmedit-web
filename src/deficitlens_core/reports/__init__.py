from __future__ import annotations

from typing import Any, Iterable

from ..config import Config
from ..schemas import (ChecklistItem, Explanation, Flag, HiddenDeficiency, Level1, Level2, NextTest, Report, Reports,
                       ReportSection, ValueRow)

DISCLAIMER = "Исследовательский прототип, не медицинское изделие. Решение принимает врач."
TREATMENT_LINE = ("Лечение — по клиническим рекомендациям Минздрава России (ID 669, 536, 540); "
                  "схемы и дозы прототип не приводит.")
NEUTRAL_PATIENT_LINE = "Обсудите этот результат с врачом."

class _Safe(dict):

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"

class Texts:

    def __init__(self, data: Any):
        self._data = data if isinstance(data, dict) else {}

    def raw(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def t(self, path: str, **kw: Any) -> str:
        template = self.raw(path)
        if not isinstance(template, str):
            return ""
        try:
            return template.format_map(_Safe(kw))
        except (ValueError, IndexError, KeyError, AttributeError):
            return template

class Lines(list):

    def add(self, text: Any) -> None:
        text = " ".join(str(text or "").split())
        if text:
            self.append(text)

def cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text

def decap(text: str) -> str:
    if len(text) >= 2 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text

def sentence(text: str) -> str:
    text = (text or "").strip()
    if text and text[-1] not in ".!?…":
        text += "."
    return text

def pct(p: float | None) -> str:
    if p is None:
        return "—"
    value = 100.0 * float(p)
    if 0 < value < 1:
        return "<1"
    return str(int(round(value)))

def _norm_for_filter(text: Any) -> str:
    return str(text).lower().replace("ё", "е")

def filter_patient_line(line: Any, forbidden: Iterable[str], neutral: str = NEUTRAL_PATIENT_LINE) -> str:
    try:
        low = _norm_for_filter(line)
        for word in forbidden or ():
            if word is None:
                continue
            w = _norm_for_filter(word)
            if w and w in low:
                return neutral
        return str(line)
    except Exception:
        return neutral

def filter_patient_lines(lines: Iterable[Any], forbidden: Iterable[str],
                         neutral: str = NEUTRAL_PATIENT_LINE) -> list[str]:
    forbidden = list(forbidden or ())
    out: list[str] = []
    for line in lines:
        safe = filter_patient_line(line, forbidden, neutral)
        if safe == neutral and out and out[-1] == neutral:
            continue
        out.append(safe)
    return out

def build_reports(*, norm, level1: Level1, level2: Level2, hidden: HiddenDeficiency, flags: list[Flag],
                  checklist: list[ChecklistItem], next_tests: list[NextTest], explanation: Explanation,
                  values: list[ValueRow], cfg: Config) -> Reports:
    from .doctor import build_doctor_report
    from .patient import build_patient_report

    kwargs = dict(norm=norm, level1=level1, level2=level2, hidden=hidden, flags=flags or [], checklist=checklist or [],
                  next_tests=next_tests or [], explanation=explanation or Explanation(), values=values or [], cfg=cfg)
    return Reports(doctor=build_doctor_report(**kwargs), patient=build_patient_report(**kwargs))

__all__ = ["build_reports", "filter_patient_line", "filter_patient_lines", "Texts", "Lines",
           "DISCLAIMER", "TREATMENT_LINE", "NEUTRAL_PATIENT_LINE", "Report", "ReportSection"]
