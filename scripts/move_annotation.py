"""
move_annotation.py - Reposition a Markdown annotation in the PDF.
Called by Sioyek after dragging a pill badge or card on the PDF page.
"""

import sys
import os
import pymupdf as fitz

try:
    from .sioyek import Sioyek, clean_path
except ImportError:
    from sioyek import Sioyek, clean_path


def rect_distance_to_point(rect, pt):
    """Calculates Euclidean distance from a point to a rectangle."""
    dx = max(rect.x0 - pt.x, 0.0, pt.x - rect.x1)
    dy = max(rect.y0 - pt.y, 0.0, pt.y - rect.y1)
    return (dx * dx + dy * dy) ** 0.5


def main():
    if len(sys.argv) < 6:
        return

    SIOYEK_PATH = clean_path(sys.argv[1])
    LOCAL_DATABASE_PATH = clean_path(sys.argv[2])
    SHARED_DATABASE_PATH = clean_path(sys.argv[3])
    FILE_PATH = clean_path(sys.argv[4])
    raw_coords = clean_path(sys.argv[5])

    # raw_coords format: "page orig_x0 orig_y0 new_x0 new_y0"
    tokens = raw_coords.replace(',', ' ').split()
    if len(tokens) < 5:
        return

    try:
        page_num = int(float(tokens[0]))
        orig_x0 = float(tokens[1])
        orig_y0 = float(tokens[2])
        new_x0 = float(tokens[3])
        new_y0 = float(tokens[4])
    except ValueError:
        return

    try:
        doc = fitz.open(FILE_PATH)
    except Exception:
        return

    if page_num < 0 or page_num >= len(doc):
        doc.close()
        return

    page = doc[page_num]

    # Handle page crop/media origin offset
    if page.rect.x0 != 0 or page.rect.y0 != 0:
        orig_x0 += page.rect.x0
        orig_y0 += page.rect.y0
        new_x0 += page.rect.x0
        new_y0 += page.rect.y0

    orig_pt = fitz.Point(orig_x0, orig_y0)

    # Search for the target annotation on this page
    candidates = []
    for annot in page.annots():
        if annot.type[1] == 'Stamp':
            subj = annot.info.get('subject', '')
            if subj in ('sioyek_markdown_pill', 'sioyek_markdown_card', 'sioyek_markdown'):
                # Check top-left delta or distance to point
                tl_dist = ((annot.rect.x0 - orig_x0) ** 2 + (annot.rect.y0 - orig_y0) ** 2) ** 0.5
                pt_dist = rect_distance_to_point(annot.rect, orig_pt)
                score = min(tl_dist, pt_dist)
                if annot.rect.contains(orig_pt) or score <= 40.0:
                    candidates.append((score, annot))

    if not candidates:
        doc.close()
        return

    candidates.sort(key=lambda x: x[0])
    target_annot = candidates[0][1]

    # Calculate new bounding box preserving original width and height
    w = target_annot.rect.width
    h = target_annot.rect.height
    new_rect = fitz.Rect(new_x0, new_y0, new_x0 + w, new_y0 + h)

    target_annot.set_rect(new_rect)
    target_annot.update()
    doc.saveIncr()
    doc.close()

    sioyek = Sioyek(SIOYEK_PATH, LOCAL_DATABASE_PATH, SHARED_DATABASE_PATH)
    try:
        sioyek.reload()
        sioyek.set_status_string("已更新批注位置")
    except Exception:
        pass


if __name__ == "__main__":
    main()
