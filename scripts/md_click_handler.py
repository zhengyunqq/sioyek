"""
md_click_handler.py - Sioyek Markdown Note Inspector & Reader Dialog

Triggered by Option + Left Click (⌥ + Click) on any Markdown annotation
(pill badge or expanded card) in Sioyek.
Pops up an expansive 780x560 reader window with rich LaTeX/Markdown rendering
and one-click seamless transition to editing.
"""

import sys
import os
import pymupdf as fitz
from PyQt5.QtWidgets import QApplication

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
    log_path = os.path.expanduser("~/.config/sioyek/md_click.log")

    page_num, mouse_x, mouse_y = parse_mouse_pos(raw_mouse_args)
    if page_num is None:
        sys.exit(0)

    try:
        doc = fitz.open(FILE_PATH)
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Error opening document {FILE_PATH}: {e}\n")
        sys.exit(0)

    if page_num < 0 or page_num >= len(doc):
        doc.close()
        sys.exit(0)

    page = doc[page_num]
    click_x = mouse_x + page.rect.x0
    click_y = mouse_y + page.rect.y0
    click_pt = fitz.Point(click_x, click_y)

    # Search for Markdown or FreeText/Text annotations under or near click position
    candidates = []
    all_annots = list(page.annots())
    for annot in all_annots:
        atype = annot.type[1]
        is_md = False
        if atype == 'Stamp':
            subj = annot.info.get('subject', '')
            if subj in ('sioyek_markdown', 'sioyek_markdown_pill', 'sioyek_markdown_card'):
                is_md = True
        elif atype in ('FreeText', 'Text'):
            is_md = True
        if is_md:
            if annot.rect.contains(click_pt):
                area = annot.rect.width * annot.rect.height
                score = 100000.0 - (area * 0.001)
                candidates.append((score, annot))
            else:
                dist = rect_distance_to_point(annot.rect, click_pt)
                if dist <= 35.0:
                    score = 1000.0 - dist
                    candidates.append((score, annot))

    if not candidates:
        # Not clicking on a note -> Quietly exit
        doc.close()
        sys.exit(0)

    # Sort best matching annotation
    candidates.sort(key=lambda x: x[0], reverse=True)
    target_annot = candidates[0][1]
    md_text = target_annot.info.get('content', '')
    subj = target_annot.info.get('subject', '')
    existing_mode = 'pill' if subj == 'sioyek_markdown_pill' else 'card'

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

    def update_annot_snapshot():
        try:
            try:
                from .preserve_pdf_annotations import sync_snapshot
            except ImportError:
                from preserve_pdf_annotations import sync_snapshot
            sync_snapshot(FILE_PATH, allow_empty=True)
        except Exception:
            pass

    def do_promote_to_tex(text_to_promote):
        annot_rect = list(target_annot.rect)
        try:
            try:
                from .add_study_note import promote_annotation_to_study_note
            except ImportError:
                from add_study_note import promote_annotation_to_study_note
            tex_msg = promote_annotation_to_study_note(FILE_PATH, page_num, annot_rect, text_to_promote)
            page.delete_annot(target_annot)
            doc.saveIncr()
            doc.close()
            update_annot_snapshot()
            safe_notify(f"已转为 TeX 批注：{tex_msg}")
        except Exception as ex:
            doc.close()
            safe_notify(f"转为 TeX 批注失败：{ex}")

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

    if reader_action == "to_tex":
        do_promote_to_tex(md_text)
        app.quit()
        sys.exit(0)

    if reader_action == "edit":
        # Transition seamlessly to Markdown Editor Dialog
        init_w = target_annot.rect.width if (existing_mode == 'card' and target_annot.rect.width > 100) else 260.0
        editor = MarkdownEditorDialog(
            initial_text=md_text,
            is_edit=True,
            initial_width=init_w,
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

        if editor_action == "cancel":
            doc.close()
            app.quit()
            sys.exit(0)

        if editor_action == "to_tex":
            do_promote_to_tex((editor_text or md_text).strip())
            app.quit()
            sys.exit(0)

        if editor_action == "delete":
            page.delete_annot(target_annot)
            doc.saveIncr()
            doc.close()
            update_annot_snapshot()
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
                update_annot_snapshot()
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
            update_annot_snapshot()

            msg = f"Updated Markdown {new_mode}"
            safe_notify(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Success: {msg} on page {page_num} rect {final_rect}\n")

            app.quit()
            sys.exit(0)

    doc.close()
    app.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
