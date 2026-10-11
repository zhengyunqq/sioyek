"""
Add / edit / delete a study note directly in the LaTeX source of a SyncTeX-enabled PDF.

Usage (from sioyek prefs_user.config):
    python -m sioyek.add_study_note SIOYEK LOCAL_DB SHARED_DB PDF RECT [TEXT]
    python -m sioyek.add_study_note SIOYEK LOCAL_DB SHARED_DB PDF --undo

Design:
  * The selected rect is mapped to (tex file, line) with the `synctex edit` CLI.
  * The note is written as `\\studynote{...}` at the end of that source line (before a
    trailing `%` comment). If the line ends inside math / floats / tabular / an open brace
    group etc., the note is moved to the first line after that construct is closed.
  * On first use, a `\\providecommand{\\studynote}` definition (margin note built on
    \\marginpar + color) with a global `\\ifstudynotes` switch is injected right before
    `\\begin{document}` in the root file.
  * Files are backed up to `.sioyek_bak/` (latest 20 kept), written atomically with the
    original encoding and line endings. `--undo` restores the most recent write.
  * Nothing is compiled; the user's own build workflow picks up the change.
"""
import sys
import os
import re
import json
import glob
import shutil
import hashlib
import datetime
import tempfile
import difflib
import subprocess
import traceback

from .sioyek import Sioyek, clean_path

LOG_PATH = os.path.expanduser("~/.config/sioyek/add_text.log")
HISTORY_PATH = os.path.expanduser("~/.config/sioyek/study_note_history.json")
BACKUP_DIR_NAME = ".sioyek_bak"
BACKUP_KEEP = 20
HISTORY_KEEP = 50

MARKER_BEGIN = "%% >>> sioyek studynote >>>"
MARKER_END = "%% <<< sioyek studynote <<<"
DEFINITION_LINES = [
    MARKER_BEGIN,
    r"\makeatletter",
    r"\@ifpackageloaded{xcolor}{}{\@ifpackageloaded{color}{}{\usepackage{xcolor}}}",
    r"\makeatother",
    r"\newif\ifstudynotes \studynotestrue",
    r"% \studynotesfalse   % uncomment to hide ALL study notes",
    r"\providecommand{\studynote}[1]{\ifstudynotes\marginpar{\raggedright\footnotesize\color{blue}\textbf{Note:} #1}\fi}",
    MARKER_END,
]

# Environments in which \marginpar is illegal / lost (compared without trailing '*').
UNSAFE_ENVS = {
    "equation", "align", "alignat", "flalign", "xalignat", "gather", "multline", "eqnarray",
    "math", "displaymath", "split", "aligned", "alignedat", "gathered", "cases", "dcases",
    "array", "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "Bmatrix", "smallmatrix",
    "figure", "table", "wrapfigure", "wraptable", "sidewaysfigure", "sidewaystable",
    "subfigure", "subtable", "tabular", "tabularx", "tabulary", "longtable", "tabu",
    "tblr", "longtblr", "tikzpicture", "pgfpicture", "picture", "minipage", "tcolorbox",
    "algorithm", "algorithmic", "algorithm2e", "multicols", "center*",
}
VERBATIM_ENVS = {"verbatim", "Verbatim", "lstlisting", "minted", "comment", "alltt", "BVerbatim", "LVerbatim"}

STUDYNOTE_RE = re.compile(r"\\studynote\s*\{")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[study_note] {msg}\n")
    except Exception:
        pass


class StudyNoteError(Exception):
    pass


# ---------------------------------------------------------------------------
# Rect / SyncTeX
# ---------------------------------------------------------------------------

