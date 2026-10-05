from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from ..config import Config, config_dir
from ..normalize import unit_key

MAX_UNRECOGNIZED = 30
UNRECOGNIZED_WIDTH = 60

_HOMOGLYPHS = str.maketrans("авекмнорстух", "abekmhopctyx")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_EDGE_PUNCT = " .:;|*!•·…=_/\\-"
_PARENS = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_OPEN_TAIL = re.compile(r"[(\[][^)\]]*$")
_CAPS_TOKEN = re.compile(r"^[A-Z][A-Z0-9#%+\-]{1,7}$")

_FLAG_PHRASES = re.compile(
    r"отклонени[ея]\s+от(?:\s+нормы)?|(?:ниже|выше)\s+нормы|в\s+пределах\s+нормы|\(?\s*числовой\s+результат\s*\)?"
    r"|критическ\w*\s+значени\w*|нормы\b", re.IGNORECASE)
_FLAG_TOKENS = re.compile(r"(?<!\S)(?:[↑↓▲▼⇑⇓↗↘]+|\*{1,3}|!{1,3}|HH?|LL?|\+{1,2})(?!\S)")
_ARROWS = re.compile(r"[↑↓▲▼⇑⇓↗↘]")

class Target(NamedTuple):
    kind: str
    code: str

def strip_flags(text: str) -> str:
    text = _FLAG_PHRASES.sub(" ", str(text))
    text = _ARROWS.sub(" ", text)
    return _FLAG_TOKENS.sub(" ", text)

def name_key(s: Any) -> str:
    s = str(s).casefold().replace("ё", "е").translate(_DASHES)
    s = re.sub(r"\s*-\s*", "-", s)
    s = " ".join(s.replace(",", " ").split())
    return s.strip(_EDGE_PUNCT).translate(_HOMOGLYPHS)

def split_parens(text: str) -> tuple[str, list[str]]:
    items: list[str] = []

    def take(m: re.Match[str]) -> str:
        items.extend(p.strip() for p in re.split(r"[,;/]", m.group(1)) if p.strip())
        return " "

    base = str(text)
    for _ in range(3):
        new = _PARENS.sub(take, base)
        if new == base:
            break
        base = new
    base = _OPEN_TAIL.sub(" ", base)
    return " ".join(base.split()), items

def clean_name(raw: str) -> str:
    s = strip_flags(raw)
    s = re.sub(r"^\s*(?:\d{1,2}[.)]\s+|[-–—•·]\s*)", "", s)
    return " ".join(s.split()).strip(" .:;|*!•·…=_-–—")

@lru_cache(maxsize=4)
def _not_used_cached(path: str, mtime: float) -> tuple[tuple[str, tuple[str, ...]], ...]:
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for item in data.get("not_used") or []:
        if isinstance(item, dict) and item.get("name_ru"):
            out.append((str(item["name_ru"]), tuple(str(a) for a in item.get("aliases") or [])))
    return tuple(out)

def not_used_entries() -> tuple[tuple[str, tuple[str, ...]], ...]:
    p = Path(config_dir()) / "analytes.yaml"
    try:
        return _not_used_cached(str(p), p.stat().st_mtime)
    except OSError:
        return ()

def _keys_for(name: str) -> list[str]:
    base, _ = split_parens(name)
    k = name_key(base)
    return [k] if k else []

_INDEX_CACHE: dict[int, tuple[Any, tuple, dict[str, Target]]] = {}

def synonym_table(cfg: Config) -> list[tuple[str, Target, str]]:
    rows: list[tuple[str, Target, str]] = []
    for code in cfg.analytes:
        for k in {name_key(code), name_key(code.replace("_", " "))}:
            rows.append((k, Target("analyte", code), code))
    for code, spec in cfg.analytes.items():
        for alias in [*(spec.get("aliases") or []), spec.get("short_ru"), spec.get("name_ru")]:
            if alias:
                for k in _keys_for(alias):
                    rows.append((k, Target("analyte", code), str(alias)))
    for display, aliases in not_used_entries():
        for alias in (display, *aliases):
            for k in _keys_for(alias):
                rows.append((k, Target("ignored", display), alias))
    return rows

