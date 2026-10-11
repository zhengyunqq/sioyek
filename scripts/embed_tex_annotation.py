"""
Automatically embed highlights directly into LaTeX source files
for SyncTeX-enabled PDF documents.

Commands supported:
    python -m sioyek.embed_tex_annotation highlight SIOYEK LOCAL_DB SHARED_DB PDF RECT TYPE TEXT
    python -m sioyek.embed_tex_annotation delete_highlight SIOYEK LOCAL_DB SHARED_DB PDF RECT TYPE TEXT
    python -m sioyek.embed_tex_annotation undo SIOYEK LOCAL_DB SHARED_DB PDF [highlight]
"""

import sys
import os
import re
import json
import glob
import shutil
import hashlib
import datetime
import traceback

from .sioyek import Sioyek, clean_path
from .add_study_note import (
    TexFile, atomic_write, make_backup, sha1,
    has_synctex_data, find_synctex_binary, synctex_query, resolve_source,
    map_stale_line, scan_states, find_insert_line, find_document_bounds,
    split_comment, strip_eol, find_root_file, BACKUP_DIR_NAME, BACKUP_KEEP,
)

LOG_PATH = os.path.expanduser("~/.config/sioyek/add_text.log")
ANNOT_HISTORY_PATH = os.path.expanduser("~/.config/sioyek/annotation_history.json")
ANNOT_HISTORY_KEEP = 50

MARKER_HL_BEGIN = "%% >>> sioyek highlight >>>"
MARKER_HL_END = "%% <<< sioyek highlight <<<"
HL_DEFINITION_LINES = [
    MARKER_HL_BEGIN,
    r"\makeatletter",
    r"\@ifpackageloaded{xcolor}{}{\@ifpackageloaded{color}{}{\usepackage{xcolor}}}",
    r"\@ifpackageloaded{amssymb}{}{\usepackage{amssymb}}",
    r"\@ifpackageloaded{graphicx}{}{\usepackage{graphicx}}",
    r"\colorlet{sioyek_a}{orange!85!black}",
    r"\colorlet{sioyek_b}{cyan!80!black}",
    r"\colorlet{sioyek_c}{green!70!black}",
    r"\colorlet{sioyek_d}{magenta!80!black}",
    r"\colorlet{sioyek_e}{orange!90!black}",
    r"\colorlet{sioyek_r}{red!80!black}",
    r"\colorlet{sioyek_g}{green!70!black}",
    r"\colorlet{sioyek_y}{orange!85!black}",
    r"\newcommand{\sioyekhlcolor@a}{sioyek_a}",
    r"\newcommand{\sioyekhlcolor@b}{sioyek_b}",
    r"\newcommand{\sioyekhlcolor@c}{sioyek_c}",
    r"\newcommand{\sioyekhlcolor@d}{sioyek_d}",
    r"\newcommand{\sioyekhlcolor@e}{sioyek_e}",
    r"\newcommand{\sioyekhlcolor@r}{sioyek_r}",
    r"\newcommand{\sioyekhlcolor@g}{sioyek_g}",
    r"\newcommand{\sioyekhlcolor@y}{sioyek_y}",
    r"\newif\ifsioyekhl \sioyekhltrue",
    r"% \sioyekhlfalse   % uncomment to hide all highlight marks",
    r"\newcommand{\sioyek@gethlcolor}[1]{%",
    r"  \@ifundefined{sioyekhlcolor@#1}{sioyek_a}{\csname sioyekhlcolor@#1\endcsname}%",
    r"}",
    r"\providecommand{\sioyekhlbeginmark}{\raisebox{0.1ex}{\scalebox{0.65}{$\blacktriangleright$}}}",
    r"\providecommand{\sioyekhlendmark}{\raisebox{0.1ex}{\scalebox{0.65}{$\blacktriangleleft$}}}",
    r"\providecommand{\sioyekhlbegin}[1][a]{%",
    r"  \ifsioyekhl%",
    r"    \ifmmode%",
    r"      {\color{\sioyek@gethlcolor{#1}}\raisebox{0.5ex}{\scalebox{0.6}{$\blacktriangleright$}}}%",
    r"    \else%",
    r"      \begingroup\color{\sioyek@gethlcolor{#1}}\textsuperscript{\sioyekhlbeginmark}\endgroup%",
    r"    \fi%",
    r"  \fi%",
    r"}",
    r"\providecommand{\sioyekhlend}[1][a]{%",
    r"  \ifsioyekhl%",
    r"    \ifmmode%",
    r"      {\color{\sioyek@gethlcolor{#1}}\raisebox{0.5ex}{\scalebox{0.6}{$\blacktriangleleft$}}}%",
    r"    \else%",
    r"      \begingroup\color{\sioyek@gethlcolor{#1}}\textsuperscript{\sioyekhlendmark}\endgroup%",
    r"    \fi%",
    r"  \fi%",
    r"}",
    r"% Aliases & backwards-compatibility",
    r"\providecommand{\sioyekhlmark}[2][a]{%",
    r"  \sioyekhlbegin[#1]#2\sioyekhlend[#1]%",
    r"}",
    r"\providecommand{\sioyekhl}[2][a]{%",
    r"  \sioyekhlbegin[#1]#2\sioyekhlend[#1]%",
    r"}",
    r"\makeatother",
    MARKER_HL_END,
]


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[embed_annot] {msg}\n")
    except Exception:
        pass


