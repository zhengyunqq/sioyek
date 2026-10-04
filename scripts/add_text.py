import sys
from .sioyek import Sioyek, clean_path
from collections import defaultdict

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

def parse_params(ps):
    res = dict()

    def default_creator():
        def default(x):
            return x
        return default

    def parse_color(s):
        c = tuple(float(part) for part in s.split(','))
        if any(v > 1.0 for v in c):
            c = tuple(v / 255.0 for v in c)
        return c

    key_validators = defaultdict(default_creator)
    key_validators['fontsize'] = float
    key_validators['text_color'] = parse_color
    key_validators['fill_color'] = parse_color
    key_validators['border_color'] = parse_color

    for param_string in ps:
        key, value = param_string.split('=')
        res[key] = key_validators[key](value)

    return res

if __name__ == '__main__':

    SIOYEK_PATH = clean_path(sys.argv[1])
    LOCAL_DATABASE_PATH = clean_path(sys.argv[2])
    SHARED_DATABASE_PATH = clean_path(sys.argv[3])
    FILE_PATH = clean_path(sys.argv[4])
    rect_string = clean_path(sys.argv[5])
    added_text = clean_path(sys.argv[6])

    params = parse_params(sys.argv[7:])

    try:
        sioyek = Sioyek(SIOYEK_PATH, LOCAL_DATABASE_PATH, SHARED_DATABASE_PATH)
        document = sioyek.get_document(FILE_PATH)
        selected_page, selected_rect = parse_rect(rect_string)

        pdf_page = document.get_page(selected_page)
        if pdf_page.rect.x0 != 0 or pdf_page.rect.y0 != 0:
            selected_rect[0] += pdf_page.rect.x0
            selected_rect[1] += pdf_page.rect.y0
            selected_rect[2] += pdf_page.rect.x0
            selected_rect[3] += pdf_page.rect.y0

        if not added_text.strip() or added_text.strip() in (':delete', ':del', ':d'):
            search_rect = fitz.Rect(selected_rect)
            if search_rect.width < 10 and search_rect.height < 10:
                search_rect.x0 -= 15
                search_rect.y0 -= 15
                search_rect.x1 += 15
                search_rect.y1 += 15
            deleted_count = 0
            for annot in list(pdf_page.annots()):
                if annot.type[1] in ('FreeText', 'Text') and annot.rect.intersects(search_rect):
                    pdf_page.delete_annot(annot)
                    deleted_count += 1
            if deleted_count > 0:
                document.save_changes()
                sioyek.reload()
                sioyek.set_status_string(f"Deleted {deleted_count} text annotation(s)")
            else:
                sioyek.set_status_string("No text annotation found to delete")
            sys.exit(0)

        fontsize = params.get('fontsize', 11.0)
        # Ensure minimum height and width so text is not clipped
        if len(selected_rect) == 4:
            min_h = max(16.0, fontsize * 1.4)
            if selected_rect[3] - selected_rect[1] < min_h:
                selected_rect[3] = selected_rect[1] + min_h
            min_w = max(40.0, float(len(added_text) * 10))
            if selected_rect[2] - selected_rect[0] < min_w:
                selected_rect[2] = selected_rect[0] + min_w

        # If text contains non-ASCII characters, use china-s font for CJK compatibility
        if any(ord(ch) > 127 for ch in added_text) and 'fontname' not in params:
            params['fontname'] = 'china-s'

        document.embed_text_in_pdf(added_text, selected_page, selected_rect, params)
        sioyek.reload()
        import os
        log_path = os.path.expanduser("~/.config/sioyek/add_text.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Success: embedded '{added_text}' on page {selected_page} rect {selected_rect}\n")
    except Exception as e:
        import traceback
        import os
        log_path = os.path.expanduser("~/.config/sioyek/add_text.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Error: {e}\n")
            traceback.print_exc(file=f)
        raise