def name_index(cfg: Config) -> dict[str, Target]:
    nu = not_used_entries()
    cached = _INDEX_CACHE.get(id(cfg.analytes))
    if cached is not None and cached[0] is cfg.analytes and cached[1] == nu:
        return cached[2]
    index: dict[str, Target] = {}
    for k, target, _ in synonym_table(cfg):
        index.setdefault(k, target)
    _INDEX_CACHE[id(cfg.analytes)] = (cfg.analytes, nu, index)
    return index

_NAME_UNIT = re.compile(r"^(?P<name>.+?),\s*(?P<unit>[^,]{1,16})$")
_UNIT_LIKE = re.compile(r"/|%|‰|^(?:фл|пг|fl|pg)$", re.IGNORECASE)

def split_name_unit(name: str) -> tuple[str, str | None]:
    m = _NAME_UNIT.match(str(name).strip())
    if m and _UNIT_LIKE.search(m.group("unit").strip()):
        return m.group("name"), m.group("unit").strip()
    return str(name), None

def match_name(raw: str, index: dict[str, Target]) -> Target | None:
    found = _match_name(raw, index)
    if found is None:
        base, unit = split_name_unit(clean_name(raw))
        if unit is not None:
            found = _match_name(base, index)
    return found

def _match_name(raw: str, index: dict[str, Target]) -> Target | None:
    text = clean_name(raw)
    if not text:
        return None
    base, items = split_parens(text)
    full = index.get(name_key(base)) if base else None
    candidates = list(items)
    tokens = base.split()
    if len(tokens) >= 2:
        for tok in (tokens[-1], tokens[0]):
            if _CAPS_TOKEN.match(tok):
                candidates.append(tok)
    abbr: list[Target] = []
    for a in candidates:
        t = index.get(name_key(a))
        if t is not None and t not in abbr:
            abbr.append(t)
    if full is not None:
        if len(abbr) == 1 and abbr[0] != full:
            return abbr[0]
        return full
    return abbr[0] if len(abbr) == 1 else None

_MATERIAL_IN_NAME: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("моча", re.compile(r"\bмоч[аеиу]\b|\bмочой\b|\burine\b|\burinalysis\b|\bосад(?:ок|ка|ке)\b|\bп\s*/\s*зр?\b"
                        r"|\bп\.\s*зр?\b|\bпол[еяю]\s+зрения\b", re.IGNORECASE)),
    ("кал", re.compile(r"\bкал[еау]?\b|копрограм", re.IGNORECASE)),
)
_SECTION_OTHER: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("моча", re.compile(
        r"^(?:(?:общий|клинический)\s+)?(?:анализ|исследование)\s+мочи\b|^моча\b|^оам\b"
        r"|^(?:микроскопи\w*\s+)?(?:мочевого\s+)?осад(?:ок|ка)\b|^мочевой\s+осадок|^(?:био)?материал\s*:?\s*моча\b"
        r"|^urin|^(?:анализ\s+мочи\s+)?по\s+(?:нечипоренко|зимницкому)\b", re.IGNORECASE)),
    ("кал", re.compile(r"^копрограмм|^(?:общий\s+)?анализ\s+кала\b|^кал\b|^(?:био)?материал\s*:?\s*кал\b",
                       re.IGNORECASE)),
)
_SECTION_BLOOD = re.compile(
    r"^(?:(?:общий|клинический|развернутый|биохимический|гематологический)\s+)*(?:анализ|исследование)\s+крови\b"
    r"|^оак\b|^кла\b|^гематолог|^гемограмм|^биохими|^гормон|^коагулограм|^гемостаз|^иммунолог|^серолог"
    r"|^витамины\b|^обмен\s+железа|^показатели\s+обмена|^(?:био)?материал\s*:?\s*(?:\w+\s+)?кровь\b|^кровь\s*$"
    r"|^сыворотка\b|^плазма\b|^cbc\b|^complete\s+blood|^hematolog|^biochem|^chemistry|^лейкоцитарн\w*\s+формул",
    re.IGNORECASE)
_DATE_ANY = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}")
MATERIAL_SECTION_NAME = {"моча": "Анализ мочи", "кал": "Анализ кала"}