def notify_sioyek(sioyek, msg):
    if not msg:
        return
    log(msg)
    try:
        if sioyek is not None:
            sioyek.set_status_string(msg)
    except Exception:
        pass


def load_annot_history():
    try:
        with open(ANNOT_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_annot_history(hist):
    try:
        os.makedirs(os.path.dirname(ANNOT_HISTORY_PATH), exist_ok=True)
        with open(ANNOT_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist[-ANNOT_HISTORY_KEEP:], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def commit_annot_files(texfiles, pdf_path, kind, description):
    entry = {
        "kind": kind,
        "pdf": pdf_path,
        "time": datetime.datetime.now().isoformat(),
        "desc": description,
        "files": []
    }
    for tf in texfiles:
        new_data = tf.to_bytes()
        if new_data == tf.original_raw:
            continue
        bpath = make_backup(tf.path, tf.original_raw)
        atomic_write(tf.path, new_data)
        entry["files"].append({
            "path": tf.path,
            "backup": bpath,
            "after_sha1": sha1(new_data)
        })
    if entry["files"]:
        hist = load_annot_history()
        hist.append(entry)
        save_annot_history(hist)
    return entry


def inject_hl_definition(tf):
    # If definition block already exists, check if it needs upgrading to sioyekhlbegin / sioyekhlend
    for i, line in enumerate(tf.lines):
        if MARKER_HL_BEGIN in line:
            end_idx = -1
            has_begin = False
            for j in range(i, len(tf.lines)):
                if r"\sioyekhlbegin" in tf.lines[j]:
                    has_begin = True
                if MARKER_HL_END in tf.lines[j]:
                    end_idx = j
                    break
            if has_begin:
                return False
            if end_idx != -1:
                nl = tf.newline
                block = [l + nl for l in HL_DEFINITION_LINES]
                tf.lines[i:end_idx + 1] = block
                return True
            return False
        code, _ = split_comment(strip_eol(line)[0])
        if r"\providecommand{\sioyekhlbegin}" in code:
            return False
    begin, _ = find_document_bounds(tf.lines)
    if begin is None:
        return False
    nl = tf.newline
    if begin > 0 and not tf.lines[begin - 1].endswith(("\n", "\r")):
        tf.lines[begin - 1] += nl
    block = [l + nl for l in HL_DEFINITION_LINES]
    tf.lines[begin:begin] = block
    return True


def parse_rect_or_pos(s):
    s = clean_path(s).strip()
    parts = [clean_path(p).strip() for p in re.split(r'[, ]+', s) if p.strip()]
    page = int(parts[0])
    vals = [float(p) for p in parts[1:]]
    if len(vals) >= 4:
        return page, [min(vals[0], vals[2]), min(vals[1], vals[3]), max(vals[0], vals[2]), max(vals[1], vals[3])]
    elif len(vals) == 2:
        return page, [vals[0], vals[1], vals[0], vals[1]]
    elif len(vals) == 1:
        return page, [100.0, vals[0], 100.0, vals[0]]
    return page, [0.0, 0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Highlight Logic
# ---------------------------------------------------------------------------

def is_line_in_math_or_unsafe(state):
    depth, math, stack, in_verb = state
    if in_verb or math is not None:
        return True
    for env in stack:
        env_b = env.rstrip("*")
        if env_b in ("equation", "align", "gather", "multline", "figure", "table", "tabular", "tikzpicture"):
            return True
    return False


def try_mark_in_single_line(line_str, text_to_wrap, type_char):
    r"""
    Tries to find text_to_wrap in line_str (code part only) and wrap with marks:
    \sioyekhlbegin[type]text_to_wrap\sioyekhlend[type].
    Returns (new_line_str, True) on success, or (line_str, False).
    """
    body, eol = strip_eol(line_str)
    code, comment = split_comment(body)
    if not code.strip():
        return line_str, False

    b_tag = f"\\sioyekhlbegin[{type_char}]"
    e_tag = f"\\sioyekhlend[{type_char}]"

    # Already marked?
    if b_tag in code and e_tag in code:
        return line_str, True

    # 1. Exact match in code
    idx = code.find(text_to_wrap)
    if idx >= 0:
        before = code[:idx]
        if before.endswith("\\"):
            return line_str, False
        marked = f"{b_tag}{text_to_wrap}{e_tag}"
        new_code = code[:idx] + marked + code[idx + len(text_to_wrap):]
        return new_code + comment + eol, True

    # 2. Whitespace-flexible regex match
    words = [w for w in text_to_wrap.split() if w]
    if len(words) > 1:
        pattern = r"\s+".join(re.escape(w) for w in words)
        m = re.search(pattern, code)
        if m:
            matched_slice = m.group(0)
            marked = f"{b_tag}{matched_slice}{e_tag}"
            new_code = code[:m.start()] + marked + code[m.end():]
            return new_code + comment + eol, True

    return line_str, False


def try_mark_start_in_line(line_str, text_start, type_char):
    """Inserts \\sioyekhlbegin[type] immediately before text_start in line_str."""
    body, eol = strip_eol(line_str)
    code, comment = split_comment(body)
    b_tag = f"\\sioyekhlbegin[{type_char}]"
    if b_tag in code:
        return line_str, True

    idx = code.find(text_start)
    if idx >= 0:
        if code[:idx].endswith("\\"):
            return line_str, False
        new_code = code[:idx] + b_tag + code[idx:]
        return new_code + comment + eol, True

    words = [w for w in text_start.split() if w]
    if words:
        pattern = r"\s+".join(re.escape(w) for w in words)
        m = re.search(pattern, code)
        if m:
            new_code = code[:m.start()] + b_tag + code[m.start():]
            return new_code + comment + eol, True

    return line_str, False


def try_mark_end_in_line(line_str, text_end, type_char):
    """Inserts \\sioyekhlend[type] immediately after text_end in line_str."""
    body, eol = strip_eol(line_str)
    code, comment = split_comment(body)
    e_tag = f"\\sioyekhlend[{type_char}]"
    if e_tag in code:
        return line_str, True

    idx = code.rfind(text_end)
    if idx >= 0:
        end_pos = idx + len(text_end)
        new_code = code[:end_pos] + e_tag + code[end_pos:]
        return new_code + comment + eol, True

    words = [w for w in text_end.split() if w]
    if words:
        pattern = r"\s+".join(re.escape(w) for w in words)
        matches = list(re.finditer(pattern, code))
        if matches:
            m = matches[-1]
            new_code = code[:m.end()] + e_tag + code[m.end():]
            return new_code + comment + eol, True

    return line_str, False


def embed_highlight(pdf_path, rect_str, type_char, selected_text):
    if not has_synctex_data(pdf_path):
        log(f"embed_highlight: no synctex data for {pdf_path}, skipping")
        return None

    page, rect = parse_rect_or_pos(rect_str)
    text = selected_text.strip()
    if not text:
        return None

    tex_path, synctex_line = resolve_source(pdf_path, page, rect)
    tf = TexFile(tex_path)
    current_line = map_stale_line(tex_path, synctex_line, pdf_path, tf.lines)
    target_idx = max(0, min(len(tf.lines) - 1, current_line - 1))

    # Search window around target_idx: [target_idx - 3, target_idx + 4]
    lo = max(0, target_idx - 3)
    hi = min(len(tf.lines), target_idx + 5)
    matched_idx = -1
    marked_success = False

    # 1. First check if single line matches
    search_indices = [target_idx] + [i for i in range(lo, hi) if i != target_idx]
    for idx in search_indices:
        new_line, ok = try_mark_in_single_line(tf.lines[idx], text, type_char)
        if ok:
            tf.lines[idx] = new_line
            matched_idx = idx
            marked_success = True
            break

    # 2. Multi-line spanning across 2 consecutive lines
    if not marked_success and len(text.split()) >= 2:
        words = text.split()
        for idx in range(lo, min(hi, len(tf.lines) - 1)):
            for split_pt in range(1, len(words)):
                part1 = " ".join(words[:split_pt])
                part2 = " ".join(words[split_pt:])
                nl1, ok1 = try_mark_start_in_line(tf.lines[idx], part1, type_char)
                nl2, ok2 = try_mark_end_in_line(tf.lines[idx + 1], part2, type_char)
                if ok1 and ok2:
                    tf.lines[idx] = nl1
                    tf.lines[idx + 1] = nl2
                    matched_idx = idx
                    marked_success = True
                    break
            if marked_success:
                break

    # 3. Multi-line spanning across 3 or more lines (start words on line A, end words on line B)
    if not marked_success and len(text.split()) >= 4:
        words = text.split()
        start_probe = " ".join(words[:min(4, len(words))])
        end_probe = " ".join(words[-min(4, len(words)):])
        for idx1 in range(lo, min(hi, len(tf.lines))):
            nl1, ok1 = try_mark_start_in_line(tf.lines[idx1], start_probe, type_char)
            if ok1:
                for idx2 in range(idx1 + 1, hi):
                    nl2, ok2 = try_mark_end_in_line(tf.lines[idx2], end_probe, type_char)
                    if ok2:
                        tf.lines[idx1] = nl1
                        tf.lines[idx2] = nl2
                        matched_idx = idx1
                        marked_success = True
                        break
                if marked_success:
                    break

    # 4. Fallback if text couldn't be precisely located in code
    if not marked_success:
        safe_idx, _ = find_insert_line(tf.lines, target_idx)
        body, eol = strip_eol(tf.lines[safe_idx])
        code, comment = split_comment(body)
        mark = f" \\sioyekhlbegin[{type_char}]\\sioyekhlend[{type_char}]"
        tf.lines[safe_idx] = code.rstrip(" \t") + mark + (" " + comment if comment else "") + eol
        matched_idx = safe_idx
        action_verb = "已嵌入高亮标记"
    else:
        action_verb = "已标记高亮源码"

    files_to_commit = [tf]
    root_path = find_root_file(tex_path, pdf_path)
    if root_path:
        if os.path.normpath(root_path) == os.path.normpath(tex_path):
            inject_hl_definition(tf)
        else:
            root_tf = TexFile(root_path)
            if inject_hl_definition(root_tf):
                files_to_commit.append(root_tf)

    desc = f"高亮标记 [{type_char}] @ {os.path.basename(tex_path)}:{matched_idx + 1}: {text[:30]}"
    commit_annot_files(files_to_commit, pdf_path, "highlight", desc)
    return f"{action_verb} [{type_char}]：{os.path.basename(tex_path)}:{matched_idx + 1}（等待编译）"


# ---------------------------------------------------------------------------
# Deletion & Undo
# ---------------------------------------------------------------------------

def delete_highlight(pdf_path, rect_str, type_char, selected_text):
    if not has_synctex_data(pdf_path):
        return None

    page, rect = parse_rect_or_pos(rect_str)
    tex_path, synctex_line = resolve_source(pdf_path, page, rect)
    tf = TexFile(tex_path)
    current_line = map_stale_line(tex_path, synctex_line, pdf_path, tf.lines)
    target_idx = max(0, min(len(tf.lines) - 1, current_line - 1))

    lo = max(0, target_idx - 5)
    hi = min(len(tf.lines), target_idx + 6)
    modified = False

    # 1. Look for \sioyekhlbegin and \sioyekhlend on a single line
    for idx in range(lo, hi):
        line = tf.lines[idx]
        if r"\sioyekhlbegin" in line and r"\sioyekhlend" in line:
            new_line = re.sub(r"\\sioyekhlbegin(?:\[[^\]]*\])?", "", line)
            new_line = re.sub(r"\\sioyekhlend(?:\[[^\]]*\])?", "", new_line)
            if new_line != line:
                tf.lines[idx] = new_line
                modified = True
                break

    # If not on same line, look for multi-line begin/end pair
    if not modified:
        begin_idx = -1
        end_idx = -1
        for idx in range(lo, hi):
            if r"\sioyekhlbegin" in tf.lines[idx] and begin_idx == -1:
                begin_idx = idx
            if r"\sioyekhlend" in tf.lines[idx] and end_idx == -1:
                end_idx = idx
        if begin_idx != -1 and end_idx != -1 and begin_idx <= end_idx:
            tf.lines[begin_idx] = re.sub(r"\\sioyekhlbegin(?:\[[^\]]*\])?", "", tf.lines[begin_idx])
            tf.lines[end_idx] = re.sub(r"\\sioyekhlend(?:\[[^\]]*\])?", "", tf.lines[end_idx])
            modified = True

    # 2. Look for legacy \sioyekhl[type]{...} or \sioyekhlmark[type]{...}
    if not modified:
        for idx in range(lo, hi):
            line = tf.lines[idx]
            m = re.search(r"\\sioyekhl(?:\[[^\]]*\])?\{([^{}]*)\}", line)
            if m:
                content = m.group(1)
                tf.lines[idx] = line[:m.start()] + content + line[m.end():]
                modified = True
                break
            m2 = re.search(r"\\sioyekhlmark(?:\[[^\]]*\])?\{[^{}]*\}", line)
            if m2:
                tf.lines[idx] = line[:m2.start()].rstrip(" \t") + line[m2.end():]
                modified = True
                break

    if modified:
        desc = f"删除高亮标记 @ {os.path.basename(tex_path)}:{target_idx + 1}"
        commit_annot_files([tf], pdf_path, "highlight", desc)
        return f"已从 {os.path.basename(tex_path)}:{target_idx + 1} 移除高亮标记（等待编译）"
    return None


def undo_annotation(pdf_path, target_kind=None):
    hist = load_annot_history()
    idx = None
    for i in range(len(hist) - 1, -1, -1):
        item = hist[i]
        if os.path.normpath(item.get("pdf", "")) == os.path.normpath(pdf_path):
            if target_kind is None or item.get("kind") == target_kind:
                idx = i
                break

    if idx is None:
        target_name = "高亮" if target_kind == "highlight" else "标注"
        return f"没有可撤销的 {target_name} 写入"

    entry = hist[idx]
    for fi in entry["files"]:
        if not os.path.exists(fi["path"]):
            return f"文件不存在: {fi['path']}"
        with open(fi["path"], "rb") as f:
            cur = f.read()
        if sha1(cur) != fi["after_sha1"]:
            return f"{os.path.basename(fi['path'])} 已被手动修改，无法安全撤销；备份在 {fi['backup']}"
        if not os.path.exists(fi["backup"]):
            return f"备份丢失: {fi['backup']}"

    for fi in entry["files"]:
        with open(fi["backup"], "rb") as f:
            data = f.read()
        atomic_write(fi["path"], data)

    del hist[idx]
    save_annot_history(hist)
    names = ", ".join(os.path.basename(f["path"]) for f in entry["files"])
    return f"已撤销：{entry['desc']}（已恢复 {names}，等待编译）"


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 6:
        print("Usage: python -m sioyek.embed_tex_annotation <action> SIOYEK LOCAL_DB SHARED_DB PDF [ARGS...]")
        return

    action = sys.argv[1].lower()
    sioyek_path = clean_path(sys.argv[2])
    local_db = clean_path(sys.argv[3])
    shared_db = clean_path(sys.argv[4])
    pdf_path = os.path.abspath(clean_path(sys.argv[5]))

    try:
        sioyek = Sioyek(sioyek_path, local_db, shared_db)
    except Exception:
        sioyek = None

    msg = None
    try:
        if action == "highlight" and len(sys.argv) >= 9:
            rect_str = clean_path(sys.argv[6])
            type_char = clean_path(sys.argv[7]) or "a"
            selected_text = sys.argv[8]
            msg = embed_highlight(pdf_path, rect_str, type_char, selected_text)

        elif action == "delete_highlight" and len(sys.argv) >= 9:
            rect_str = clean_path(sys.argv[6])
            type_char = clean_path(sys.argv[7]) or "a"
            selected_text = sys.argv[8]
            msg = delete_highlight(pdf_path, rect_str, type_char, selected_text)

        elif action == "undo":
            target_kind = clean_path(sys.argv[6]) if len(sys.argv) > 6 else None
            msg = undo_annotation(pdf_path, target_kind)

        else:
            log(f"Unknown action or insufficient args: {sys.argv}")

    except Exception as e:
        log(f"Error in {action}: {e}\n{traceback.format_exc()}")
        msg = f"TeX 批注出错: {e}"

    if msg:
        print(msg)
        notify_sioyek(sioyek, msg)


if __name__ == "__main__":
    main()
