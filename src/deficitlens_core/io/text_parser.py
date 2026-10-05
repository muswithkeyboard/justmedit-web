from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Config
from . import lab_blank as lb

MAX_TEXT_CHARS = 20000
MAX_LINES = 400
MAX_UNRECOGNIZED = lb.MAX_UNRECOGNIZED
UNRECOGNIZED_WIDTH = lb.UNRECOGNIZED_WIDTH

_RANGE_DASH = r"(?:-|–|—|−|‑|\.\.\.?|…)"
_NUMBER = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё^.,/])([<>≤≥]\s*)?"
    rf"(\d{{1,3}}(?:{lb.THOUSANDS_SEP}\d{{3}})+(?:[.,]\d+)?(?![\d.,]|\s*/|\s*{_RANGE_DASH}\s*\d)|\d+(?:[.,]\d+)?)"
    r"(?![0-9]*[A-Za-zА-Яа-яЁё]{2,}\d)")
_DATE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b")
_LIST_MARKER = re.compile(r"^\s*(?:\d{1,2}[.)]\s+|[-–—•*·]\s*)")
_HAS_DIGIT = re.compile(r"\d")
_ARROW_NUMBER = re.compile(r"^\s*(?:→|⟶|➔|➜|➝|⇒|->|=>|>>)\s*[<>≤≥]?\s*\d")

def _to_float(number: str) -> float:
    return float(re.sub(lb.THOUSANDS_SEP, "", number).replace(",", "."))

@dataclass
class _Split:
    name: str
    value: float | None
    sign: str | None
    rest: str
    head_range: tuple[float | None, float | None] | None
    raw: str = ""

def _split_value(line: str) -> _Split | None:
    m = _NUMBER.search(line)
    if m is None:
        return None
    r = lb.RANGE.match(line, m.start())
    if r is not None and (r.group("a") is not None or r.group("c") is not None):
        m2 = _NUMBER.search(line, r.end())
        if m2 is not None and lb.RANGE.match(line, m2.start()) is None and not lb.POW_UNIT.match(line, m2.start()):
            return _Split(name=line[:m.start()], value=_to_float(m2.group(2)), sign=m2.group(1),
                          rest=line[m2.end():], head_range=lb.range_from_match(r), raw=m2.group(2))
        return _Split(name=line[:m.start()], value=None, sign=None, rest=line[r.end():], head_range=None)
    return _Split(name=line[:m.start()], value=_to_float(m.group(2)), sign=m.group(1), rest=line[m.end():],
                  head_range=None, raw=m.group(2))

def second_result(rest: str) -> bool:
    if _ARROW_NUMBER.match(rest):
        return True
    masked = lb.POW_UNIT.sub(lambda m: " " * len(m.group(0)), rest)
    s = lb.strip_flags(masked)
    pos = len(s) - len(s.lstrip())
    if _NUMBER.match(s, pos) is None:
        return False
    return lb.RANGE.match(s, pos) is None and _DATE.match(s, pos) is None

def _match(name: str, index) -> tuple[lb.Target | None, str | None]:
    t = lb.match_name(name, index)
    if t is None:
        return None, None
    _, unit = lb.split_name_unit(lb.clean_name(name))
    return t, unit

def _masked_rest(rest: str) -> tuple[str, re.Match[str] | None, tuple | None]:
    masked = rest
    pow_m = lb.POW_UNIT.search(masked)
    if pow_m:
        masked = masked[:pow_m.start()] + " " * (pow_m.end() - pow_m.start()) + masked[pow_m.end():]
    rng = None
    found = lb.find_range(masked)
    if found is not None:
        rng, (a, b) = found
        masked = masked[:a] + " " * (b - a) + masked[b:]
    return lb.strip_flags(masked), pow_m, rng

def _unit_and_range(rest: str, spec: dict) -> tuple[str | None, bool, tuple | None]:
    masked, pow_m, rng = _masked_rest(rest)
    if pow_m:
        unit, unknown = lb.unit_in_rest(pow_m.group(0), spec)
    else:
        unit, unknown = lb.unit_in_rest(masked, spec)
    return unit, unknown, rng

def unit_text(rest: str) -> str:
    masked, pow_m, _ = _masked_rest(rest)
    return pow_m.group(0) if pow_m and not masked.strip() else masked

def _sections(prepared: list[str]) -> tuple[list[str | None], set[int]]:
    out: list[str | None] = []
    headers: set[int] = set()
    current: str | None = None
    for i, line in enumerate(prepared):
        sec = lb.section_of(line) if line else None
        if sec is not None:
            current = None if sec == "кровь" else sec
            headers.add(i)
        out.append(current)
    return out, headers