def parse_rect(s):
    s = clean_path(s).strip()
    parts = [clean_path(p).strip() for p in s.split(',')]
    page = int(parts[0])
    vals = [float(p) for p in parts[1:]]
    if len(vals) >= 4:
        x0, y0, x1, y1 = vals[:4]
        return page, [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
    if len(vals) >= 2:
        return page, [vals[0], vals[1], vals[0], vals[1]]
    raise StudyNoteError(f"无法解析选区: {s}")


def find_synctex_binary():
    for cand in ("/Library/TeX/texbin/synctex", "/usr/local/texlive/bin/synctex"):
        if os.path.exists(cand):
            return cand
    found = shutil.which("synctex")
    if found:
        return found
    for cand in glob.glob("/usr/local/texlive/*/bin/*/synctex"):
        return cand
    raise StudyNoteError("找不到 synctex 命令（请安装 TeX Live / MacTeX）")


def has_synctex_data(pdf_path):
    base = os.path.splitext(pdf_path)[0]
    return os.path.exists(base + ".synctex.gz") or os.path.exists(base + ".synctex")


def synctex_query(synctex_bin, pdf_path, page_1based, x, y):
    """Returns list of (abs_input_path, line) for one point."""
    try:
        out = subprocess.run(
            [synctex_bin, "edit", "-o", f"{page_1based}:{x:.2f}:{y:.2f}:{pdf_path}"],
            capture_output=True, text=True, timeout=10,
            cwd=os.path.dirname(pdf_path) or None,
        ).stdout
    except Exception as e:
        log(f"synctex failed: {e}")
        return []
    results = []
    cur_input = None
    for raw in out.splitlines():
        if raw.startswith("Input:"):
            cur_input = raw[len("Input:"):].strip()
        elif raw.startswith("Line:") and cur_input:
            try:
                line = int(raw[len("Line:"):].strip())
            except ValueError:
                continue
            path = cur_input
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(pdf_path), path)
            path = os.path.normpath(path)
            if line > 0:
                results.append((path, line))
    return results


def resolve_source(pdf_path, page0, rect):
    """Maps the selection rect to the best (tex_path, line_1based)."""
    if not has_synctex_data(pdf_path):
        raise StudyNoteError("该 PDF 没有 SyncTeX 数据（请用 -synctex=1 编译）")
    synctex_bin = find_synctex_binary()
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    points = [(cx, cy)]
    if x1 - x0 > 2 or y1 - y0 > 2:
        points += [(x0 + 1, y0 + 1), (x1 - 1, y1 - 1), (x0 + 1, cy), (x1 - 1, cy)]
    hits = []  # (path, line, is_center)
    for idx, (px, py) in enumerate(points):
        for path, line in synctex_query(synctex_bin, pdf_path, page0 + 1, px, py):
            if path.lower().endswith((".tex", ".ltx")) and os.path.exists(path):
                hits.append((path, line, idx == 0))
    if not hits:
        raise StudyNoteError("SyncTeX 未能定位到 .tex 源文件")
    counts = {}
    for path, _, _ in hits:
        counts[path] = counts.get(path, 0) + 1
    best_file = max(counts, key=lambda p: counts[p])
    center_lines = [l for p, l, c in hits if p == best_file and c]
    if center_lines:
        line = center_lines[0]
    else:
        lines = sorted(l for p, l, _ in hits if p == best_file)
        line = lines[len(lines) // 2]
    return best_file, line


# ---------------------------------------------------------------------------
# File IO (encoding / line endings preserving, atomic)
# ---------------------------------------------------------------------------

class TexFile:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read()
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        body = raw[3:] if self.bom else raw
        try:
            text = body.decode("utf-8")
            self.encoding = "utf-8"
        except UnicodeDecodeError:
            text = body.decode("latin-1")
            self.encoding = "latin-1"
        self.newline = "\r\n" if "\r\n" in text else ("\r" if ("\r" in text and "\n" not in text) else "\n")
        self.lines = text.splitlines(keepends=True)
        self.original_raw = raw

    def to_bytes(self):
        text = "".join(self.lines)
        try:
            data = text.encode(self.encoding)
        except UnicodeEncodeError:
            raise StudyNoteError(f"{os.path.basename(self.path)} 是 {self.encoding} 编码，无法写入这些字符")
        return (b"\xef\xbb\xbf" + data) if self.bom else data


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def make_backup(path, raw):
    bdir = os.path.join(os.path.dirname(path), BACKUP_DIR_NAME)
    os.makedirs(bdir, exist_ok=True)
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    bpath = os.path.join(bdir, f"{stem}.{stamp}{ext}")
    with open(bpath, "wb") as f:
        f.write(raw)
    existing = sorted(glob.glob(os.path.join(bdir, f"{glob.escape(stem)}.*{ext}")))
    for old in existing[:-BACKUP_KEEP]:
        try:
            os.remove(old)
        except Exception:
            pass
    return bpath


def atomic_write(path, data):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".sioyek_tmp_", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(path, tmp)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(hist):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist[-HISTORY_KEEP:], f, ensure_ascii=False, indent=1)


