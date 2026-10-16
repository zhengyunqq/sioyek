"""
Preserve and restore PDF-embedded annotations (Markdown cards/pills, FreeText boxes,
and external PDF annotations such as Highlights, Notes, Underlines, Stamps, Ink, etc.)
across LaTeX recompilations.

Snapshots are stored in `<pdf_dir>/.sioyek_bak/<pdf_stem>.pdf_annots.json`.
Each annotation stores both its page/rect coordinates and an optional SyncTeX anchor
(`tex_file`, `line`, `ref_page`, `ref_y`, `delta_y`) so that if LaTeX edits shift a
paragraph vertically or across pages, the annotation automatically follows the text.
"""
import os
import sys
import json
import base64
import shutil
import glob
import subprocess
import tempfile
import fitz

BACKUP_DIR_NAME = ".sioyek_bak"
LOG_PATH = os.path.expanduser("~/.config/sioyek/add_text.log")

# Annotation subtypes that belong to user annotations (excluding Link/Widget/Popup/Watermark)
PRESERVED_SUBTYPES = {
    "Stamp", "FreeText", "Text", "Highlight", "Underline",
    "StrikeOut", "Squiggly", "Ink", "Square", "Circle",
    "Line", "PolyLine", "Polygon", "Caret",
}


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[preserve_annots] {msg}\n")
    except Exception:
        pass


def get_snapshot_path(pdf_path):
    pdf_path = os.path.abspath(pdf_path)
    pdf_dir = os.path.dirname(pdf_path)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    bdir = os.path.join(pdf_dir, BACKUP_DIR_NAME)
    return os.path.join(bdir, f"{stem}.pdf_annots.json")


def find_synctex_binary():
    for cand in ("/Library/TeX/texbin/synctex", "/usr/local/texlive/bin/synctex"):
        if os.path.exists(cand):
            return cand
    found = shutil.which("synctex")
    if found:
        return found
    for cand in glob.glob("/usr/local/texlive/*/bin/*/synctex"):
        return cand
    return None


def has_synctex_data(pdf_path):
    base = os.path.splitext(pdf_path)[0]
    return os.path.exists(base + ".synctex.gz") or os.path.exists(base + ".synctex")


