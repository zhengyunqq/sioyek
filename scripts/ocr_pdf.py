"""
Sioyek OCR: add an invisible, searchable text layer to scanned PDFs.

Usage (from prefs_user.config):
    new_command _ocr python -m sioyek.ocr_pdf "%{sioyek_path}" "%{local_database}" "%{shared_database}" "%{file_path}"

Pipeline
    1. Find pages without a usable text layer (scanned pages).
    2. Render them with PyMuPDF and feed them to the Apple Vision helper
       (ocr_vision.swift, compiled on first use), which runs concurrently on the
       Neural Engine with automatic language detection.
    3. For every recognized token, write invisible text (render mode 3) with a
       "glyphless" CID font whose glyphs are exactly 0.5em wide; Tz horizontal
       scaling stretches each token to its exact box, so MuPDF character quads
       line up with the scanned glyphs. That is what makes Sioyek's smart jump,
       selection, highlighting, search and copy work on the page.
    4. Save in place (incrementally when possible) and ask Sioyek to reload.
       Sioyek keys highlights/bookmarks by path -> cached checksum, so they survive.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import pymupdf as fitz

try:
    from .sioyek import Sioyek, clean_path
except ImportError:  # running as a plain script
    from sioyek import Sioyek, clean_path

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT_SOURCE = os.path.join(HERE, "ocr_vision.swift")
GLYPHLESS_FONT = os.path.join(HERE, "pdf.ttf")  # Tesseract's glyphless font (Apache-2.0)
CACHE_DIR = os.path.expanduser("~/.cache/sioyek_ocr")
HELPER_BIN = os.path.join(CACHE_DIR, "sioyek_ocr_vision")
LOG_PATH = os.path.expanduser("~/.config/sioyek/ocr.log")

OCR_FONT_NAME = "SioyekOCRGlyphLess"
OCR_RESOURCE_NAME = "SioyekOCR"  # page /Font resource key (unique, never clashes with /F1 etc.)
BASELINE_SHIFT = 0.0            # baseline offset (in em) above the bottom of Vision's line box
TARGET_LONG_SIDE_PX = 3000      # render resolution for recognition
MIN_TEXT_CHARS = 20             # pages with fewer extracted chars are treated as scanned
MAX_PENDING_IMAGES = 12         # bounded render-ahead to keep disk/memory usage low


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


class Status:
    """Throttled status-bar updates through the Sioyek CLI bridge."""

    def __init__(self, sioyek):
        self.sioyek = sioyek
        self.last = 0.0

    def set(self, text, force=False):
        now = time.time()
        if not force and now - self.last < 0.7:
            return
        self.last = now
        try:
            self.sioyek.set_status_string(text)
        except Exception:
            pass

    def clear(self):
        try:
            self.sioyek.clear_status_string()
        except Exception:
            pass


# --------------------------------------------------------------------------- helper binary

def ensure_helper(status):
    if not os.path.exists(SWIFT_SOURCE):
        raise RuntimeError(f"找不到 OCR 引擎源码: {SWIFT_SOURCE}")
    if os.path.exists(HELPER_BIN) and os.path.getmtime(HELPER_BIN) >= os.path.getmtime(SWIFT_SOURCE):
        return HELPER_BIN
    os.makedirs(CACHE_DIR, exist_ok=True)
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    status.set("OCR: 首次使用，正在编译 Apple Vision 识别引擎…", force=True)
    tmp_bin = HELPER_BIN + ".tmp"
    proc = subprocess.run([swiftc, "-O", SWIFT_SOURCE, "-o", tmp_bin],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("编译 OCR 引擎失败（需要 Xcode Command Line Tools）:\n" + proc.stderr[-2000:])
    os.replace(tmp_bin, HELPER_BIN)
    return HELPER_BIN


# --------------------------------------------------------------------------- page selection

def page_has_ocr_layer(page):
    try:
        return any(OCR_FONT_NAME in (f[3] or "") for f in page.get_fonts(full=True))
    except Exception:
        return False


def page_needs_ocr(page):
    if page_has_ocr_layer(page):
        return False
    text = page.get_text("text") or ""
    return len("".join(text.split())) < MIN_TEXT_CHARS


# --------------------------------------------------------------------------- text layer

TOUNICODE_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def create_glyphless_font(doc):
    """Creates a Type0/Identity-H font in `doc` where CID == Unicode (BMP) and every
    glyph is 0.5em wide. Returns the xref of the Type0 font dictionary."""
    tounicode = doc.get_new_xref()
    doc.update_object(tounicode, "<<>>")
    doc.update_stream(tounicode, TOUNICODE_CMAP)

    fontfile = None
    if os.path.exists(GLYPHLESS_FONT):
        with open(GLYPHLESS_FONT, "rb") as f:
            data = f.read()
        fontfile = doc.get_new_xref()
        doc.update_object(fontfile, f"<</Length1 {len(data)}>>")
        doc.update_stream(fontfile, data)

    descriptor = doc.get_new_xref()
    doc.update_object(
        descriptor,
        f"<</Type/FontDescriptor/FontName/{OCR_FONT_NAME}/Flags 5"
        "/FontBBox[0 0 500 1000]/ItalicAngle 0/Ascent 1000/Descent 0"
        "/CapHeight 1000/StemV 80"
        + (f"/FontFile2 {fontfile} 0 R" if fontfile else "")
        + ">>",
    )

    cidfont = doc.get_new_xref()
    doc.update_object(
        cidfont,
        f"<</Type/Font/Subtype/CIDFontType2/BaseFont/{OCR_FONT_NAME}"
        "/CIDSystemInfo<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>"
        f"/FontDescriptor {descriptor} 0 R/DW 500/CIDToGIDMap/Identity>>",
    )

    type0 = doc.get_new_xref()
    doc.update_object(
        type0,
        f"<</Type/Font/Subtype/Type0/BaseFont/{OCR_FONT_NAME}/Encoding/Identity-H"
        f"/DescendantFonts[{cidfont} 0 R]/ToUnicode {tounicode} 0 R>>",
    )
    return type0


def encode_bmp(text):
    return "".join(f"{ord(c):04X}" for c in text if ord(c) <= 0xFFFF and c not in "\r\n\t")


def char_weight(c):
    o = ord(c)
    if 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
        return 1.0
    if c.isspace():
        return 0.3
    return 0.55


def token_boxes_for_line(line):
    """Returns [(text, x0, x1)] in normalized coordinates. Falls back to proportional
    splitting when Vision returns the whole-line box for every sub-range."""
    lx, ly, lw, lh = line["box"]
    tokens = [t for t in line.get("tokens", []) if t.get("t", "").strip()]
    if not tokens:
        return []
    degenerate = len(tokens) > 1 and all(
        abs(t["box"][2] - lw) < 0.02 * max(lw, 1e-6) for t in tokens
    )
    if not degenerate:
        out = []
        for t in tokens:
            x, _, w, _ = t["box"]
            x0 = max(lx - 0.002, x)
            x1 = min(lx + lw + 0.002, x + w)
            if x1 > x0:
                out.append((t["t"], x0, x1, bool(t.get("sp", False))))
        return out

    # proportional fallback using the full line string (keeps inter-word spacing)
    text = line["text"]
    weights = [char_weight(c) for c in text]
    total = sum(weights) or 1.0
    out, pos, cur, start = [], 0.0, "", 0.0
    for c, wgt in zip(text, weights):
        if c.isspace():
            if cur:
                out.append((cur, lx + lw * start / total, lx + lw * pos / total, True))
                cur = ""
            pos += wgt
            continue
        if not cur:
            start = pos
        cur += c
        pos += wgt
    if cur:
        out.append((cur, lx + lw * start / total, lx + lw * pos / total, False))
    return out


def _show(ops, hex_text, n, x, w, baseline, font_size):
    natural_w = n * 0.5 * font_size
    tz = max(1.0, min(1000.0, 100.0 * w / natural_w))
    ops.append(f"/{OCR_RESOURCE_NAME} {font_size:.2f} Tf {tz:.2f} Tz "
               f"1 0 0 1 {x:.2f} {baseline:.2f} Tm <{hex_text}> Tj")


def build_content_stream(result, width, height):
    """PDF content stream (overlay page of size width x height, visible orientation)."""
    ops = ["BT", "3 Tr"]
    count = 0
    for line in result.get("lines", []):
        lx, ly, lw, lh = line["box"]
        if lw <= 0 or lh <= 0:
            continue
        font_size = lh * height
        if font_size < 1.0:
            continue
        # one baseline per recognized line keeps MuPDF from splitting it into fragments
        baseline = height - (ly + lh) * height + BASELINE_SHIFT * font_size
        tokens = token_boxes_for_line(line)
        for i, (text, x0, x1, space_after) in enumerate(tokens):
            hex_text = encode_bmp(text)
            n = len(hex_text) // 4
            if n == 0:
                continue
            _show(ops, hex_text, n, x0 * width, (x1 - x0) * width, baseline, font_size)
            count += 1
            # explicit space glyph spanning the gap to the next word
            if space_after and i + 1 < len(tokens):
                gap_x0 = x1 * width
                gap_w = max(tokens[i + 1][1] * width - gap_x0, 0.15 * font_size)
                _show(ops, "0020", 1, gap_x0, gap_w, baseline, font_size)
    ops.append("ET")
    return "\n".join(ops).encode("latin-1"), count


def _append_contents(doc, page, stream):
    """Appends a new content stream to `page`, isolating the existing content in q/Q."""
    page.wrap_contents()
    cxref = doc.get_new_xref()
    doc.update_object(cxref, "<<>>")
    doc.update_stream(cxref, stream)
    kind, value = doc.xref_get_key(page.xref, "Contents")
    if kind == "array":
        new = value.strip()[:-1] + f" {cxref} 0 R]"
    elif kind == "xref":
        new = f"[{value} {cxref} 0 R]"
    else:
        new = f"[{cxref} 0 R]"
    doc.xref_set_key(page.xref, "Contents", new)


def apply_text_layers(doc, results):
    """Injects the invisible text layer into every OCR'd page of `doc`."""
    font_buffer = None
    if os.path.exists(GLYPHLESS_FONT):
        with open(GLYPHLESS_FONT, "rb") as f:
            font_buffer = f.read()
    glyphless = create_glyphless_font(doc)
    glyphless_dict = doc.xref_object(glyphless, compressed=True)
    patched = set()
    injected = 0

    for pno, result in sorted(results.items()):
        page = doc[pno]
        rect = page.rect  # visible (rotated) page rect == the space we rendered in
        content, count = build_content_stream(result, rect.width, rect.height)
        if count == 0:
            continue

        # Register a font resource on the page (PyMuPDF resolves inherited /Resources
        # for us), then point that font object at our glyphless CID font.
        fxref = page.insert_font(fontname=OCR_RESOURCE_NAME,
                                 fontbuffer=font_buffer or fitz.Font("helv").buffer)
        if fxref not in patched:
            doc.update_object(fxref, glyphless_dict)
            patched.add(fxref)

        # visible bottom-left coords -> visible top-left -> unrotated page -> PDF user space
        to_pdf = (fitz.Matrix(1, 0, 0, -1, 0, rect.height)
                  * page.derotation_matrix
                  * ~page.transformation_matrix)
        cm = " ".join(f"{v:.6f}" for v in tuple(to_pdf))
        stream = f"q\n{cm} cm\n".encode("latin-1") + content + b"\nQ\n"
        _append_contents(doc, page, stream)
        injected += 1
    return injected