def commit_files(texfiles, pdf_path, description):
    """Backs up + atomically writes the modified TexFile objects; records an undo entry."""
    entry = {"pdf": pdf_path, "time": datetime.datetime.now().isoformat(), "desc": description, "files": []}
    for tf in texfiles:
        new_data = tf.to_bytes()
        if new_data == tf.original_raw:
            continue
        bpath = make_backup(tf.path, tf.original_raw)
        atomic_write(tf.path, new_data)
        entry["files"].append({"path": tf.path, "backup": bpath, "after_sha1": sha1(new_data)})
    if entry["files"]:
        hist = load_history()
        hist.append(entry)
        save_history(hist)
    return entry


def undo_last(pdf_path):
    hist = load_history()
    idx = None
    for i in range(len(hist) - 1, -1, -1):
        if os.path.normpath(hist[i].get("pdf", "")) == os.path.normpath(pdf_path):
            idx = i
            break
    if idx is None:
        raise StudyNoteError("没有可撤销的 study note 写入")
    entry = hist[idx]
    for fi in entry["files"]:
        if not os.path.exists(fi["path"]):
            raise StudyNoteError(f"文件不存在: {fi['path']}")
        with open(fi["path"], "rb") as f:
            cur = f.read()
        if sha1(cur) != fi["after_sha1"]:
            raise StudyNoteError(f"{os.path.basename(fi['path'])} 写入后已被修改，无法自动撤销；备份在 {fi['backup']}")
        if not os.path.exists(fi["backup"]):
            raise StudyNoteError(f"备份丢失: {fi['backup']}")
    for fi in entry["files"]:
        with open(fi["backup"], "rb") as f:
            data = f.read()
        atomic_write(fi["path"], data)
    del hist[idx]
    save_history(hist)
    return entry


# ---------------------------------------------------------------------------
# LaTeX scanning
# ---------------------------------------------------------------------------

def is_escaped(s, i):
    """True if s[i] is preceded by an odd number of backslashes."""
    n = 0
    j = i - 1
    while j >= 0 and s[j] == "\\":
        n += 1
        j -= 1
    return n % 2 == 1


def split_comment(line_body):
    """Returns (code, comment) where comment starts at the first unescaped '%' (or '')."""
    for i, c in enumerate(line_body):
        if c == "%" and not is_escaped(line_body, i):
            return line_body[:i], line_body[i:]
    return line_body, ""


