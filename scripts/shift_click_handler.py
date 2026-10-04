"""
Sioyek Shift+Click Handler
===========================
Smart router for Shift + Left Click in Sioyek:
1. If clicking on/near a Markdown annotation (Pill Badge or Card), opens the large
   Markdown Reader window with rich HTML/LaTeX rendering and one-click edit support.
2. If clicking on standard PDF text/elements (no Markdown note), seamlessly falls back
   to Sioyek's built-in `overview_under_cursor` preview.
"""

import sys
import os
import fitz

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

try:
    from .add_markdown import (
        MarkdownReaderDialog,
        MarkdownEditorDialog,
        render_pill_badge_to_png_bytes,
        render_card_to_png_bytes,
        setup_macos_accessory,
    )
    from .sioyek import Sioyek, clean_path
except ImportError:
    from add_markdown import (
        MarkdownReaderDialog,
        MarkdownEditorDialog,
        render_pill_badge_to_png_bytes,
        render_card_to_png_bytes,
        setup_macos_accessory,
    )
    from sioyek import Sioyek, clean_path


def rect_distance_to_point(rect, pt):
    """Calculates Euclidean distance from a point to a rectangle."""
    dx = max(rect.x0 - pt.x, 0.0, pt.x - rect.x1)
    dy = max(rect.y0 - pt.y, 0.0, pt.y - rect.y1)
    return (dx * dx + dy * dy) ** 0.5


def parse_mouse_pos(raw_args):
    """Parses Sioyek %{mouse_pos_document} into (page, x, y)."""
    tokens = []
    for arg in raw_args:
        cleaned = clean_path(arg).strip().replace(',', ' ')
        tokens.extend(cleaned.split())
    if len(tokens) >= 3:
        try:
            page = int(float(tokens[0]))
            x = float(tokens[1])
            y = float(tokens[2])
            return page, x, y
        except ValueError:
            pass
    return None, None, None


def main():
    if len(sys.argv) < 5:
        return

    SIOYEK_PATH = clean_path(sys.argv[1])
    LOCAL_DATABASE_PATH = clean_path(sys.argv[2])
    SHARED_DATABASE_PATH = clean_path(sys.argv[3])
    FILE_PATH = clean_path(sys.argv[4])
    raw_mouse_args = sys.argv[5:]

    sioyek = Sioyek(SIOYEK_PATH, LOCAL_DATABASE_PATH, SHARED_DATABASE_PATH)
    log_path = os.path.expanduser("~/.config/sioyek/shift_click.log")

    page_num, mouse_x, mouse_y = parse_mouse_pos(raw_mouse_args)
    if page_num is None:
        # Unable to parse mouse pos, fallback to native overview
        sioyek.run_command("overview_under_cursor")
        sys.exit(0)

    try:
        doc = fitz.open(FILE_PATH)
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Error opening document {FILE_PATH}: {e}\n")
        sioyek.run_command("overview_under_cursor")
        sys.exit(0)

    if page_num < 0 or page_num >= len(doc):
        doc.close()
        sioyek.run_command("overview_under_cursor")
        sys.exit(0)

    page = doc[page_num]
    click_x = mouse_x + page.rect.x0
    click_y = mouse_y + page.rect.y0
    click_pt = fitz.Point(click_x, click_y)

    # Search for Markdown annotations under or near click position
    candidates = []
    for annot in page.annots():
        if annot.type[1] == 'Stamp':
            subj = annot.info.get('subject', '')
            if subj in ('sioyek_markdown', 'sioyek_markdown_pill', 'sioyek_markdown_card'):
                if annot.rect.contains(click_pt):
                    # Direct hit: prioritize smaller bounding boxes if overlapping
                    area = annot.rect.width * annot.rect.height
                    score = 100000.0 - (area * 0.001)
                    candidates.append((score, annot))
                else:
                    dist = rect_distance_to_point(annot.rect, click_pt)
                    if dist <= 20.0:
                        score = 1000.0 - dist
                        candidates.append((score, annot))

    if not candidates:
        # Not clicking on a Markdown note -> Fall back to Sioyek's built-in overview
        doc.close()
        sioyek.run_command("overview_under_cursor")
        sys.exit(0)

    # Sort best matching Markdown annotation
    candidates.sort(key=lambda x: x[0], reverse=True)
    target_annot = candidates[0][1]
    md_text = target_annot.info.get('content', '')
    subj = target_annot.info.get('subject', '')
    existing_mode = 'pill' if subj == 'sioyek_markdown_pill' else 'card'

    # Launch large popup Markdown Reader Dialog
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)
    setup_macos_accessory()

    reader = MarkdownReaderDialog(md_text, page_num=page_num + 1)
    reader.exec_()
    reader_action = reader.action
    reader.close()
    reader.deleteLater()

    if reader_action == "close":
        doc.close()
        app.quit()
        sys.exit(0)

    if reader_action == "edit":
        # Transition seamlessly to Markdown Editor Dialog
        editor = MarkdownEditorDialog(
            initial_text=md_text,
            is_edit=True,
            initial_width=target_annot.rect.width,
            page_num=page_num + 1,
            initial_mode=existing_mode
        )
        editor.exec_()
        editor_action = editor.action
        editor_text = editor.get_text()
        editor_mode = editor.get_mode()
        editor_width = editor.get_width()
        editor.close()
        editor.deleteLater()

        def safe_notify(msg=None):
            try:
                sioyek.reload()
            except Exception:
                pass
            try:
                if msg:
                    sioyek.set_status_string(msg)
            except Exception:
                pass

        if editor_action == "cancel":
            doc.close()
            app.quit()
            sys.exit(0)

        if editor_action == "delete":
            page.delete_annot(target_annot)
            doc.saveIncr()
            doc.close()
            safe_notify("Deleted Markdown note")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Success: deleted note on page {page_num}\n")
            app.quit()
            sys.exit(0)

        if editor_action == "save":
            new_text = editor_text.strip()
            if not new_text:
                page.delete_annot(target_annot)
                doc.saveIncr()
                doc.close()
                safe_notify("Deleted Markdown note")
                app.quit()
                sys.exit(0)

            new_mode = editor_mode
            card_width = editor_width
            target_x0 = target_annot.rect.x0
            target_y0 = target_annot.rect.y0

            if new_mode == "pill":
                png_bytes, final_w, final_h = render_pill_badge_to_png_bytes(new_text)
                new_subj = "sioyek_markdown_pill"
            else:
                png_bytes, final_w, final_h = render_card_to_png_bytes(new_text, card_width)
                new_subj = "sioyek_markdown_card"

            page.delete_annot(target_annot)
            final_rect = fitz.Rect(target_x0, target_y0, target_x0 + final_w, target_y0 + final_h)
            new_annot = page.add_stamp_annot(final_rect, stamp=png_bytes)
            new_annot.set_info({
                'content': new_text,
                'subject': new_subj,
                'title': f'Markdown [{new_mode}]'
            })
            new_annot.update()
            doc.saveIncr()
            doc.close()

            msg = f"Updated Markdown {new_mode}"
            safe_notify(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Success: {msg} on page {page_num} rect {final_rect}\n")

            app.quit()
            sys.exit(0)

    doc.close()
    app.quit()
    sys.exit(0)
