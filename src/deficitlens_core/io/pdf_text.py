from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_PAGES = 10
MAX_SECONDS = 4.0
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_TABLES = 60
MAX_TABLE_CELLS = 8_000
MAX_CELL_CHARS = 300
MAX_ABOVE_CHARS = 400
MAX_MEMORY_BYTES = 1536 * 1024 * 1024
HARD_TIMEOUT_EXTRA = 3.0
PDF_MAGIC = b"%PDF-"
SCAN_MAX_PAGES = 5
SCAN_DPI = 200
SCAN_MAX_SIDE = 3200

NO_TEXT_LAYER = "В PDF не найден ни текст, ни изображение страницы: введите значения вручную или вставьте текст бланка."
_SRC_DIR = str(Path(__file__).resolve().parents[2])

@dataclass
class PdfText:
    text: str
    pages_total: int
    pages_read: int
    notes: list[str] = field(default_factory=list)
    tables: list = field(default_factory=list)
    text_outside: str = ""
    table_above: list = field(default_factory=list)
    images: int = 0
    scanned: bool = False

def _limit_child_resources(max_seconds: float) -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        cpu = int(max_seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except (ImportError, ValueError, OSError):
        pass

def _outside(bboxes: list[tuple]):
    def keep(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in bboxes)
    return keep

def _precheck(data: bytes) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
    finally:
        pdf.close()
    if total < 1:
        raise ValueError("в PDF нет страниц")
    return total

def _extract_in_process(data: bytes, max_pages: int, max_seconds: float) -> PdfText:
    import logging

    _precheck(data)
    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    t0 = time.monotonic()
    parts: list[str] = []
    outside: list[str] = []
    tables: list = []
    above: list[str] = []
    cells = 0
    notes: list[str] = []
    read = images = graphics = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages[:max_pages]:
            if time.monotonic() - t0 > max_seconds:
                notes.append(f"PDF разбирался дольше {max_seconds:g} с — прочитаны первые {read} стр.; "
                             "проверьте, все ли показатели найдены.")
                break
            text = page.extract_text() or ""
            parts.append(text)
            images += len(page.images)
            graphics += len(page.images) + len(page.curves) + len(page.rects) + len(page.lines)
            bboxes = []
            if text.strip() and len(tables) < MAX_TABLES and cells < MAX_TABLE_CELLS:
                found = sorted(page.find_tables(), key=lambda t: (t.bbox[1], t.bbox[0]))
                for t in found:
                    rows = [[(c or "")[:MAX_CELL_CHARS] for c in row] for row in t.extract()]
                    n = sum(len(r) for r in rows)
                    if len(tables) >= MAX_TABLES or cells + n > MAX_TABLE_CELLS:
                        notes.append("В PDF много таблиц — разобраны не все; проверьте, все ли показатели найдены.")
                        break
                    tables.append(rows)
                    cells += n
                    bboxes.append(t.bbox)
                above.extend(_text_above(page, bboxes, [t.bbox for t in found]))
            outside.append((page.filter(_outside(bboxes)).extract_text() or "") if bboxes else text)
            read += 1
            if sum(len(p) for p in parts) > MAX_TEXT_CHARS:
                notes.append(f"Текст PDF слишком длинный — прочитаны первые {read} стр.")
                break
    if total > max_pages and not notes:
        notes.append(f"В PDF {total} стр. — прочитаны первые {max_pages}; проверьте, все ли показатели найдены.")
    text = "\n".join(parts)[:MAX_TEXT_CHARS]
    return PdfText(text=text, pages_total=total, pages_read=read, notes=notes, tables=tables,
                   text_outside="\n".join(outside)[:MAX_TEXT_CHARS], images=images,
                   scanned=not text.strip() and graphics > 0, table_above=above[:len(tables)])

def _text_above(page, taken: list[tuple], all_boxes: list[tuple]) -> list[str]:
    x0, top0, x1, _ = page.bbox
    out: list[str] = []
    prev_bottom = top0
    for bbox in taken:
        top = bbox[1]
        text = ""
        if top - prev_bottom >= 1:
            try:
                region = page.crop((x0, prev_bottom, x1, top))
                text = (region.filter(_outside(all_boxes)).extract_text() or "")[-MAX_ABOVE_CHARS:]
            except Exception:
                text = ""
        out.append(text)
        prev_bottom = max(prev_bottom, bbox[3])
    return out

def _render_in_process(data: bytes, max_pages: int, dpi: int) -> tuple[list[bytes], int]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
        out: list[bytes] = []
        for i in range(min(total, max_pages)):
            page = pdf[i]
            w, h = page.get_size()
            scale = min(dpi / 72.0, SCAN_MAX_SIDE / max(w, h, 1.0))
            image = page.render(scale=scale, grayscale=True).to_pil().convert("L")
            buf = io.BytesIO()
            image.save(buf, "PNG", optimize=False)
            out.append(buf.getvalue())
            page.close()
        return out, total
    finally:
        pdf.close()

def _worker_main() -> None:
    if sys.argv[1] == "render":
        max_pages, max_seconds, dpi = int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
        _limit_child_resources(max_seconds)
        try:
            pages, total = _render_in_process(sys.stdin.buffer.read(), max_pages, dpi)
            out = {"status": "ok", "pages_total": total,
                   "pages": [base64.b64encode(p).decode("ascii") for p in pages]}
        except Exception:
            out = {"status": "error"}
        sys.stdout.write(json.dumps(out, ensure_ascii=True))
        return
    max_pages, max_seconds = int(sys.argv[1]), float(sys.argv[2])
    _limit_child_resources(max_seconds)
    try:
        r = _extract_in_process(sys.stdin.buffer.read(), max_pages, max_seconds)
        out = {"status": "ok", "text": r.text, "pages_total": r.pages_total, "pages_read": r.pages_read,
               "notes": r.notes, "tables": r.tables, "text_outside": r.text_outside, "images": r.images,
               "scanned": r.scanned, "table_above": r.table_above}
    except Exception:
        out = {"status": "error"}
    sys.stdout.write(json.dumps(out, ensure_ascii=True))

def _input_error(code: str, message: str, field: str | None = "file") -> Exception:
    from ..schemas import InputError

    return InputError(code, message, field)

def _bad_pdf() -> Exception:
    return _input_error("bad_pdf", "Не удалось прочитать PDF: файл повреждён, зашифрован или это не PDF. "
                        "Сохраните бланк как PDF с текстом или вставьте текст анализа.")

def _run_child(args: list[str], data: bytes, max_seconds: float) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")}
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "deficitlens_core.io.pdf_text", *args]
    try:
        proc = subprocess.run(cmd, input=bytes(data), capture_output=True, timeout=max_seconds + HARD_TIMEOUT_EXTRA,
                              check=False, env=env)
    except subprocess.TimeoutExpired as e:
        raise _input_error("pdf_too_complex", f"PDF не удалось разобрать за {max_seconds:g} с: вставьте текст "
                           "анализа или введите значения вручную.") from e
    except OSError as e:
        raise _input_error("pdf_unavailable", "Разбор PDF сейчас недоступен: вставьте текст анализа.") from e
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _bad_pdf() from e
    if not isinstance(out, dict) or out.get("status") != "ok":
        raise _bad_pdf()
    return out

