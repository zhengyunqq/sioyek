r"""
Automatically embed highlights directly into LaTeX source files
for SyncTeX-enabled PDF documents.

Highlights are rendered with:
1. Micro-triangles at the start and end of highlighted text:
   \sioyekhlbegin[type] ... \sioyekhlend[type] -> ▸ ... ◂
2. Margin notes in the blank page margin:
   \sioyekhlmark[type]{snippet} -> [Highlight: snippet]

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
    r"\providecommand{\sioyekhlmark}[2][a]{%",
    r"  \ifsioyekhl\marginpar{\raggedright\footnotesize\color{\sioyek@gethlcolor{#1}}\textbf{[Highlight:} #2\textbf{]}}\fi%",
    r"}",
    r"\providecommand{\sioyekhl}[2][a]{%",
    r"  \sioyekhlmark[#1]{#2}%",
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
    for i, line in enumerate(tf.lines):
        if MARKER_HL_BEGIN in line:
            end_idx = -1
            has_all_macros = False
            for j in range(i, len(tf.lines)):
                if (r"\sioyekhlbeginmark" in tf.lines[j] and
                    r"\sioyekhlmark" in tf.lines[j] and
                    r"\marginpar" in tf.lines[j]):
                    has_all_macros = True
                if MARKER_HL_END in tf.lines[j]:
                    end_idx = j
                    break
            if has_all_macros:
                return False
            if end_idx != -1:
                nl = tf.newline
                block = [l + nl for l in HL_DEFINITION_LINES]
                tf.lines[i:end_idx + 1] = block
                return True
            return False
        code, _ = split_comment(strip_eol(line)[0])
        if r"\providecommand{\sioyekhlbegin}" in code and r"\providecommand{\sioyekhlmark}" in code:
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
# Snippet Formatting & Sliding-Window Candidate Probes
# ---------------------------------------------------------------------------

def format_hl_snippet(selected_text, max_len=45):
    r"""
    Formats selected text into a concise snippet for the margin note:
    - If <= max_len characters: returns sanitized text.
    - If > max_len characters: extracts start words, \dots{}, end words.
    """
    text = " ".join(selected_text.split()).strip()
    if not text:
        return ""

    def sanitize(s):
        s = s.replace("{", "").replace("}", "").replace("\\", "")
        s = s.replace("%", r"\%").replace("&", r"\&").replace("#", r"\#").replace("_", r"\_").replace("$", r"\$")
        return s.strip()

    has_cjk = bool(re.search(r'[\u4e00-\u9fa5]', text))

    if has_cjk:
        if len(text) <= 25:
            return sanitize(text)
        head = text[:12]
        tail = text[-12:]
        return f"{sanitize(head)} \\dots{{}} {sanitize(tail)}"

    if len(text) <= max_len:
        return sanitize(text)

    words = text.split()
    if len(words) >= 6:
        head = " ".join(words[:3])
        tail = " ".join(words[-3:])
        if len(head) > 22:
            head = head[:20] + ".."
        if len(tail) > 22:
            tail = ".." + tail[-20:]
        return f"{sanitize(head)} \\dots{{}} {sanitize(tail)}"
    else:
        head = text[:20]
        tail = text[-20:]
        return f"{sanitize(head)} \\dots{{}} {sanitize(tail)}"


def normalize_probe_regex(word):
    r"""Builds a regex snippet that matches word with optional quotes/apostrophes."""
    w_escaped = re.escape(word)
    return r'(?:``|"|“|‘|`)?' + w_escaped + r"(?:''|\"|”|’|')?"


def get_start_probe_candidates(selected_text):
    r"""
    Generates candidate start probe word sequences using a sliding window.
    Resilient against spurious leading words (e.g. from section headers or run-in titles).
    """
    text = selected_text.strip()
    has_cjk = bool(re.search(r'[\u4e00-\u9fa5]', text))
    if has_cjk:
        cjk_chars = re.findall(r'[\u4e00-\u9fa5]', text)
        candidates = []
        n = len(cjk_chars)
        for i in range(min(5, max(1, n - 3))):
            candidates.append(''.join(cjk_chars[i:i+4]))
        for i in range(min(5, max(1, n - 2))):
            candidates.append(''.join(cjk_chars[i:i+3]))
        return candidates, True

    words = text.split()
    clean_words = []
    for w in words:
        cw = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if len(cw) >= 2 or cw in ('a', 'A', 'I'):
            clean_words.append(cw)
        if len(clean_words) >= 10:
            break

    candidates = []
    # Try 3-word sliding windows (offset 0, 1, 2, 3...)
    for i in range(min(6, max(1, len(clean_words) - 2))):
        candidates.append(clean_words[i:i+3])
    # Try 2-word sliding windows
    for i in range(min(6, max(1, len(clean_words) - 1))):
        candidates.append(clean_words[i:i+2])
    return candidates, False


def get_end_probe_candidates(selected_text):
    r"""
    Generates candidate end probe word sequences using a sliding window from the end.
    """
    text = selected_text.strip()
    has_cjk = bool(re.search(r'[\u4e00-\u9fa5]', text))
    if has_cjk:
        cjk_chars = re.findall(r'[\u4e00-\u9fa5]', text)
        candidates = []
        n = len(cjk_chars)
        for i in range(n, max(0, n - 5), -1):
            if i >= 4:
                candidates.append(''.join(cjk_chars[i-4:i]))
        for i in range(n, max(0, n - 5), -1):
            if i >= 3:
                candidates.append(''.join(cjk_chars[i-3:i]))
        return candidates, True

    words = text.split()
    clean_words = []
    for w in reversed(words):
        cw = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if len(cw) >= 2 or cw in ('a', 'A', 'I'):
            clean_words.insert(0, cw)
        if len(clean_words) >= 10:
            break

    candidates = []
    n = len(clean_words)
    # 3-word windows from the end
    for i in range(n, max(0, n - 6), -1):
        if i >= 3:
            candidates.append(clean_words[i-3:i])
    # 2-word windows from the end
    for i in range(n, max(0, n - 6), -1):
        if i >= 2:
            candidates.append(clean_words[i-2:i])
    return candidates, False


def find_start_in_line(code, start_probe, is_cjk):
    r"""Returns the start character index of start_probe in code, or -1."""
    if is_cjk:
        m = re.search(re.escape(start_probe), code)
        return m.start() if m else -1
    parts = [normalize_probe_regex(w) for w in start_probe]
    pat = r'\s+'.join(parts)
    m = re.search(pat, code)
    if m:
        if code[:m.start()].endswith('\\'):
            return -1
        return m.start()
    return -1


def find_end_in_line(code, end_probe, is_cjk, selected_text, start_offset=0):
    r"""Returns the end character index of end_probe in code, or -1."""
    sub_code = code[start_offset:]
    abs_end = -1
    if is_cjk:
        matches = list(re.finditer(re.escape(end_probe), sub_code))
        if matches:
            m_end = matches[-1]
            abs_end = start_offset + m_end.end()
    else:
        parts = [normalize_probe_regex(w) for w in end_probe]
        pat = r'\s+'.join(parts)
        matches = list(re.finditer(pat, sub_code))
        if matches:
            m_end = matches[-1]
            abs_end = start_offset + m_end.end()
            last_word = end_probe[-1]
            idx_in_pdf = selected_text.rfind(last_word)
            pdf_tail = selected_text[idx_in_pdf + len(last_word):].strip() if idx_in_pdf >= 0 else ''
            tail_code = code[abs_end:]
            if pdf_tail:
                last_char = pdf_tail[-1]
                if last_char in '.!?,;:)]}"\'':
                    p_idx = tail_code.find(last_char)
                    if p_idx >= 0:
                        abs_end += p_idx + 1
                elif '$' in tail_code:
                    first_dollar = tail_code.find('$')
                    if first_dollar >= 0:
                        second_dollar = tail_code.find('$', first_dollar + 1)
                        if second_dollar >= 0:
                            abs_end += second_dollar + 1
    return abs_end


def find_all_sioyekhlmarks_in_line(line):
    r"""
    Finds all \sioyekhlmark[...]{...} and \sioyekhl[...]{...} calls in line,
    correctly handling nested braces (such as \dots{}).
    Returns list of (start_idx, end_idx_exclusive, opt_arg, body_arg).
    """
    pattern = re.compile(r"\\sioyekhl(?:mark)?(?:\s*\[([^\]]*)\])?\s*\{")
    results = []
    pos = 0
    while pos < len(line):
        m = pattern.search(line, pos)
        if not m:
            break
        open_brace_idx = m.end() - 1
        depth = 1
        i = open_brace_idx + 1
        matched = False
        while i < len(line):
            c = line[i]
            if c == "{" and (i == 0 or line[i - 1] != "\\"):
                depth += 1
            elif c == "}" and (i == 0 or line[i - 1] != "\\"):
                depth -= 1
                if depth == 0:
                    start_idx = m.start()
                    opt_arg = m.group(1) or "a"
                    body_arg = line[open_brace_idx + 1:i]
                    results.append((start_idx, i + 1, opt_arg, body_arg))
                    pos = i + 1
                    matched = True
                    break
            i += 1
        if not matched:
            break
    return results


# ---------------------------------------------------------------------------
# Core Highlight Embedding & Deletion
# ---------------------------------------------------------------------------

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

    # Search window around target_idx: [target_idx - 6, target_idx + 8]
    lo = max(0, target_idx - 6)
    hi = min(len(tf.lines), target_idx + 8)
    search_indices = [target_idx] + [i for i in range(lo, hi) if i != target_idx]

    b_tag = f"\\sioyekhlbegin[{type_char}]"
    e_tag = f"\\sioyekhlend[{type_char}]"
    snippet = format_hl_snippet(text)
    m_tag = f"\\sioyekhlmark[{type_char}]{{{snippet}}}"

    marked_success = False
    matched_idx = target_idx

    start_candidates, is_cjk = get_start_probe_candidates(text)
    end_candidates, _ = get_end_probe_candidates(text)

    # 1. Search for start probe and end probe
    # 1a. Same line match
    for idx in search_indices:
        body, eol = strip_eol(tf.lines[idx])
        code, comment = split_comment(body)
        for sp in start_candidates:
            s_idx = find_start_in_line(code, sp, is_cjk)
            if s_idx >= 0:
                for ep in end_candidates:
                    e_idx = find_end_in_line(code, ep, is_cjk, text, start_offset=s_idx)
                    if e_idx > s_idx:
                        # Found start and end on the same line!
                        # Check if e_idx is inside math mode ($...$)
                        code_before_e = code[:e_idx]
                        in_inline_math = (code_before_e.count('$') - code_before_e.count(r'\$')) % 2 != 0
                        if in_inline_math:
                            closing_dollar = code.find('$', e_idx)
                            m_pos = closing_dollar + 1 if closing_dollar >= 0 else len(code)
                        else:
                            m_pos = e_idx

                        if m_pos == e_idx:
                            new_code = code[:s_idx] + b_tag + code[s_idx:e_idx] + e_tag + " " + m_tag + code[e_idx:]
                        else:
                            new_code = (code[:s_idx] + b_tag + code[s_idx:e_idx] + e_tag +
                                        code[e_idx:m_pos] + " " + m_tag + code[m_pos:])

                        sep_com = " " if comment and not new_code.endswith(" ") else ""
                        tf.lines[idx] = new_code + sep_com + comment + eol
                        matched_idx = idx
                        marked_success = True
                        break
                if marked_success:
                    break
        if marked_success:
            break

    # 1b. Multi-line match: start on line found_a, end on line found_b (found_a < found_b)
    if not marked_success:
        for sp in start_candidates:
            found_a = -1
            s_pos = -1
            for idx in range(lo, hi):
                body, _ = strip_eol(tf.lines[idx])
                code, _ = split_comment(body)
                s = find_start_in_line(code, sp, is_cjk)
                if s >= 0:
                    found_a = idx
                    s_pos = s
                    break

            if found_a >= 0:
                for ep in end_candidates:
                    found_b = -1
                    e_pos = -1
                    for idx in range(found_a + 1, hi):
                        body, _ = strip_eol(tf.lines[idx])
                        code, _ = split_comment(body)
                        e = find_end_in_line(code, ep, is_cjk, text, start_offset=0)
                        if e >= 0:
                            found_b = idx
                            e_pos = e
                            break

                    if found_b >= 0:
                        # Insert b_tag on line A
                        body_a, eol_a = strip_eol(tf.lines[found_a])
                        code_a, com_a = split_comment(body_a)
                        new_code_a = code_a[:s_pos] + b_tag + code_a[s_pos:]
                        sep_com_a = " " if com_a and not new_code_a.endswith(" ") else ""
                        tf.lines[found_a] = new_code_a + sep_com_a + com_a + eol_a

                        # Insert e_tag on line B + margin note
                        body_b, eol_b = strip_eol(tf.lines[found_b])
                        code_b, com_b = split_comment(body_b)
                        code_before_e = code_b[:e_pos]
                        in_inline_math = (code_before_e.count('$') - code_before_e.count(r'\$')) % 2 != 0
                        if in_inline_math:
                            closing_dollar = code_b.find('$', e_pos)
                            m_pos = closing_dollar + 1 if closing_dollar >= 0 else len(code_b)
                        else:
                            m_pos = e_pos

                        if m_pos == e_pos:
                            new_code_b = code_b[:e_pos] + e_tag + " " + m_tag + code_b[e_pos:]
                        else:
                            new_code_b = (code_b[:e_pos] + e_tag + code_b[e_pos:m_pos] +
                                          " " + m_tag + code_b[m_pos:])
                        sep_com_b = " " if com_b and not new_code_b.endswith(" ") else ""
                        tf.lines[found_b] = new_code_b + sep_com_b + com_b + eol_b

                        matched_idx = found_a
                        marked_success = True
                        break
            if marked_success:
                break

    # 2. Fallback: only if probe matching completely failed, insert ONLY m_tag on safe line!
    # DO NOT dump dummy \sioyekhlbegin\sioyekhlend (▸◂) together at the end of the line!
    if not marked_success:
        safe_idx, _ = find_insert_line(tf.lines, target_idx)
        body, eol = strip_eol(tf.lines[safe_idx])
        code, comment = split_comment(body)
        sep = "" if not code or code.endswith(" ") else " "
        new_code = code + sep + m_tag
        sep_com = " " if comment and not new_code.endswith(" ") else ""
        tf.lines[safe_idx] = new_code + sep_com + comment + eol
        matched_idx = safe_idx

    files_to_commit = [tf]
    root_path = find_root_file(tex_path, pdf_path)
    if root_path:
        if os.path.normpath(root_path) == os.path.normpath(tex_path):
            inject_hl_definition(tf)
        else:
            root_tf = TexFile(root_path)
            if inject_hl_definition(root_tf):
                files_to_commit.append(root_tf)

    desc = f"高亮标记 [{type_char}] @ {os.path.basename(tex_path)}:{matched_idx + 1}: {snippet[:30]}"
    commit_annot_files(files_to_commit, pdf_path, "highlight", desc)
    return f"已标记高亮与页边注 [{type_char}]：{os.path.basename(tex_path)}:{matched_idx + 1}（等待编译）"


def delete_highlight(pdf_path, rect_str, type_char, selected_text):
    if not has_synctex_data(pdf_path):
        return None

    page, rect = parse_rect_or_pos(rect_str)
    tex_path, synctex_line = resolve_source(pdf_path, page, rect)
    tf = TexFile(tex_path)
    current_line = map_stale_line(tex_path, synctex_line, pdf_path, tf.lines)
    target_idx = max(0, min(len(tf.lines) - 1, current_line - 1))

    # Search window around target_idx
    lo = max(0, target_idx - 8)
    hi = min(len(tf.lines), target_idx + 10)
    search_indices = [target_idx] + [i for i in range(lo, hi) if i != target_idx]

    snippet = format_hl_snippet(selected_text) if selected_text else ""
    probe_words = [w for w in re.findall(r"\w+", selected_text.lower()) if len(w) >= 3] if selected_text else []

    matched_idx = -1
    modified = False

    # 1. Remove \sioyekhlmark matching snippet/type
    for idx in search_indices:
        line = tf.lines[idx]
        marks = find_all_sioyekhlmarks_in_line(line)
        for s, e, opt, body in marks:
            body_lower = body.lower()
            text_match = (snippet and snippet in body) or (probe_words and any(pw in body_lower for pw in probe_words))
            type_match = (not type_char or opt == type_char)
            if text_match or type_match:
                while s > 0 and line[s - 1] in (" ", "\t"):
                    s -= 1
                tf.lines[idx] = line[:s] + line[e:]
                matched_idx = idx
                modified = True
                break
        if modified:
            break

    # 2. Remove \sioyekhlbegin and \sioyekhlend tags
    for idx in search_indices:
        line = tf.lines[idx]
        if r"\sioyekhlbegin" in line or r"\sioyekhlend" in line:
            new_line = re.sub(r"\\sioyekhlbegin(?:\[[^\]]*\])?", "", line)
            new_line = re.sub(r"\\sioyekhlend(?:\[[^\]]*\])?", "", new_line)
            if new_line != line:
                tf.lines[idx] = new_line
                matched_idx = idx
                modified = True

    if modified:
        desc = f"删除高亮标记 @ {os.path.basename(tex_path)}:{matched_idx + 1}"
        commit_annot_files([tf], pdf_path, "highlight", desc)
        return f"已从 {os.path.basename(tex_path)}:{matched_idx + 1} 移除高亮与页边标记（等待编译）"
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