def synctex_backward(synctex_bin, pdf_path, page0, x, y):
    """Maps PDF (page0, x, y) -> (rel_tex_path, line_1based) or None."""
    try:
        out = subprocess.run(
            [synctex_bin, "edit", "-o", f"{page0 + 1}:{x:.2f}:{y:.2f}:{pdf_path}"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(pdf_path) or None,
        ).stdout
    except Exception:
        return None
    cur_input = None
    for raw in out.splitlines():
        if raw.startswith("Input:"):
            cur_input = raw[len("Input:"):].strip()
        elif raw.startswith("Line:") and cur_input:
            try:
                line = int(raw[len("Line:"):].strip())
            except ValueError:
                continue
            if line > 0:
                abs_p = cur_input if os.path.isabs(cur_input) else os.path.join(os.path.dirname(pdf_path), cur_input)
                abs_p = os.path.normpath(abs_p)
                if os.path.exists(abs_p):
                    rel_p = os.path.relpath(abs_p, os.path.dirname(pdf_path))
                    return rel_p, line
    return None


def synctex_forward(synctex_bin, pdf_path, rel_tex_path, line):
    """Maps (rel_tex_path, line) -> (page0, min_v_on_page) in pdf_path or None."""
    abs_tex = os.path.normpath(os.path.join(os.path.dirname(pdf_path), rel_tex_path))
    for query_tex in (abs_tex, rel_tex_path):
        try:
            out = subprocess.run(
                [synctex_bin, "view", "-i", f"{line}:0:{query_tex}", "-o", pdf_path],
                capture_output=True, text=True, timeout=5,
                cwd=os.path.dirname(pdf_path) or None,
            ).stdout
        except Exception:
            continue
        cur_page = None
        hits = []
        for raw in out.splitlines():
            if raw.startswith("Page:"):
                try:
                    cur_page = int(raw[len("Page:"):].strip()) - 1
                except ValueError:
                    cur_page = None
            elif raw.startswith("v:") and cur_page is not None and cur_page >= 0:
                try:
                    v = float(raw[len("v:"):].strip())
                    hits.append((cur_page, v))
                except ValueError:
                    continue
        if hits:
            first_page = hits[0][0]
            min_v = min(v for p, v in hits if p == first_page)
            return first_page, min_v
    return None


def get_tex_line_snippet(pdf_path, rel_tex_path, line):
    abs_tex = os.path.normpath(os.path.join(os.path.dirname(pdf_path), rel_tex_path))
    if not os.path.exists(abs_tex):
        return ""
    try:
        with open(abs_tex, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()[:160]
    except Exception:
        pass
    return ""


def resolve_current_tex_line(pdf_path, rel_tex_path, saved_line, saved_line_text):
    """If lines were inserted/deleted in rel_tex_path, finds the new 1-based line number matching saved_line_text."""
    if not saved_line_text:
        return saved_line
    abs_tex = os.path.normpath(os.path.join(os.path.dirname(pdf_path), rel_tex_path))
    if not os.path.exists(abs_tex):
        return saved_line
    try:
        with open(abs_tex, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip()[:160] for l in f.readlines()]
    except Exception:
        return saved_line
    if not lines:
        return saved_line
    idx0 = max(0, min(len(lines) - 1, saved_line - 1))
    if lines[idx0] == saved_line_text:
        return saved_line

    import difflib
    best_line = saved_line
    best_score = 0.0
    for i, l_str in enumerate(lines):
        if not l_str:
            continue
        if l_str == saved_line_text or (len(saved_line_text) >= 20 and (saved_line_text in l_str or l_str in saved_line_text)):
            ratio = 1.0
        else:
            ratio = difflib.SequenceMatcher(None, saved_line_text, l_str).ratio()
        if ratio >= 0.65:
            # Prefer higher similarity, breaking ties by proximity to saved_line
            score = ratio - abs(i - idx0) * 0.001
            if score > best_score:
                best_score = score
                best_line = i + 1
    return best_line


def compute_synctex_anchor(pdf_path, page0, rect):
    """Computes SyncTeX anchor dict for an annotation at (page0, rect)."""
    if not has_synctex_data(pdf_path):
        return None
    synctex_bin = find_synctex_binary()
    if not synctex_bin:
        return None
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    # If the annotation is in the left or right margin, probe the main text column first at y0
    if cx < 135.0 or cx > 440.0:
        probe_points = [(250.0, y0 + 6.0), (250.0, cy), (300.0, y0 + 6.0), (cx, cy)]
    else:
        probe_points = [(cx, cy), (cx, y0 + 6.0), (250.0, y0 + 6.0), (300.0, cy)]
    for px, py in probe_points:
        hit = synctex_backward(synctex_bin, pdf_path, page0, px, py)
        if hit:
            rel_tex, line = hit
            fwd = synctex_forward(synctex_bin, pdf_path, rel_tex, line)
            if fwd is not None:
                ref_page, ref_y = fwd
                # Ensure the anchor belongs to the same page (or adjacent page for page-spanning paragraphs)
                if abs(ref_page - page0) <= 1:
                    return {
                        "tex_file": rel_tex,
                        "line": line,
                        "line_text": get_tex_line_snippet(pdf_path, rel_tex, line),
                        "ref_page": ref_page,
                        "ref_y": round(ref_y, 2),
                        "delta_y": round(y0 - ref_y, 2),
                    }
    return None


def extract_annotations_from_doc(doc, pdf_path, existing_entries=None):
    """Extracts all user annotations from an open fitz.Document."""
    # Map (page, round_rect, subtype, content) -> existing synctex_anchor so we don't re-query unchanged annots
    anchor_cache = {}
    if existing_entries:
        for ent in existing_entries:
            r = ent.get("rect", [0, 0, 0, 0])
            key = (ent.get("page"), tuple(round(v, 1) for v in r), ent.get("subtype"), ent.get("info", {}).get("content", ""))
            if ent.get("synctex_anchor"):
                anchor_cache[key] = ent["synctex_anchor"]

    entries = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        for annot in page.annots():
            subtype = annot.type[1]
            if subtype not in PRESERVED_SUBTYPES:
                continue
            r = [round(annot.rect.x0, 2), round(annot.rect.y0, 2), round(annot.rect.x1, 2), round(annot.rect.y1, 2)]
            info = dict(annot.info or {})
            colors = dict(annot.colors or {})
            opacity = annot.opacity if annot.opacity is not None and annot.opacity >= 0 else 1.0
            border = dict(annot.border or {})

            entry = {
                "page": page_idx,
                "rect": r,
                "subtype": subtype,
                "info": info,
                "colors": {
                    "stroke": list(colors.get("stroke")) if colors.get("stroke") else None,
                    "fill": list(colors.get("fill")) if colors.get("fill") else None,
                },
                "opacity": opacity,
                "border_width": border.get("width", 0),
            }

            if subtype == "Stamp":
                try:
                    pix = annot.get_pixmap(dpi=216, alpha=True)
                    entry["stamp_png_b64"] = base64.b64encode(pix.tobytes("png")).decode("ascii")
                except Exception as e:
                    log(f"Failed to capture stamp pixmap: {e}")

            if subtype in ("Highlight", "Underline", "StrikeOut", "Squiggly", "Ink", "Line", "PolyLine", "Polygon"):
                verts = annot.vertices
                if verts:
                    entry["vertices"] = [list(v) if isinstance(v, (tuple, list)) else v for v in verts]

            cache_key = (page_idx, tuple(round(v, 1) for v in r), subtype, info.get("content", ""))
            anchor = anchor_cache.get(cache_key)
            if anchor is None:
                anchor = compute_synctex_anchor(pdf_path, page_idx, r)
            if anchor:
                entry["synctex_anchor"] = anchor

            entries.append(entry)
    return entries


def load_snapshot(pdf_path):
    spath = get_snapshot_path(pdf_path)
    if not os.path.exists(spath):
        return None
    try:
        with open(spath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_snapshot_data(pdf_path, entries):
    spath = get_snapshot_path(pdf_path)
    os.makedirs(os.path.dirname(spath), exist_ok=True)
    data = {
        "version": 1,
        "pdf": os.path.basename(pdf_path),
        "annotations": entries,
    }
    fd, tmp = tempfile.mkstemp(prefix=".sioyek_annots_", dir=os.path.dirname(spath))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, spath)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def sync_snapshot(pdf_path, allow_empty=False):
    """
    Reads current annotations from `pdf_path` and saves them to `.sioyek_bak/<stem>.pdf_annots.json`.
    If `allow_empty=True` (e.g. called after user explicitly deleted an annotation), an empty list
    will overwrite the snapshot so deleted annotations do not come back.
    """
    if not os.path.exists(pdf_path):
        return 0
    prev = load_snapshot(pdf_path)
    prev_entries = prev.get("annotations", []) if prev else []
    doc = fitz.open(pdf_path)
    try:
        entries = extract_annotations_from_doc(doc, pdf_path, existing_entries=prev_entries)
    finally:
        doc.close()

    if entries or allow_empty:
        save_snapshot_data(pdf_path, entries)
        log(f"Saved snapshot for {os.path.basename(pdf_path)}: {len(entries)} annotation(s)")
    return len(entries)


def resolve_target_page_and_rect(doc, pdf_path, entry, synctex_bin):
    """
    Determines the target (page_idx, fitz.Rect) for an annotation entry in `doc`.
    Uses SyncTeX forward lookup if available and if the anchored line moved across pages or vertically.
    """
    orig_page = entry.get("page", 0)
    r = list(entry.get("rect", [100, 100, 200, 130]))
    w = r[2] - r[0]
    h = r[3] - r[1]

    target_page = min(max(0, orig_page), len(doc) - 1)
    target_x0 = r[0]
    target_y0 = r[1]

    anchor = entry.get("synctex_anchor")
    if anchor and synctex_bin and has_synctex_data(pdf_path):
        rel_tex = anchor.get("tex_file")
        line = anchor.get("line")
        ref_page = anchor.get("ref_page", orig_page)
        ref_y = anchor.get("ref_y", r[1])
        delta_y = anchor.get("delta_y", 0.0)
        if rel_tex and line:
            cur_line = resolve_current_tex_line(pdf_path, rel_tex, line, anchor.get("line_text", ""))
            fwd = synctex_forward(synctex_bin, pdf_path, rel_tex, cur_line)
            if fwd is not None:
                new_page, new_ref_y = fwd
                if 0 <= new_page < len(doc):
                    # If the anchored .tex line moved to another page or shifted vertically by > 4pt
                    if new_page != ref_page or abs(new_ref_y - ref_y) > 4.0:
                        target_page = new_page
                        target_y0 = new_ref_y + delta_y

    page_rect = doc[target_page].rect
    # Clamp within page vertical bounds
    if target_y0 < page_rect.y0 + 4:
        target_y0 = page_rect.y0 + 4
    if target_y0 + h > page_rect.y1 - 4:
        target_y0 = max(page_rect.y0 + 4, page_rect.y1 - h - 4)

    return target_page, fitz.Rect(target_x0, target_y0, target_x0 + w, target_y0 + h)


def embed_entry_into_doc(doc, pdf_path, entry, synctex_bin):
    """Embeds a single saved annotation entry into `doc`."""
    subtype = entry.get("subtype")
    info = entry.get("info", {})
    colors = entry.get("colors", {})
    opacity = entry.get("opacity", 1.0)
    target_page, target_rect = resolve_target_page_and_rect(doc, pdf_path, entry, synctex_bin)
    page = doc[target_page]

    orig_page = entry.get("page", target_page)
    orig_rect = entry.get("rect", [target_rect.x0, target_rect.y0, target_rect.x1, target_rect.y1])
    dy = target_rect.y0 - orig_rect[1]
    dx = target_rect.x0 - orig_rect[0]

    def shift_pts(verts):
        if not verts:
            return None
        out = []
        for v in verts:
            if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], (int, float)):
                out.append((v[0] + dx, v[1] + dy))
            elif isinstance(v, (list, tuple)):
                out.append([(p[0] + dx, p[1] + dy) for p in v])
        return out

    annot = None
    if subtype == "Stamp":
        png_bytes = None
        subj = info.get("subject", "")
        content = info.get("content", "")
        if entry.get("stamp_png_b64"):
            try:
                png_bytes = base64.b64decode(entry["stamp_png_b64"])
            except Exception:
                png_bytes = None
        if png_bytes is None and subj in ("sioyek_markdown_card", "sioyek_markdown_pill") and content:
            try:
                from .add_markdown import render_card_to_png_bytes, render_pill_badge_to_png_bytes
                if subj == "sioyek_markdown_pill":
                    png_bytes, _, _ = render_pill_badge_to_png_bytes(content)
                else:
                    png_bytes, _, _ = render_card_to_png_bytes(content, target_rect.width)
            except Exception as e:
                log(f"Markdown re-render fallback failed: {e}")
        if png_bytes:
            annot = page.add_stamp_annot(target_rect, stamp=png_bytes)

    elif subtype == "FreeText":
        content = info.get("content", "")
        stroke_c = tuple(colors["stroke"]) if colors.get("stroke") else (1.0, 0.0, 0.0)
        fill_c = tuple(colors["fill"]) if colors.get("fill") else None
        fontname = "china-s" if any(ord(ch) > 127 for ch in content) else "helv"
        annot = page.add_freetext_annot(
            target_rect,
            content,
            fontsize=11.0,
            fontname=fontname,
            text_color=stroke_c,
            fill_color=fill_c,
        )

    elif subtype == "Text":
        annot = page.add_text_annot(target_rect.tl, info.get("content", ""))

    elif subtype in ("Highlight", "Underline", "StrikeOut", "Squiggly"):
        verts = shift_pts(entry.get("vertices"))
        quads = fitz. points_to_quads(verts) if (verts and hasattr(fitz, "points_to_quads")) else target_rect
        try:
            if subtype == "Highlight":
                annot = page.add_highlight_annot(quads)
            elif subtype == "Underline":
                annot = page.add_underline_annot(quads)
            elif subtype == "StrikeOut":
                annot = page.add_strikeout_annot(quads)
            elif subtype == "Squiggly":
                annot = page.add_squiggly_annot(quads)
        except Exception:
            annot = page.add_highlight_annot(target_rect)

    elif subtype == "Square":
        annot = page.add_rect_annot(target_rect)

    elif subtype == "Circle":
        annot = page.add_circle_annot(target_rect)

    elif subtype == "Caret":
        annot = page.add_caret_annot(target_rect.tl)

    elif subtype == "Ink":
        verts = shift_pts(entry.get("vertices"))
        if verts:
            annot = page.add_ink_annot(verts)

    if annot is not None:
        annot.set_info({
            "content": info.get("content", ""),
            "title": info.get("title", ""),
            "subject": info.get("subject", ""),
        })
        if subtype != "Stamp" and subtype != "FreeText":
            stroke_c = tuple(colors["stroke"]) if colors.get("stroke") else None
            fill_c = tuple(colors["fill"]) if colors.get("fill") else None
            if stroke_c or fill_c:
                try:
                    annot.set_colors(stroke=stroke_c, fill=fill_c)
                except Exception:
                    pass
        if opacity is not None and 0.0 <= opacity < 1.0:
            try:
                annot.set_opacity(opacity)
            except Exception:
                pass
        annot.update()
        return True
    return False