def _check_magic(data: bytes) -> None:
    if not isinstance(data, (bytes, bytearray)) or not bytes(data[:1024]).lstrip().startswith(PDF_MAGIC):
        raise _bad_pdf()
    if len(data) > MAX_PDF_BYTES:
        raise _input_error("pdf_too_large", f"PDF больше {MAX_PDF_BYTES // (1024 * 1024)} МБ. Бланк анализа обычно "
                           "меньше 1 МБ: сохраните только страницы с результатами или вставьте текст анализа.")

def extract_text(data: bytes, *, max_pages: int = MAX_PAGES, max_seconds: float = MAX_SECONDS) -> PdfText:
    _check_magic(data)
    out = _run_child([str(int(max_pages)), str(float(max_seconds))], data, max_seconds)
    text = str(out.get("text") or "")
    scanned = bool(out.get("scanned"))
    if not text.strip() and not scanned:
        raise _input_error("no_text_layer", NO_TEXT_LAYER)
    tables = out.get("tables") or []
    if not isinstance(tables, list):
        tables = []
    above = out.get("table_above") or []
    above = [str(a or "") for a in above] if isinstance(above, list) else []
    above = (above + [""] * len(tables))[:len(tables)]
    return PdfText(text=text, pages_total=int(out.get("pages_total") or 0), pages_read=int(out.get("pages_read") or 0),
                   notes=[str(n) for n in out.get("notes") or []], tables=tables,
                   text_outside=str(out.get("text_outside") or ""), images=int(out.get("images") or 0),
                   scanned=scanned and not text.strip(), table_above=above)

def render_pages(data: bytes, *, max_pages: int = SCAN_MAX_PAGES, dpi: int = SCAN_DPI,
                 max_seconds: float = MAX_SECONDS) -> tuple[list[bytes], int]:
    _check_magic(data)
    out = _run_child(["render", str(int(max_pages)), str(float(max_seconds)), str(int(dpi))], data, max_seconds)
    pages = []
    for p in out.get("pages") or []:
        try:
            pages.append(base64.b64decode(str(p), validate=True))
        except ValueError:
            raise _bad_pdf() from None
    if not pages:
        raise _bad_pdf()
    return pages, int(out.get("pages_total") or len(pages))

if __name__ == "__main__":
    _worker_main()