# --------------------------------------------------------------------------- OCR pipeline

def run_ocr(doc, pages, helper, status, workdir):
    total = len(pages)
    results = {}
    errors = []
    pending = threading.Semaphore(MAX_PENDING_IMAGES)
    proc = subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    render_error = []

    def producer():
        try:
            for pno in pages:
                pending.acquire()
                page = doc[pno]
                zoom = TARGET_LONG_SIDE_PX / max(page.rect.width, page.rect.height)
                zoom = max(2.0, min(5.0, zoom))
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
                path = os.path.join(workdir, f"p{pno:05d}.png")
                pix.save(path)
                proc.stdin.write(f"{pno}\t{path}\n")
                proc.stdin.flush()
        except Exception as e:  # pragma: no cover - surfaced below
            render_error.append(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    # PyMuPDF documents are not thread-safe; rendering happens only in the producer,
    # the consumer only parses JSON, and the document is not touched until both finish.
    t = threading.Thread(target=producer, daemon=True)
    t.start()

    start = time.time()
    for raw in proc.stdout:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        pno = data.get("page")
        if data.get("error"):
            errors.append(f"page {pno}: {data['error']}")
        results[pno] = data
        try:
            os.remove(os.path.join(workdir, f"p{pno:05d}.png"))
        except OSError:
            pass
        pending.release()
        done = len(results)
        elapsed = time.time() - start
        eta = (elapsed / done) * (total - done) if done else 0
        status.set(f"OCR 进行中: {done}/{total} 页 ({done * 100 // total}%)  剩余约 {int(eta)}s",
                   force=(done == total))

    t.join()
    proc.wait()
    if render_error:
        raise render_error[0]
    if proc.returncode not in (0, None):
        errors.append(proc.stderr.read()[-1000:])
    return results, errors


def save_in_place(doc, file_path):
    if doc.can_save_incrementally():
        doc.saveIncr()
        return "incremental"
    tmp = file_path + ".ocr_tmp"
    doc.save(tmp, garbage=1, deflate=True)
    doc.close()
    os.replace(tmp, file_path)
    return "full"


def main():
    if len(sys.argv) < 5:
        print("usage: python -m sioyek.ocr_pdf SIOYEK LOCAL_DB SHARED_DB FILE [first-last|all]")
        sys.exit(1)

    sioyek_path = clean_path(sys.argv[1])
    local_db = clean_path(sys.argv[2])
    shared_db = clean_path(sys.argv[3])
    file_path = clean_path(sys.argv[4])
    page_range = clean_path(sys.argv[5]) if len(sys.argv) > 5 else ""

    sioyek = Sioyek(sioyek_path, local_db, shared_db)
    status = Status(sioyek)

    os.makedirs(CACHE_DIR, exist_ok=True)
    lock_path = os.path.join(CACHE_DIR, str(abs(hash(os.path.abspath(file_path)))) + ".lock")
    if os.path.exists(lock_path) and time.time() - os.path.getmtime(lock_path) < 3600:
        status.set("OCR 已在后台处理该文档，请稍候…", force=True)
        return
    open(lock_path, "w").close()

    workdir = tempfile.mkdtemp(prefix="sioyek_ocr_")
    try:
        doc = fitz.open(file_path)
        if not doc.is_pdf:
            status.set("OCR 仅支持 PDF 文档", force=True)
            return
        if doc.needs_pass:
            status.set("OCR 无法处理加密的 PDF", force=True)
            return

        candidates = list(range(len(doc)))
        if page_range and page_range != "all" and "-" in page_range:
            a, b = page_range.split("-", 1)
            candidates = list(range(max(0, int(a) - 1), min(len(doc), int(b))))

        status.set("OCR: 正在分析页面文字层…", force=True)
        pages = [p for p in candidates if page_needs_ocr(doc[p])]
        if not pages:
            status.set("OCR: 当前文档已有文字层，无需识别", force=True)
            time.sleep(3)
            status.clear()
            return

        helper = ensure_helper(status)
        status.set(f"OCR 进行中: 0/{len(pages)} 页 (0%)", force=True)
        t0 = time.time()
        results, errors = run_ocr(doc, pages, helper, status, workdir)

        status.set("OCR: 正在写入隐藏文字层…", force=True)
        injected = apply_text_layers(doc, results)
        mode = save_in_place(doc, file_path)
        elapsed = time.time() - t0
        log(f"OK {file_path}: {injected}/{len(pages)} pages, {elapsed:.1f}s, save={mode}, errors={errors}")

        sioyek.reload()
        status.set(f"OCR 完成: 已为 {injected} 页注入文字层（{elapsed:.0f}s），智能跳转/搜索/划词现已可用",
                   force=True)
        time.sleep(5)
        status.clear()
    except Exception as e:
        log("ERROR " + file_path + "\n" + traceback.format_exc())
        status.set(f"OCR 失败: {e}", force=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            os.remove(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