def restore_or_sync_pdf_annotations(pdf_path):
    """
    Main entry point called when Sioyek opens or reloads `pdf_path`:
    1. If `pdf_path` has 0 user annotations (e.g. freshly compiled by LaTeX) and
       `.sioyek_bak/<stem>.pdf_annots.json` has saved annotations, restores all saved
       annotations into `pdf_path` (adjusting positions via SyncTeX if text shifted)
       and updates the snapshot's SyncTeX anchors.
    2. Otherwise, if `pdf_path` has user annotations on disk (e.g. added in an external
       PDF viewer or existing before snapshot was created), snapshots them to
       `.sioyek_bak/<stem>.pdf_annots.json`.
    Returns the number of annotations restored (or 0 if none needed restoring).
    """
    if not os.path.exists(pdf_path):
        return 0

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log(f"Could not open {pdf_path}: {e}")
        return 0

    try:
        current_annots = []
        for page_idx in range(len(doc)):
            for annot in doc[page_idx].annots():
                if annot.type[1] in PRESERVED_SUBTYPES:
                    current_annots.append((page_idx, annot))

        snap = load_snapshot(pdf_path)
        saved_entries = snap.get("annotations", []) if snap else []

        # Case 1: Freshly recompiled PDF (0 annotations on disk, saved annotations in snapshot)
        if len(current_annots) == 0 and len(saved_entries) > 0:
            synctex_bin = find_synctex_binary() if has_synctex_data(pdf_path) else None
            restored = 0
            for entry in saved_entries:
                try:
                    if embed_entry_into_doc(doc, pdf_path, entry, synctex_bin):
                        restored += 1
                except Exception as e:
                    log(f"Failed to restore annotation {entry.get('subtype')}: {e}")
            if restored > 0:
                doc.saveIncr()
                # Refresh anchors after restoration
                updated_entries = extract_annotations_from_doc(doc, pdf_path, existing_entries=saved_entries)
                save_snapshot_data(pdf_path, updated_entries)
                log(f"Restored {restored}/{len(saved_entries)} annotation(s) into recompiled {os.path.basename(pdf_path)}")
            return restored

        # Case 2: PDF on disk has annotations (e.g. external PDF annotations or existing ones)
        if len(current_annots) > 0 and len(current_annots) != len(saved_entries):
            entries = extract_annotations_from_doc(doc, pdf_path, existing_entries=saved_entries)
            save_snapshot_data(pdf_path, entries)
            log(f"Synced {len(entries)} annotation(s) from {os.path.basename(pdf_path)} into snapshot")
        return 0
    finally:
        doc.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)
    target_pdf = sys.argv[1].strip().strip('"').strip("'")
    mode = sys.argv[2] if len(sys.argv) > 2 else "--auto"
    if mode == "--snapshot":
        sync_snapshot(target_pdf, allow_empty=True)
    else:
        restore_or_sync_pdf_annotations(target_pdf)