_FOREIGN_UNIT = re.compile(
    r"^\s*(?:в\s+)?(?:п\s*/\s*зр?\b|п\.\s*зр?\b|пол[еяю]\s+зрени|(?:кл|клет\w*|cells?)\.?\s*/\s*(?:мкл|ul|µl|μl|мл)\b"
    r"|/\s*(?:hpf|lpf)\b)", re.IGNORECASE)

def material_of(name: str) -> str | None:
    s = str(name or "").casefold().replace("ё", "е")
    for material, rx in _MATERIAL_IN_NAME:
        if rx.search(s):
            return material
    return None

def section_of(line: str) -> str | None:
    s = clean_name(str(line or "")).casefold().replace("ё", "е")
    if not s or len(s) > 80 or re.search(r"\d", _DATE_ANY.sub(" ", s)):
        return None
    for material, rx in _SECTION_OTHER:
        if rx.search(s):
            return material
    return "кровь" if _SECTION_BLOOD.search(s) else None

def foreign_unit(text: str) -> bool:
    return bool(_FOREIGN_UNIT.match(strip_flags(str(text or ""))))

_MATERIAL_WORDS = re.compile(
    r"\b(?:в|из)\s+(?:моче|кале)\b|\b(?:моча|мочи|urine)\b|,?\s*\bв\s+(?:п\s*/\s*зр?|п\.\s*зр?|поле\s+зрения)\b.*$",
    re.IGNORECASE)

def material_target(name: str, index: dict[str, Target]) -> Target | None:
    base, _ = split_parens(clean_name(name))
    base = _MATERIAL_WORDS.sub(" ", base)
    return match_name(base, index) if base.strip() else None

def material_display(target: Target | None, cfg: Config, material: str) -> str:
    if target is None:
        return MATERIAL_SECTION_NAME.get(material, material)
    name = cfg.analytes[target.code].get("name_ru", target.code) if target.kind == "analyte" else target.code
    return f"{name} ({material})"

RDW_SD_DISPLAY = "RDW-SD"
_FL_UNIT = re.compile(r"\s*(?:фл|fl)(?![a-zа-яё])", re.IGNORECASE)

def reroute(target: Target, name: str, unit_text: str | None) -> Target:
    if target.kind != "analyte" or target.code != "RDW":
        return target
    _, items = split_parens(clean_name(name))
    words = name_key(name).replace("-", " ").split()
    if any(name_key(i) == "sd" for i in items) or "sd" in words or _FL_UNIT.match(strip_flags(str(unit_text or ""))):
        return Target("ignored", RDW_SD_DISPLAY)
    return target

def esr_method(name: str) -> str | None:
    s = str(name or "").casefold().replace("ё", "е")
    if "вестергрен" in s or "westergren" in s:
        return "westergren"
    if "панченков" in s or "panchenkov" in s:
        return "panchenkov"
    return None

def prefix_analyte(raw: str, index: dict[str, Target]) -> str | None:
    text = clean_name(raw)
    full = match_name(text, index)
    if full is not None:
        return full.code if full.kind == "analyte" else None
    words = text.split()
    for k in range(min(len(words), 8), 0, -1):
        t = index.get(name_key(" ".join(words[:k])))
        if t is not None:
            return t.code if t.kind == "analyte" else None
    return None

SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
POW_UNIT = re.compile(r"(?<![\d.,])(?:[x×х*·]\s*)?10\s*(?:\^|\*|e|е)?\s*([0-9⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2})\s*/\s*([a-zа-яµμ]+)",
                      re.IGNORECASE)

def unit_norm(text: Any) -> str:
    s = unit_key(str(text).translate(SUPERSCRIPTS))
    s = re.sub(r"^[x×х*·]+", "", s)
    m = re.match(r"^10(?:\^|\*|e|е)?(\d{1,2})/(.+)$", s)
    if m:
        s = f"10^{m.group(1)}/{m.group(2)}"
    return s

def unit_table(spec: dict) -> dict[str, str]:
    table: dict[str, str] = {}
    for u in spec.get("units") or {}:
        table.setdefault(unit_norm(u), str(u))
    for canonical in (spec.get("unit"), spec.get("unit_ru")):
        if canonical:
            table.setdefault(unit_norm(canonical), str(canonical))
    return table

