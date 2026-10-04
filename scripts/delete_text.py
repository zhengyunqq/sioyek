import sys
import os
import fitz
from .sioyek import Sioyek, clean_path

def parse_rect(s):
    s = clean_path(s).strip()
    parts = s.split(',')
    page = int(clean_path(parts[0]).strip())
    raw_rect = [float(clean_path(part).strip()) for part in parts[1:]]
    if len(raw_rect) == 4:
        rect = [
            min(raw_rect[0], raw_rect[2]),
            min(raw_rect[1], raw_rect[3]),
            max(raw_rect[0], raw_rect[2]),
            max(raw_rect[1], raw_rect[3]),
        ]
    else:
        rect = raw_rect
    return page, rect

def is_deletable_annot(annot):
    if annot.type[1] in ('FreeText', 'Text'):
        return True
    if annot.type[1] == 'Stamp' and (annot.info.get('subject') in ('sioyek_markdown', 'sioyek_markdown_pill', 'sioyek_markdown_card') or annot.info.get('name') == 'ImageStamp'):
        return True
    return False

def main():
    if len(sys.argv) < 6:
        return

    SIOYEK_PATH = clean_path(sys.argv[1])
    LOCAL_DATABASE_PATH = clean_path(sys.argv[2])
    SHARED_DATABASE_PATH = clean_path(sys.argv[3])
    FILE_PATH = clean_path(sys.argv[4])
    rect_arg = clean_path(sys.argv[5])

    sioyek = Sioyek(SIOYEK_PATH, LOCAL_DATABASE_PATH, SHARED_DATABASE_PATH)
    doc = fitz.open(FILE_PATH)
    log_path = os.path.expanduser("~/.config/sioyek/add_text.log")

    def safe_notify(msg=None):
        try:
            sioyek.reload()
            if msg:
                sioyek.set_status_string(msg)
        except Exception:
            pass

    try:
        if rect_arg == 'last':
            target_page_num = -1
            if len(sys.argv) > 6:
                try:
                    target_page_num = int(clean_path(sys.argv[6]))
                except ValueError:
                    target_page_num = -1

            pages_to_search = [target_page_num] if 0 <= target_page_num < len(doc) else range(len(doc) - 1, -1, -1)
            found = False
            for pno in pages_to_search:
                page = doc[pno]
                target_annots = [a for a in page.annots() if is_deletable_annot(a)]
                if target_annots:
                    last_annot = target_annots[-1]
                    content = last_annot.info.get('content', '')
                    page.delete_annot(last_annot)
                    doc.saveIncr()
                    safe_notify(f"Deleted note: {content[:20]}")
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"Success: deleted last note '{content}' on page {pno}\n")
                    found = True
                    break
            if not found:
                safe_notify("No text annotation found to delete")
            return

        selected_page, selected_rect = parse_rect(rect_arg)
        if selected_page < 0 or selected_page >= len(doc):
            return

        page = doc[selected_page]

        if page.rect.x0 != 0 or page.rect.y0 != 0:
            selected_rect[0] += page.rect.x0
            selected_rect[1] += page.rect.y0
            selected_rect[2] += page.rect.x0
            selected_rect[3] += page.rect.y0

        search_rect = fitz.Rect(selected_rect)

        # If rect is tiny (click), expand slightly to make clicking easy
        if search_rect.width < 10 and search_rect.height < 10:
            search_rect.x0 -= 15
            search_rect.y0 -= 15
            search_rect.x1 += 15
            search_rect.y1 += 15

        deleted_count = 0
        deleted_contents = []

        for annot in list(page.annots()):
            if is_deletable_annot(annot):
                if annot.rect.intersects(search_rect):
                    content = annot.info.get('content', '')
                    deleted_contents.append(content)
                    page.delete_annot(annot)
                    deleted_count += 1

        # Fallback: if clicking didn't directly intersect, find closest annotation within 30 points
        if deleted_count == 0 and search_rect.width <= 40 and search_rect.height <= 40:
            click_point = fitz.Point((search_rect.x0 + search_rect.x1) / 2, (search_rect.y0 + search_rect.y1) / 2)
            best_annot = None
            best_dist = 30.0
            for annot in list(page.annots()):
                if is_deletable_annot(annot):
                    dist = annot.rect.distance_to_point(click_point)
                    if dist < best_dist:
                        best_dist = dist
                        best_annot = annot
            if best_annot:
                content = best_annot.info.get('content', '')
                deleted_contents.append(content)
                page.delete_annot(best_annot)
                deleted_count += 1

        if deleted_count > 0:
            doc.saveIncr()
            msg = f"Deleted {deleted_count} annotation(s)"
            if deleted_contents and deleted_contents[0]:
                msg += f": {deleted_contents[0][:20]}"
            safe_notify(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Success: deleted {deleted_count} annotations on page {selected_page}: {deleted_contents}\n")
        else:
            safe_notify("No text annotation found in selected area")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Notice: no annotations found to delete on page {selected_page} in rect {selected_rect}\n")

    except Exception as e:
        import traceback
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Error deleting annotation: {e}\n")
            traceback.print_exc(file=f)
        raise
    finally:
        doc.close()

if __name__ == '__main__':
    main()