def strip_eol(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def env_base(env):
    return env.rstrip("*")


def scan_states(lines):
    """Returns per-line end states: (brace_depth, math_mode, env_stack_tuple, in_verbatim)."""
    states = []
    depth = 0
    math = None
    stack = []
    verb_env = None
    begin_re = re.compile(r"\s*\{([^{}]*)\}")
    for line in lines:
        s, _ = strip_eol(line)
        n = len(s)
        i = 0
        if verb_env is not None:
            m = re.search(r"\\end\s*\{" + re.escape(verb_env) + r"\}", s)
            if m:
                if stack and stack[-1] == verb_env:
                    stack.pop()
                verb_env = None
                i = m.end()
            else:
                states.append((depth, math, tuple(stack), True))
                continue
        while i < n:
            c = s[i]
            if c == "%":
                break
            if c == "\\":
                if i + 1 >= n:
                    i += 1
                    continue
                nxt = s[i + 1]
                if nxt.isalpha() or nxt == "@":
                    j = i + 1
                    while j < n and (s[j].isalpha() or s[j] == "@"):
                        j += 1
                    word = s[i + 1:j]
                    if word in ("begin", "end"):
                        m = begin_re.match(s, j)
                        if m:
                            env = m.group(1).strip()
                            j = m.end()
                            if word == "begin":
                                stack.append(env)
                                if env in VERBATIM_ENVS:
                                    m2 = re.search(r"\\end\s*\{" + re.escape(env) + r"\}", s[j:])
                                    if m2:
                                        stack.pop()
                                        j = j + m2.end()
                                    else:
                                        verb_env = env
                                        j = n
                            else:
                                if env in stack:
                                    k = len(stack) - 1 - stack[::-1].index(env)
                                    del stack[k:]
                    elif word in ("verb", "lstinline") and j < n:
                        if s[j] == "*":
                            j += 1
                        if j < n:
                            delim = s[j]
                            if delim == "{":
                                delim = "}"
                            end = s.find(delim, j + 1)
                            j = n if end < 0 else end + 1
                    i = j
                    continue
                if nxt == "(":
                    math = "\\("
                elif nxt == ")":
                    math = None
                elif nxt == "[":
                    math = "\\["
                elif nxt == "]":
                    math = None
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)
            elif c == "$":
                if i + 1 < n and s[i + 1] == "$":
                    math = None if math == "$$" else ("$$" if math is None else math)
                    i += 2
                    continue
                math = None if math == "$" else ("$" if math is None else math)
            i += 1
        states.append((depth, math, tuple(stack), verb_env is not None))
    return states


def find_document_bounds(lines):
    begin = end = None
    for i, line in enumerate(lines):
        code, _ = split_comment(strip_eol(line)[0])
        if begin is None and re.search(r"\\begin\s*\{document\}", code):
            begin = i
        elif begin is not None and re.search(r"\\end\s*\{document\}", code):
            end = i
            break
    return begin, end


def unsafe_reason(state):
    depth, math, stack, in_verb = state
    if in_verb:
        return "verbatim 环境"
    if math is not None:
        return "数学模式"
    for env in reversed(stack):
        if env_base(env) in UNSAFE_ENVS or env in VERBATIM_ENVS:
            return f"{env} 环境"
    if depth > 0:
        return "未闭合的 { } 参数"
    return None


def has_code(line):
    code, _ = split_comment(strip_eol(line)[0])
    return code.strip() != ""


def find_insert_line(lines, target_idx):
    """Returns (effective_idx, moved_reason or None)."""
    if target_idx < 0 or target_idx >= len(lines):
        raise StudyNoteError(f"行号 {target_idx + 1} 超出文件范围")
    begin, end = find_document_bounds(lines)
    idx = target_idx
    # Blank / comment-only line → previous line with code.
    while idx > 0 and not has_code(lines[idx]):
        idx -= 1
    if begin is not None and idx < begin:
        raise StudyNoteError("定位到了导言区（\\begin{document} 之前），无法插入")
    if end is not None and idx >= end:
        raise StudyNoteError("定位到了 \\end{document} 之后，无法插入")
    states = scan_states(lines)
    first_reason = unsafe_reason(states[idx])
    if first_reason is None:
        return idx, None
    limit = min(len(lines), idx + 800)
    for j in range(idx + 1, limit):
        if end is not None and j >= end:
            break
        if unsafe_reason(states[j]) is None and has_code(lines[j]):
            return j, first_reason
    raise StudyNoteError(f"第 {idx + 1} 行位于{first_reason}内，且找不到安全的插入位置")