def unit_factor(spec: dict, unit: str | None) -> float:
    if unit is None:
        return 1.0
    units = spec.get("units") or {}
    if unit in units:
        return float(units[unit])
    return 1.0

def unit_for_cell(cell: str, spec: dict) -> tuple[str | None, bool]:
    s = " ".join(str(cell or "").split())
    if not s or s in ("-", "—", "–"):
        return None, False
    found = unit_table(spec).get(unit_norm(s))
    return (found, False) if found else (None, True)

_NORM_WORDS = re.compile(r"\b(?:норм[аы]?|референс\w*|реф\.?|ref\w*|интервал|диапазон)\b", re.IGNORECASE)
_DASH_TOKENS = re.compile(r"(?<!\S)[-–—]+(?!\S)")

def unit_in_rest(rest: str, spec: dict) -> tuple[str | None, bool]:
    table = unit_table(spec)
    m = POW_UNIT.search(rest)
    if m:
        found = table.get(unit_norm(m.group(0)))
        return (found, False) if found else (None, True)
    cleaned = _NORM_WORDS.sub(" ", rest)
    cleaned = re.sub(r"[()\[\]:;|]", " ", cleaned)
    cleaned = _DASH_TOKENS.sub(" ", cleaned).strip()
    if not cleaned:
        return None, False
    compact = unit_key(cleaned.translate(SUPERSCRIPTS))
    for key in sorted(table, key=len, reverse=True):
        if compact.startswith(key):
            return table[key], False
    return None, bool(re.match(r"[^\s\d(\[]{1,12}(?:/[^\s(\[]{1,8})?", cleaned))

_NUM = r"\d+(?:[.,]\d+)?"
RANGE = re.compile(
    rf"(?<![\w.,^/*\-–—])(?P<a>{_NUM})\s*(?:-|–|—|−|‑|\.\.\.?|…)\s*(?P<b>{_NUM})(?![\d.,]|\s*[-–—]\s*\d)"
    rf"|(?<![\w])от\s*(?P<c>{_NUM})\s*до\s*(?P<d>{_NUM})(?![\d.,])"
    rf"|(?:<=|≤|<|(?<![\w])(?:до|менее|меньше|ниже|не\s+более))\s*(?P<hi>{_NUM})(?![\d.,])"
    rf"|(?:>=|≥|>|(?<![\w])(?:от|более|больше|выше|свыше|не\s+менее))\s*(?P<lo>{_NUM})(?![\d.,])",
    re.IGNORECASE)

def _num(s: str | None) -> float | None:
    return None if s is None else float(s.replace(",", "."))

def range_from_match(m: re.Match[str]) -> tuple[float | None, float | None]:
    if m.group("a") is not None:
        return _num(m.group("a")), _num(m.group("b"))
    if m.group("c") is not None:
        return _num(m.group("c")), _num(m.group("d"))
    if m.group("hi") is not None:
        return None, _num(m.group("hi"))
    return _num(m.group("lo")), None

def find_range(text: str) -> tuple[tuple[float | None, float | None], tuple[int, int]] | None:
    m = RANGE.search(str(text or ""))
    if m is None:
        return None
    return range_from_match(m), m.span()

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря")
_DATE_RX = re.compile(
    r"(?<!\d)(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4}|\d{2})(?!\d)"
    r"|(?<!\d)(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})(?!\d)"
    r"|(?<!\d)(?P<d3>\d{1,2})\s+(?P<mon>" + "|".join(_MONTHS) + r")\s+(?P<y3>\d{4})(?!\d)", re.IGNORECASE)
def _words(*words: str) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(w.translate(_HOMOGLYPHS)) for w in words))

def _skeleton_rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern.translate(_HOMOGLYPHS))

MAX_DATE_AGE_DAYS = 15 * 365 + 4
_P_BIRTH = _skeleton_rx(r"рожд|\bр\S{1,2}жд|\bр\S{1,3}дени|г\.\s*р\b|\bд\s*[./]?\s*р\b|дата\s*р\b|\bр\s*-\s*я\b"
                        r"|birth|\bdob\b")