def _parse_lines(lines: list[str], col: lb.Collector, index, *, report_no_number: bool, quiet: bool) -> None:
    prepared = [" ".join(_LIST_MARKER.sub("", raw).split()) for raw in lines]
    splits = [_split_value(line) if line else None for line in prepared]
    sections, headers = _sections(prepared)
    consumed: set[int] = set(headers)
    pending: list[int] = []

    def free_name_line(j: int) -> bool:
        return 0 <= j < len(prepared) and j not in consumed and bool(prepared[j]) and splits[j] is None

    for i, line in enumerate(prepared):
        if i in consumed:
            pending = [] if i in headers else pending
            continue
        if not line:
            pending = []
            continue
        sp = splits[i]
        if sp is None:
            pending = (pending + [i])[-2:]
            continue
        where, order = f"строка {i + 1}", (1, i + 1)
        own = sp.name
        cands: list[tuple[list[int], str]] = [([], own)]
        pend = [j for j in pending if free_name_line(j)]
        if pend:
            cands.append(([pend[-1]], prepared[pend[-1]] + " " + own))
        if len(pend) == 2:
            cands.append((pend, prepared[pend[0]] + " " + prepared[pend[1]] + " " + own))
        if free_name_line(i + 1):
            cands.append(([i + 1], own + " " + prepared[i + 1]))
            if pend:
                cands.append(([pend[-1], i + 1], prepared[pend[-1]] + " " + own + " " + prepared[i + 1]))
        pending = []
        best: tuple[int, lb.Target, list[int], str | None, str] | None = None
        for used, name in cands:
            if not name.strip():
                continue
            t, name_unit = _match(name, index)
            if t is not None and (best is None or len(used) > best[0]):
                best = (len(used), t, used, name_unit, name)
        if best is None:
            material = sections[i] or lb.material_of(sp.name)
            if material:
                col.ignore(lb.material_display(lb.material_target(sp.name, index), col.cfg, material), order,
                           where, quiet=quiet or bool(_DATE.search(line)))
            elif not quiet and not _DATE.search(line):
                col.remember(order, where, "название показателя не распознано")
            continue
        _, target, used, name_unit, name_text = best
        consumed.update(used)
        material = sections[i] or lb.material_of(name_text)
        if material:
            col.ignore(lb.material_display(target, col.cfg, material), order, where, quiet=quiet)
            continue
        target = lb.reroute(target, name_text, unit_text(sp.rest) if sp.value is not None else "")
        if target.kind == "ignored":
            col.ignore(target.code, order, where, quiet=quiet)
            continue
        code = target.code
        spec = col.cfg.analytes[code]
        if lb.foreign_unit(unit_text(sp.rest)) or (name_unit is not None and lb.foreign_unit(name_unit)):
            col.foreign(code, order, where)
            continue
        if sp.value is None:
            col.remember(order, where, f"{col.name(code)} — нет числа")
            continue
        if second_result(sp.rest):
            col.multiple(code, order, where)
            continue
        unit, unknown, rng = _unit_and_range(sp.rest, spec)
        if unit is None and not unknown and name_unit:
            unit, unknown = lb.unit_for_cell(name_unit, spec)
        col.add(code, sp.value, sign=sp.sign, unit=unit, unit_unknown=unknown, rng=sp.head_range or rng,
                order=order, where=where, lineno=i + 1, raw=sp.raw,
                method=lb.esr_method(name_text) if code == "ESR" else None)

    for i, line in enumerate(prepared):
        if i in consumed or not line or splits[i] is not None or quiet:
            continue
        if sections[i] or lb.material_of(line):
            continue
        if not (report_no_number or _HAS_DIGIT.search(line)):
            continue
        code = lb.prefix_analyte(line, index)
        if code is not None:
            col.remember((1, i + 1), f"строка {i + 1}", f"{col.name(code)} — нет числа")

def parse_lab_text(text: str, cfg: Config, *, line_numbers: bool = False, report_no_number: bool = False,
                   extended: bool = False, tables: list | None = None, table_above: list | None = None) -> dict:
    if not isinstance(text, str):
        raise ValueError("text должен быть строкой")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"текст длиннее {MAX_TEXT_CHARS} символов: вставьте только строки с результатами анализов")
    col = lb.Collector(cfg, extended=extended)
    index = lb.name_index(cfg)
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        col.notes.append(f"Разобраны первые {MAX_LINES} строк.")
        lines = lines[:MAX_LINES]
    table_lines: list[str] = []
    if tables:
        from .lab_tables import parse_tables
        leftover, table_lines = parse_tables(tables, col, index, above=table_above)
        lines = lines + leftover
    _parse_lines(lines, col, index, report_no_number=report_no_number, quiet=bool(tables) and bool(col.values))
    date = (lb.find_date([*lines, *table_lines]) or col.date_hint) if extended else None
    return col.result(date=date, line_numbers=line_numbers)

def parse_value_cell(value_text: str) -> tuple[float | None, str | None, str, str] | None:
    text = str(value_text or "")
    m = _NUMBER.search(text)
    if m is None:
        return None
    r = lb.RANGE.match(text, m.start())
    if r is not None and (r.group("a") is not None or r.group("c") is not None):
        return None, None, text[r.end():], ""
    return _to_float(m.group(2)), m.group(1), text[m.end():], m.group(2)

def unit_and_range(rest: str, spec: dict) -> tuple[str | None, bool, tuple | None]:
    return _unit_and_range(rest, spec)