def find_matching_brace(s, open_idx):
    depth = 0
    for i in range(open_idx, len(s)):
        c = s[i]
        if c in "{}" and is_escaped(s, i):
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def find_existing_note(code):
    """Returns (start, end_exclusive, content) of the last \\studynote{...} in code, or None."""
    last = None
    for m in STUDYNOTE_RE.finditer(code):
        if is_escaped(code, m.start()):
            continue
        open_idx = m.end() - 1
        close = find_matching_brace(code, open_idx)
        if close < 0:
            continue
        last = (m.start(), close + 1, code[open_idx + 1:close])
    return last


# ---------------------------------------------------------------------------
# Note text conversion / validation
# ---------------------------------------------------------------------------

def validate_note(text):
    """Returns error message or None."""
    depth = 0
    for i, c in enumerate(text):
        if c in "{}" and is_escaped(text, i):
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return "花括号不匹配：多了 '}'"
    if depth > 0:
        return f"花括号不匹配：缺少 {depth} 个 '}}'"
    return None


def note_to_latex(text):
    """Text is written verbatim (no escaping); only newlines become \\\\ so the note stays on one source line."""
    text = text.strip()
    text = re.sub(r"[ \t]*\r?\n[\s]*", "\n", text)  # collapse blank lines / indentation
    out = []
    math = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in "()[]":
                math = None if nxt in ")]" else nxt
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "$":
            if i + 1 < n and text[i + 1] == "$":
                math = None if math == "$$" else "$$"
                out.append("$$")
                i += 2
                continue
            math = None if math == "$" else "$"
            out.append(c)
        elif c == "\n":
            out.append(" " if math else r"\\ ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def latex_to_note(content):
    """Inverse of the newline conversion for pre-filling the editor."""
    return re.sub(r"\\\\ ?", "\n", content)


# ---------------------------------------------------------------------------
# Root file / definition injection
# ---------------------------------------------------------------------------

def file_has_begin_document(path):
    try:
        tf = TexFile(path)
    except Exception:
        return False
    return find_document_bounds(tf.lines)[0] is not None


def find_root_file(tex_path, pdf_path):
    try:
        tf = TexFile(tex_path)
        for line in tf.lines[:30]:
            m = re.match(r"\s*%\s*!\s*TEX\s+root\s*=\s*(.+?)\s*$", strip_eol(line)[0], re.I)
            if m:
                cand = os.path.normpath(os.path.join(os.path.dirname(tex_path), m.group(1)))
                if not cand.lower().endswith(".tex") and os.path.exists(cand + ".tex"):
                    cand += ".tex"
                if os.path.exists(cand):
                    return cand
    except Exception:
        pass
    same = os.path.splitext(pdf_path)[0] + ".tex"
    if os.path.exists(same):
        return same
    if file_has_begin_document(tex_path):
        return tex_path
    for cand in sorted(glob.glob(os.path.join(os.path.dirname(pdf_path), "*.tex"))):
        if file_has_begin_document(cand):
            return cand
    return None


def definition_present(tf):
    for line in tf.lines:
        code, _ = split_comment(strip_eol(line)[0])
        if MARKER_BEGIN in line or re.search(r"\\(re)?(new|provide)command\s*\{?\s*\\studynote\b", code) \
                or re.search(r"\\def\s*\\studynote\b", code):
            return True
    return False


def inject_definition(tf):
    """Inserts the macro block before \\begin{document}; returns True if modified."""
    if definition_present(tf):
        return False
    begin, _ = find_document_bounds(tf.lines)
    if begin is None:
        raise StudyNoteError(f"{os.path.basename(tf.path)} 中找不到 \\begin{{document}}")
    nl = tf.newline
    # Make sure the previous line ends with a newline.
    if begin > 0 and not tf.lines[begin - 1].endswith(("\n", "\r")):
        tf.lines[begin - 1] += nl
    block = [l + nl for l in DEFINITION_LINES]
    tf.lines[begin:begin] = block
    return True


# ---------------------------------------------------------------------------
# Core operation
# ---------------------------------------------------------------------------

def synctex_mtime(pdf_path):
    base = os.path.splitext(pdf_path)[0]
    for ext in (".synctex.gz", ".synctex"):
        if os.path.exists(base + ext):
            return os.path.getmtime(base + ext)
    return None


def map_stale_line(tex_path, line_1based, pdf_path, current_lines):
    """
    SyncTeX line numbers refer to the source as it was at compile time. If we already wrote
    notes (or injected the definition) since then, the first backup taken after the compile
    *is* the compile-time source; diff it against the current file to map the line.
    """
    st = synctex_mtime(pdf_path)
    if st is None:
        return line_1based
    try:
        if os.path.getmtime(tex_path) <= st:
            return line_1based
    except OSError:
        return line_1based
    compile_backup = None
    for entry in load_history():
        try:
            t = datetime.datetime.fromisoformat(entry.get("time", "")).timestamp()
        except Exception:
            continue
        if t < st:
            continue
        for fi in entry.get("files", []):
            if os.path.normpath(fi.get("path", "")) == os.path.normpath(tex_path) and os.path.exists(fi.get("backup", "")):
                compile_backup = fi["backup"]
                break
        if compile_backup:
            break
    if not compile_backup:
        return line_1based
    try:
        old_lines = TexFile(compile_backup).lines
    except Exception:
        return line_1based
    old_idx = line_1based - 1
    sm = difflib.SequenceMatcher(a=[strip_eol(l)[0] for l in old_lines],
                                 b=[strip_eol(l)[0] for l in current_lines], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if i1 <= old_idx < i2:
            if tag == "equal":
                return j1 + (old_idx - i1) + 1
            # replaced line (e.g. a note was appended to it): map proportionally
            return min(j1 + (old_idx - i1), max(j1, j2 - 1)) + 1
    return line_1based


class NoteTarget:
    """Everything the editor needs to know about where the note goes."""

    def __init__(self, pdf_path, page0, rect):
        self.pdf_path = pdf_path
        self.tex_path, self.synctex_line = resolve_source(pdf_path, page0, rect)
        self.tf = TexFile(self.tex_path)
        mapped = map_stale_line(self.tex_path, self.synctex_line, pdf_path, self.tf.lines)
        if mapped != self.synctex_line:
            log(f"stale synctex: line {self.synctex_line} -> {mapped}")
        self.synctex_line = mapped
        self.idx, self.moved_reason = find_insert_line(self.tf.lines, self.synctex_line - 1)
        body, _ = strip_eol(self.tf.lines[self.idx])
        code, _ = split_comment(body)
        self.existing = find_existing_note(code)
        self.root_path = find_root_file(self.tex_path, pdf_path)

    @property
    def line_no(self):
        return self.idx + 1

    def context_lines(self, radius=2):
        lo = max(0, self.idx - radius)
        hi = min(len(self.tf.lines), self.idx + radius + 1)
        return [(i + 1, strip_eol(self.tf.lines[i])[0], i == self.idx) for i in range(lo, hi)]

    def existing_text(self):
        return latex_to_note(self.existing[2]) if self.existing else ""

    def preamble_supports_unicode(self):
        path = self.root_path or self.tex_path
        try:
            tf = self.tf if path == self.tex_path else TexFile(path)
        except Exception:
            return True
        head = "".join(tf.lines[:400])
        return bool(re.search(r"ctex|xeCJK|\{CJK|CJKutf8|fontspec|luatexja|TeX\s+(TS-)?program\s*=\s*(xe|lua)", head, re.I))

    def apply(self, raw_text):
        """Writes / updates / deletes the note. Returns status message."""
        text = raw_text.strip()
        body, eol = strip_eol(self.tf.lines[self.idx])
        code, comment = split_comment(body)
        files = [self.tf]
        if not text:
            if not self.existing:
                return None
            s, e, _ = self.existing
            new_code = code[:s].rstrip(" \t") + code[e:]
            if comment and new_code and not new_code.endswith((" ", "\t")) and code[s - 1:s] in (" ", "\t"):
                new_code += " "
            self.tf.lines[self.idx] = new_code + comment + eol
            desc = f"删除 study note @ {os.path.basename(self.tex_path)}:{self.line_no}"
            commit_files(files, self.pdf_path, desc)
            return f"已删除 study note：{os.path.basename(self.tex_path)}:{self.line_no}（等待编译）"

        err = validate_note(text)
        if err:
            raise StudyNoteError(err)
        latex = note_to_latex(text)
        macro = "\\studynote{" + latex + "}"
        if self.existing:
            s, e, _ = self.existing
            new_code = code[:s] + macro + code[e:]
            verb = "已更新"
        else:
            stripped = code.rstrip(" \t")
            trailing_ws = code[len(stripped):]
            sep = " " if stripped else ""
            if comment:
                new_code = stripped + sep + macro + (trailing_ws if trailing_ws else " ")
            else:
                new_code = stripped + sep + macro + trailing_ws
            verb = "已写入"
        self.tf.lines[self.idx] = new_code + comment + eol

        shift = 0
        if self.root_path:
            if os.path.normpath(self.root_path) == os.path.normpath(self.tex_path):
                if inject_definition(self.tf):
                    shift = len(DEFINITION_LINES)
            else:
                root_tf = TexFile(self.root_path)
                if inject_definition(root_tf):
                    files.append(root_tf)
        else:
            raise StudyNoteError("找不到根文件（含 \\begin{document}），无法注入 \\studynote 定义")

        desc = f"{verb} study note @ {os.path.basename(self.tex_path)}:{self.line_no}"
        commit_files(files, self.pdf_path, desc)
        # The definition block (if injected into this file) sits above the note line.
        final_line = self.line_no + shift
        return f"{verb} {os.path.basename(self.tex_path)}:{final_line}，等待编译"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_dialog(target):
    from PyQt5.QtWidgets import (
        QApplication, QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QLabel,
        QPushButton, QMessageBox, QTextEdit,
    )
    from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
    from PyQt5.QtCore import Qt

    try:
        from .add_markdown import setup_macos_accessory, activate_macos_window
    except Exception:
        def setup_macos_accessory():
            pass

        def activate_macos_window(w):
            w.raise_()
            w.activateWindow()

    app = QApplication.instance() or QApplication(sys.argv)
    setup_macos_accessory()

    class Editor(QPlainTextEdit):
        def __init__(self, dialog):
            super().__init__()
            self.dialog = dialog

        def keyPressEvent(self, event):
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
                self.dialog.on_save()
                return
            super().keyPressEvent(event)

    class Dialog(QDialog):
        def __init__(self):
            super().__init__()
            self.result_msg = None
            self.setWindowTitle("Study Note → LaTeX")
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.resize(640, 420)
            lay = QVBoxLayout(self)

            rel = os.path.relpath(target.tex_path, os.path.dirname(target.pdf_path))
            head = f"<b>{rel}</b> &nbsp; 第 <b>{target.line_no}</b> 行"
            if target.existing:
                head += " &nbsp; <span style='color:#b36b00'>（编辑已有 study note，清空后提交 = 删除）</span>"
            lbl = QLabel(head)
            lbl.setTextFormat(Qt.RichText)
            lay.addWidget(lbl)
            if target.moved_reason:
                mv = QLabel(f"SyncTeX 定位到第 {target.synctex_line} 行（位于{target.moved_reason}内），已改为插入到第 {target.line_no} 行末尾")
                mv.setStyleSheet("color:#b36b00;")
                mv.setWordWrap(True)
                lay.addWidget(mv)

            ctx = QTextEdit()
            ctx.setReadOnly(True)
            mono = QFont("Menlo")
            mono.setStyleHint(QFont.Monospace)
            mono.setPointSize(11)
            ctx.setFont(mono)
            ctx.setLineWrapMode(QTextEdit.NoWrap)
            cur = ctx.textCursor()
            normal = QTextCharFormat()
            normal.setForeground(QColor("#777777"))
            hl = QTextCharFormat()
            hl.setBackground(QColor("#fff3c4"))
            hl.setForeground(QColor("#000000"))
            for k, (no, txt, is_target) in enumerate(target.context_lines()):
                cur.insertText(f"{no:>5} │ {txt}" + ("" if k == len(target.context_lines()) - 1 else "\n"),
                               hl if is_target else normal)
            ctx.setFixedHeight(110)
            lay.addWidget(ctx)

            lay.addWidget(QLabel("笔记内容（原样写入 LaTeX，不做转义；换行 → \\\\；Cmd+Enter 写入）："))
            self.editor = Editor(self)
            f2 = QFont()
            f2.setPointSize(13)
            self.editor.setFont(f2)
            self.editor.setPlainText(target.existing_text())
            self.editor.moveCursor(QTextCursor.End)
            lay.addWidget(self.editor, 1)

            self.err = QLabel("")
            self.err.setStyleSheet("color:#c00;")
            self.err.setWordWrap(True)
            lay.addWidget(self.err)

            row = QHBoxLayout()
            row.addStretch(1)
            if target.existing:
                bdel = QPushButton("删除")
                bdel.clicked.connect(self.on_delete)
                row.addWidget(bdel)
            bc = QPushButton("取消")
            bc.clicked.connect(self.reject)
            row.addWidget(bc)
            bs = QPushButton("写入 (⌘↩)")
            bs.setDefault(False)
            bs.clicked.connect(self.on_save)
            row.addWidget(bs)
            lay.addLayout(row)
            self.editor.setFocus()

        def showEvent(self, e):
            super().showEvent(e)
            activate_macos_window(self)
            self.editor.setFocus()

        def keyPressEvent(self, e):
            if e.key() == Qt.Key_Escape or (e.key() == Qt.Key_W and (e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))):
                self.reject()
                return
            super().keyPressEvent(e)

        def do_apply(self, text):
            try:
                self.result_msg = target.apply(text)
                self.accept()
            except StudyNoteError as ex:
                self.err.setText(str(ex))
            except Exception as ex:
                log(traceback.format_exc())
                self.err.setText(f"写入失败：{ex}")

        def on_delete(self):
            self.do_apply("")

        def on_save(self):
            text = self.editor.toPlainText()
            if text.strip():
                err = validate_note(text)
                if err:
                    self.err.setText(err + "（未写入）")
                    return
            elif not target.existing:
                self.reject()
                return
            self.do_apply(text)

    dlg = Dialog()
    dlg.exec_()
    return dlg.result_msg


def notify(sioyek, msg):
    if not msg:
        return
    try:
        sioyek.set_status_string(msg)
    except Exception:
        pass


def main():
    if len(sys.argv) < 6:
        print("Usage: python -m sioyek.add_study_note SIOYEK LOCAL_DB SHARED_DB PDF RECT|--undo [TEXT]")
        return
    sioyek_path = clean_path(sys.argv[1])
    local_db = clean_path(sys.argv[2])
    shared_db = clean_path(sys.argv[3])
    pdf_path = os.path.abspath(clean_path(sys.argv[4]))
    arg5 = clean_path(sys.argv[5])
    cli_text = sys.argv[6] if len(sys.argv) > 6 else None

    try:
        sioyek = Sioyek(sioyek_path, local_db, shared_db)
    except Exception:
        sioyek = None

    def report(msg):
        print(msg)
        log(msg)
        if sioyek is not None:
            notify(sioyek, msg)

    try:
        if arg5 == "--undo":
            entry = undo_last(pdf_path)
            names = ", ".join(os.path.basename(f["path"]) for f in entry["files"])
            report(f"已撤销：{entry['desc']}（恢复 {names}，等待编译）")
            return
        page0, rect = parse_rect(arg5)
        target = NoteTarget(pdf_path, page0, rect)
        if cli_text is not None:
            msg = target.apply(cli_text)
        else:
            msg = run_dialog(target)
        if msg:
            report(msg)
    except StudyNoteError as e:
        report(f"Study note: {e}")
        sys.exit(1)
    except Exception as e:
        log(traceback.format_exc())
        report(f"Study note 出错: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