_BIRTH_AFTER = _skeleton_rx(r"^\s*(?:\(([^()]{0,40})\)|г\.\s*р\b)")
_P_PREV = _skeleton_rx(r"предыдущ|прошл|\bранее\b|\bпред\.")
_P_TAKEN = _words("взят", "забор", "сбор")
_P_OTHER = _words("регистр", "выдач", "печат", "поступ", "заказ", "оформ", "прием")
_P_DONE = _words("выполн", "исследован", "готов", "результат", "анализ", "валид", "одобр", "утвержд")
_P_DATE = _words("дата")
_P_ANY = _words("дат", "взят", "выполн", "забор")

def _label_priority(label: str) -> int | None:
    sk = label.translate(_HOMOGLYPHS)
    if _P_BIRTH.search(sk) or _P_PREV.search(sk):
        return None
    if _P_TAKEN.search(sk):
        return 1
    if _P_OTHER.search(sk):
        return 3
    if _P_DONE.search(sk):
        return 2
    if _P_DATE.search(sk):
        return 3
    return None

def _date_of(m: re.Match[str]) -> dt.date | None:
    try:
        if m.group("d") is not None:
            y = int(m.group("y"))
            y = y + 2000 if y < 100 else y
            return dt.date(y, int(m.group("m")), int(m.group("d")))
        if m.group("y2") is not None:
            return dt.date(int(m.group("y2")), int(m.group("m2")), int(m.group("d2")))
        return dt.date(int(m.group("y3")), _MONTHS.index(m.group("mon").lower()) + 1, int(m.group("d3")))
    except ValueError:
        return None

def _birth_after(after: str) -> bool:
    m = _BIRTH_AFTER.match(after.translate(_HOMOGLYPHS))
    return m is not None and (m.group(1) is None or bool(_P_BIRTH.search(m.group(1))))

_DATE_AT_START = re.compile(r"^\s*(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[а-яё]+\s+\d{4})(?!\d)",
                            re.IGNORECASE)

def _join_label_lines(lines: list[str]) -> list[str]:
    out = list(lines)
    for i, line in enumerate(lines):
        low = line.casefold().replace("ё", "е")
        if len(low) > 60 or not _P_ANY.search(low.translate(_HOMOGLYPHS)) or _DATE_RX.search(low):
            continue
        nxt = next((x for x in lines[i + 1:i + 3] if x.strip()), "")
        if _DATE_AT_START.match(nxt):
            out[i] = line + " " + nxt
    return out

def find_date(lines: Iterable[str], today: dt.date | None = None) -> str | None:
    today = today or dt.date.today()
    oldest, latest = today - dt.timedelta(days=MAX_DATE_AGE_DAYS), today + dt.timedelta(days=1)
    best: tuple[int, dt.date] | None = None
    for line in _join_label_lines([str(x) for x in lines]):
        low = str(line).casefold().replace("ё", "е")
        if not _P_ANY.search(low.translate(_HOMOGLYPHS)):
            continue
        prev = 0
        matches = list(_DATE_RX.finditer(low))
        for k, m in enumerate(matches):
            label = low[prev:m.start()]
            if prev:
                tail = _BIRTH_AFTER.match(label.translate(_HOMOGLYPHS))
                label = label[tail.end():] if tail else label
            prev = m.end()
            prio = _label_priority(label)
            if prio is None:
                continue
            if _birth_after(low[m.end():matches[k + 1].start() if k + 1 < len(matches) else len(low)]):
                continue
            d = _date_of(m)
            if d is None or not (oldest <= d <= latest):
                continue
            if best is None or prio < best[0]:
                best = (prio, d)
    return best[1].isoformat() if best else None

def within_hard(value: float, unit: str | None, spec: dict) -> bool:
    hard = spec.get("hard")
    if not hard:
        return True
    return hard[0] <= value * unit_factor(spec, unit) <= hard[1]

def _within(bounds, value: float, unit: str | None, spec: dict) -> bool:
    return not bounds or bounds[0] <= value * unit_factor(spec, unit) <= bounds[1]

THOUSANDS_SEP = "[    ]"
_THOUSANDS_SPLIT = re.compile(THOUSANDS_SEP)
_THREE_DECIMALS = re.compile(r"^[1-9]\d{0,2}[.,]\d{3}$")

def number_reading(raw: str, unit: str | None, spec: dict) -> tuple[str, float | None]:
    s = str(raw).strip()
    hard, soft = spec.get("hard"), spec.get("soft")
    if _THOUSANDS_SPLIT.search(s):
        parts = [p for p in _THOUSANDS_SPLIT.split(s) if p]
        grouped = float("".join(parts).replace(",", "."))
        groups = [float(p.replace(",", ".")) for p in parts]
        if not _within(hard, grouped, unit, spec):
            if _within(hard, groups[0], unit, spec):
                return "multi", None
            return "ok", grouped
        if not _within(soft, grouped, unit, spec) and all(_within(soft, g, unit, spec) for g in groups):
            return "ambiguous", None
        return "ok", grouped
    if _THREE_DECIMALS.match(s):
        thousands = float(re.sub(r"[.,]", "", s))
        if _within(hard, thousands, unit, spec):
            return "ambiguous", None
    return "ok", float(s.replace(",", "."))

class Collector:

    def __init__(self, cfg: Config, *, extended: bool = False):
        self.cfg = cfg
        self.extended = extended
        self.values: dict[str, dict] = {}
        self.found_at: dict[str, int] = {}
        self.unrecognized: list[tuple[tuple[int, int], str]] = []
        self.notes: list[str] = []
        self.ranges: dict[str, dict] = {}
        self.ignored: list[str] = []
        self.skipped = 0
        self.date_hint: str | None = None
        self._rank: dict[str, tuple[int, int]] = {}
        self._value_notes: dict[str, list[str]] = {}
        self._methods: dict[str, set[str]] = {}
        self._method: dict[str, str | None] = {}

    def name(self, code: str) -> str:
        return self.cfg.analytes[code].get("name_ru", code)

    def remember(self, order: tuple[int, int], where: str, what: str) -> None:
        if len(self.unrecognized) < MAX_UNRECOGNIZED:
            short = f"{where}: {what}"
            if len(short) > UNRECOGNIZED_WIDTH:
                short = short[:UNRECOGNIZED_WIDTH - 1] + "…"
            self.unrecognized.append((order, short))
        else:
            self.skipped += 1

    def ignore(self, display: str, order: tuple[int, int], where: str, quiet: bool = False) -> None:
        if self.extended:
            if display not in self.ignored:
                self.ignored.append(display)
        elif not quiet:
            self.remember(order, where, "название показателя не распознано")

    def multiple(self, code: str, order: tuple[int, int], where: str) -> None:
        name = self.name(code)
        self.remember(order, where, f"{name} — несколько результатов")
        self.notes.append(f"{name}: в строке несколько результатов (например, предыдущий и текущий) — значение "
                          "не взято. Проверьте бланк и введите значение вручную.")

    def ambiguous(self, code: str, order: tuple[int, int], where: str) -> None:
        name = self.name(code)
        self.remember(order, where, f"{name} — проверьте значение")
        self.notes.append(f"{name}: число в бланке можно прочитать по-разному (разделитель тысяч или десятичный "
                          "знак, одно число или два) — значение не взято. Проверьте и введите вручную.")

    def foreign(self, code: str, order: tuple[int, int], where: str) -> None:
        name = self.name(code)
        self.remember(order, where, f"{name} — единица анализа мочи")
        self.notes.append(f"{name}: значение в единицах «в поле зрения» или «клеток/мкл» — это анализ мочи, "
                          "а не крови. Значение не взято.")

    def add(self, code: str, value: float, *, sign: str | None, unit: str | None, unit_unknown: bool,
            rng: tuple[float | None, float | None] | None, order: tuple[int, int], where: str,
            lineno: int | None = None, raw: str | None = None, method: str | None = None) -> bool:
        spec = self.cfg.analytes[code]
        name = self.name(code)
        if raw is not None:
            how, read = number_reading(raw, unit, spec)
            if how == "multi":
                self.multiple(code, order, where)
                return False
            if how == "ambiguous":
                self.ambiguous(code, order, where)
                return False
            value = read
        if not math.isfinite(value) or not within_hard(value, unit, spec):
            self.remember(order, where, f"{name} — значение вне диапазона")
            self.notes.append(f"{name}: значение в строке не похоже на этот показатель — строка пропущена.")
            return False
        if method:
            self._methods.setdefault(code, set()).add(method)
        rank = (0 if unit is None and unit_unknown else 1, 1 if method == "westergren" else 0)
        if code in self.values:
            old = self._rank[code]
            both_methods = {"westergren", "panchenkov"} <= self._methods.get(code, set())
            if rank <= old:
                if not both_methods:
                    self.notes.append(f"{name} встречается несколько раз: взято первое значение.")
                return False
            self._drop(code)
            if rank[0] > old[0]:
                self.notes.append(f"{name} встречается несколько раз: взято значение с распознанной единицей.")
            elif not both_methods:
                self.notes.append(f"{name} встречается несколько раз: взято значение по Вестергрену.")
        self.values[code] = {"value": value, "unit": unit}
        self._rank[code] = rank
        self._method[code] = method
        if lineno is not None:
            self.found_at[code] = lineno
        own: list[str] = []
        if sign:
            own.append(f"{name}: в бланке значение со знаком «{sign.strip()}» — взято само число.")
        if unit is None and unit_unknown:
            own.append(f"{name}: единица не распознана — принята {spec.get('unit_ru', '')}. Проверьте.")
        elif unit is None and spec.get("unit_ambiguous"):
            own.append(f"{name}: единица не указана — принята {spec.get('unit_ru', '')}. Проверьте.")
        self.notes.extend(own)
        self._value_notes[code] = own
        if rng is not None:
            self._add_range(code, rng, unit, unit_unknown)
        return True

    def _drop(self, code: str) -> None:
        self.values.pop(code, None)
        self.found_at.pop(code, None)
        self.ranges.pop(code, None)
        for note in self._value_notes.pop(code, []):
            if note in self.notes:
                self.notes.remove(note)

    def _add_range(self, code: str, rng: tuple[float | None, float | None], unit: str | None,
                   unit_unknown: bool) -> None:
        spec = self.cfg.analytes[code]
        if unit_unknown or (unit is None and spec.get("unit_ambiguous")) or code in self.ranges:
            return
        k = unit_factor(spec, unit)
        lo = None if rng[0] is None else round(rng[0] * k, 6)
        hi = None if rng[1] is None else round(rng[1] * k, 6)
        hard_hi = float((spec.get("hard") or [0, math.inf])[1])
        if lo is None and hi is None:
            return
        if any(x is not None and (x < 0 or x > hard_hi) for x in (lo, hi)):
            return
        if lo is not None and hi is not None and lo > hi:
            return
        self.ranges[code] = {"low": lo, "high": hi, "unit": spec.get("unit_ru") or spec.get("unit")}

    def _method_notes(self) -> list[str]:
        if "ESR" not in self.values:
            return []
        name, kept, seen = self.name("ESR"), self._method.get("ESR"), self._methods.get("ESR", set())
        if kept == "westergren" and "panchenkov" in seen:
            return [f"{name}: в бланке два метода — взято значение по Вестергрену (референтный метод)."]
        if kept == "panchenkov":
            return [(f"{name}: метод Панченкова — при высоких значениях он расходится с методом Вестергрена, "
                     "по которому заданы нормы. Проверьте значение.")]
        return []

    def result(self, *, date: str | None = None, line_numbers: bool = False) -> dict:
        notes = list(dict.fromkeys([*self.notes, *self._method_notes()]))
        if self.skipped:
            notes.append(f"Ещё {self.skipped} нераспознанных строк не показаны.")
        if not self.values:
            notes.append("Показатели не найдены: проверьте, что в строках есть название анализа и число.")
        out: dict[str, Any] = {"values": self.values,
                               "unrecognized": [t for _, t in sorted(self.unrecognized, key=lambda x: x[0])],
                               "notes": notes}
        if self.extended:
            out["reference_ranges"] = self.ranges
            out["ignored"] = self.ignored
            out["date"] = date
        if line_numbers:
            out["lines"] = dict(self.found_at)
        return out
