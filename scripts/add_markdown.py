import sys
import os
import re
import io
import base64
import traceback

import pymupdf as fitz
import markdown
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from PyQt5.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QVBoxLayout,
    QPlainTextEdit, QLabel, QPushButton, QSpinBox,
    QScrollArea, QWidget, QFrame, QSizePolicy
)
from PyQt5.QtGui import (
    QFont, QPixmap, QImage, QPainter, QTextDocument,
    QColor, QPen, QBrush
)
from PyQt5.QtCore import Qt, QTimer, QRectF, QBuffer, QIODevice

from .sioyek import Sioyek, clean_path

# Global cache for rendered math formulas
MATH_CACHE = {}

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

def render_math_to_b64(expr, is_block=False, fontsize=12, dpi=200):
    """Render a LaTeX math formula to a base64 PNG data string using matplotlib."""
    expr = expr.strip()
    if not expr:
        return None
    key = (expr, is_block, fontsize, dpi)
    if key in MATH_CACHE:
        return MATH_CACHE[key]

    try:
        fig = plt.figure(figsize=(0.01, 0.01))
        # Wrap with $...$ for mathtext parser
        math_str = f"${expr}$"
        text_obj = fig.text(0, 0, math_str, fontsize=fontsize)
        fig.canvas.draw()
        bbox = text_obj.get_window_extent(fig.canvas.get_renderer())
        fig.set_size_inches(max(0.01, bbox.width / fig.dpi), max(0.01, bbox.height / fig.dpi))
        text_obj.set_position((0, 0))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", pad_inches=0.015, transparent=True)
        plt.close(fig)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        MATH_CACHE[key] = b64
        return b64
    except Exception:
        plt.close("all")
        return None

def md_to_html(md_text):
    """Convert Markdown text to styled HTML with inline and block LaTeX math images."""
    math_blocks = []
    def replace_block(m):
        idx = len(math_blocks)
        math_blocks.append(m.group(1).strip())
        return f"\n\n__PHMATHBLOCK{idx}__\n\n"

    math_inlines = []
    def replace_inline(m):
        idx = len(math_inlines)
        math_inlines.append(m.group(1).strip())
        return f"__PHMATHINLINE{idx}__"

    # 1. Extract block math $$ ... $$
    t = re.sub(r"\$\$(.*?)\$\$", replace_block, md_text, flags=re.DOTALL)
    # 2. Extract inline math $ ... $ (ignoring escaped \$)
    t = re.sub(r"(?<!\\)\$([^\$\n]+?)(?<!\\)\$", replace_inline, t)

    # 3. Convert Markdown to HTML
    html = markdown.markdown(t, extensions=["extra", "nl2br"])

    # 4. Substitute rendered math images back into HTML
    for i, code in enumerate(math_blocks):
        b64 = render_math_to_b64(code, is_block=True, fontsize=13)
        if b64:
            sub = f'<div style="text-align: center; margin: 6px 0;"><img src="data:image/png;base64,{b64}" align="middle" /></div>'
        else:
            sub = f'<pre><code>$${code}$$</code></pre>'
        html = html.replace(f"__PHMATHBLOCK{i}__", sub)

    for i, code in enumerate(math_inlines):
        b64 = render_math_to_b64(code, is_block=False, fontsize=11)
        if b64:
            sub = f'<img src="data:image/png;base64,{b64}" align="middle" />'
        else:
            sub = f'<code>${code}$</code>'
        html = html.replace(f"__PHMATHINLINE{i}__", sub)

    css = """
    <style>
    body {
        font-family: "Helvetica Neue", Helvetica, "PingFang SC", "Microsoft YaHei", sans-serif;
        font-size: 12px;
        color: #1f2328;
        line-height: 1.45;
        margin: 0;
        padding: 0;
    }
    h1 { font-size: 15px; font-weight: bold; margin: 2px 0 6px 0; border-bottom: 1px solid #e1e4e8; padding-bottom: 2px; color: #0969da; }
    h2 { font-size: 13.5px; font-weight: bold; margin: 4px 0 4px 0; color: #1f2328; }
    h3 { font-size: 12.5px; font-weight: bold; margin: 3px 0 3px 0; }
    p { margin: 3px 0; }
    ul, ol { margin: 3px 0; padding-left: 18px; }
    li { margin: 2px 0; }
    code { background: #f0f0ee; color: #cf222e; padding: 1px 3px; font-family: Menlo, Monaco, Consolas, monospace; font-size: 11px; border-radius: 3px; }
    pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 4px; padding: 6px; font-family: Menlo, Monaco, Consolas, monospace; font-size: 11px; margin: 4px 0; }
    blockquote { margin: 4px 0; padding-left: 8px; border-left: 3px solid #0969da; color: #57606a; }
    table { border-collapse: collapse; margin: 4px 0; font-size: 11px; width: 100%; }
    th, td { border: 1px solid #d0d7de; padding: 3px 6px; }
    th { background: #f2f1ec; font-weight: bold; }
    hr { border: none; border-top: 1px solid #d0d7de; margin: 6px 0; }
    </style>
    """
    return css + html

def render_card_to_qimage(md_text, card_width=260.0):
    """Render markdown text into a styled card QImage and return (qimage, width, height)."""
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    padding = 10.0
    content_width = max(60.0, float(card_width) - 2 * padding)

    html = md_to_html(md_text)
    doc = QTextDocument()
    doc.setHtml(html)
    doc.setTextWidth(content_width)

    content_height = max(16.0, doc.size().height())
    card_height = content_height + 2 * padding

    # 2.5x scale for high-DPI crystal clear rendering on retina/zoom
    scale = 2.5
    img = QImage(int(card_width * scale), int(card_height * scale), QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.scale(scale, scale)

    # Rounded card background
    bg_rect = QRectF(0.5, 0.5, card_width - 1.0, card_height - 1.0)
    painter.setBrush(QBrush(QColor("#fcfbf7")))  # warm paper background
    painter.setPen(QPen(QColor("#cfcbbe"), 1.0)) # subtle border
    painter.drawRoundedRect(bg_rect, 6.0, 6.0)

    # Draw content inside padded area
    painter.translate(padding, padding)
    doc.drawContents(painter)
    painter.end()

    return img, card_width, card_height

def render_card_to_png_bytes(md_text, card_width=260.0):
    """Render markdown text into PNG bytes and return (png_bytes, width, height)."""
    img, w, h = render_card_to_qimage(md_text, card_width)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return buf.data().data(), w, h


class MarkdownPlainTextEdit(QPlainTextEdit):
    """Custom PlainTextEdit supporting Cmd+Enter to submit and Tab to indent."""
    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog = dialog

    def keyPressEvent(self, event):
        # Cmd+Return or Ctrl+Return -> Save
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.dialog.on_save()
            return
        # Tab -> 4 spaces
        if event.key() == Qt.Key_Tab:
            self.insertPlainText("    ")
            return
        super().keyPressEvent(event)


class MarkdownEditorDialog(QDialog):
    """Dialog for creating and editing Markdown notes with live preview."""
    def __init__(self, initial_text="", is_edit=False, initial_width=260.0, page_num=1, parent=None):
        super().__init__(parent)
        self.is_edit = is_edit
        self.action = "cancel"

        title = f"📝 编辑 Markdown 批注 (第 {page_num} 页)" if is_edit else f"📝 添加 Markdown 批注 (第 {page_num} 页)"
        self.setWindowTitle(title)
        self.resize(760, 490)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # Split content: Left Editor, Right Live Preview
        split_layout = QHBoxLayout()
        split_layout.setSpacing(12)

        # --- Left Panel: Editor ---
        left_panel = QVBoxLayout()
        left_panel.setSpacing(6)

        lbl_edit_title = QLabel("<b>Markdown / LaTeX 编辑</b>")
        lbl_edit_sub = QLabel("支持标题、列表、代码块、$行内公式$ 与 $$块级公式$$")
        lbl_edit_sub.setStyleSheet("color: #656d76; font-size: 11px;")
        left_panel.addWidget(lbl_edit_title)
        left_panel.addWidget(lbl_edit_sub)

        self.editor = MarkdownPlainTextEdit(self)
        self.editor.setFont(QFont("Menlo", 13))
        self.editor.setPlaceholderText(
            "# 笔记标题\n- 重点分析\n- 关键公式: $E = mc^2$\n\n$$\\int_0^1 x^2 dx = \\frac{1}{3}$$\n\n`code snippet`"
        )
        if initial_text:
            self.editor.setPlainText(initial_text)
            cursor = self.editor.textCursor()
            cursor.movePosition(cursor.End)
            self.editor.setTextCursor(cursor)
        left_panel.addWidget(self.editor)

        # Width Control Bar
        width_bar = QHBoxLayout()
        lbl_w = QLabel("卡片宽度:")
        lbl_w.setStyleSheet("color: #444; font-size: 12px;")
        self.spin_width = QSpinBox()
        self.spin_width.setRange(140, 600)
        self.spin_width.setSingleStep(20)
        self.spin_width.setSuffix(" pt")
        self.spin_width.setValue(int(initial_width))
        self.spin_width.valueChanged.connect(self.schedule_preview)
        width_bar.addWidget(lbl_w)
        width_bar.addWidget(self.spin_width)
        width_bar.addStretch()
        left_panel.addLayout(width_bar)

        split_layout.addLayout(left_panel, 3)

        # --- Right Panel: Live Preview ---
        right_panel = QVBoxLayout()
        right_panel.setSpacing(6)

        lbl_prev_title = QLabel("<b>卡片效果预览</b>")
        lbl_prev_sub = QLabel("将直接作为 Stamp 独立批注内嵌至 PDF")
        lbl_prev_sub.setStyleSheet("color: #656d76; font-size: 11px;")
        right_panel.addWidget(lbl_prev_title)
        right_panel.addWidget(lbl_prev_sub)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("background-color: #eef0f3; border: 1px solid #d0d7de; border-radius: 4px;")

        self.preview_container = QWidget()
        self.preview_container.setStyleSheet("background-color: transparent;")
        container_layout = QVBoxLayout(self.preview_container)
        container_layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        container_layout.setContentsMargins(10, 10, 10, 10)

        self.lbl_preview = QLabel()
        self.lbl_preview.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        container_layout.addWidget(self.lbl_preview)

        self.scroll_area.setWidget(self.preview_container)
        right_panel.addWidget(self.scroll_area)

        split_layout.addLayout(right_panel, 3)
        main_layout.addLayout(split_layout)

        # --- Bottom Action Bar ---
        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(0, 4, 0, 0)

        if self.is_edit:
            self.btn_delete = QPushButton("🗑️ 删除此批注")
            self.btn_delete.setStyleSheet("""
                QPushButton {
                    color: #cf222e;
                    background-color: #ffffff;
                    border: 1px solid #d0d7de;
                    border-radius: 6px;
                    padding: 6px 12px;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background-color: #ffebe9;
                    border-color: #cf222e;
                }
            """)
            self.btn_delete.clicked.connect(self.on_delete)
            bottom_bar.addWidget(self.btn_delete)

        lbl_tips = QLabel("按 <b>⌘↵</b> (Cmd+Enter) 保存 • <b>Esc</b> 取消")
        lbl_tips.setStyleSheet("color: #656d76; font-size: 12px; margin-left: 6px;")
        bottom_bar.addWidget(lbl_tips)

        bottom_bar.addStretch()

        self.btn_cancel = QPushButton("取消 (Esc)")
        self.btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #24292f;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
            }
        """)
        self.btn_cancel.clicked.connect(self.reject)
        bottom_bar.addWidget(self.btn_cancel)

        save_label = "更新批注 (⌘↵)" if self.is_edit else "插入批注 (⌘↵)"
        self.btn_save = QPushButton(save_label)
        self.btn_save.setDefault(True)
        self.btn_save.setStyleSheet("""
            QPushButton {
                background-color: #0969da;
                color: #ffffff;
                border: 1px solid #0969da;
                border-radius: 6px;
                padding: 6px 16px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #085cc0;
            }
        """)
        self.btn_save.clicked.connect(self.on_save)
        bottom_bar.addWidget(self.btn_save)

        main_layout.addLayout(bottom_bar)

        # Debounced live preview timer
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(220)
        self.preview_timer.timeout.connect(self.update_preview)
        self.editor.textChanged.connect(self.schedule_preview)

        # Initial render
        self.update_preview()

    def schedule_preview(self):
        self.preview_timer.start()

    def update_preview(self):
        text = self.editor.toPlainText().strip()
        if not text:
            text = "*（空白卡片）*"
        width = float(self.spin_width.value())
        img, w, h = render_card_to_qimage(text, width)
        pix = QPixmap.fromImage(img)
        # Scale down for 1:1 visual preview display since image was rendered at 2.5x DPI
        preview_pix = pix.scaled(int(w), int(h), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.lbl_preview.setPixmap(preview_pix)
        self.lbl_preview.setFixedSize(preview_pix.size())

    def on_save(self):
        self.action = "save"
        self.accept()

    def on_delete(self):
        self.action = "delete"
        self.accept()

    def get_text(self):
        return self.editor.toPlainText()

    def get_width(self):
        return float(self.spin_width.value())


def main():
    if len(sys.argv) < 6:
        print("Usage: python -m sioyek.add_markdown SIOYEK LOCAL_DB SHARED_DB PDF RECT [TEXT]")
        return

    SIOYEK_PATH = clean_path(sys.argv[1])
    LOCAL_DATABASE_PATH = clean_path(sys.argv[2])
    SHARED_DATABASE_PATH = clean_path(sys.argv[3])
    FILE_PATH = clean_path(sys.argv[4])
    rect_string = clean_path(sys.argv[5])

    # Optional pre-provided text for CLI/testing
    cli_text = clean_path(sys.argv[6]) if len(sys.argv) > 6 else None

    # Initialize QApplication before any Qt objects or Sioyek socket
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    sioyek = Sioyek(SIOYEK_PATH, LOCAL_DATABASE_PATH, SHARED_DATABASE_PATH)
    log_path = os.path.expanduser("~/.config/sioyek/add_text.log")

    try:
        doc = fitz.open(FILE_PATH)
        selected_page, selected_rect = parse_rect(rect_string)

        if selected_page < 0 or selected_page >= len(doc):
            return

        page = doc[selected_page]

        # Handle page crop/media origin offset
        if page.rect.x0 != 0 or page.rect.y0 != 0:
            selected_rect[0] += page.rect.x0
            selected_rect[1] += page.rect.y0
            selected_rect[2] += page.rect.x0
            selected_rect[3] += page.rect.y0

        search_rect = fitz.Rect(selected_rect)
        is_click = (search_rect.width < 12 and search_rect.height < 12)
        if is_click:
            # Expand search area for single-click to make targeting easy
            search_rect.x0 -= 16
            search_rect.y0 -= 16
            search_rect.x1 += 16
            search_rect.y1 += 16

        # --- Secondary Edit Detection: check for existing Markdown card or annotation ---
        candidates = []
        for annot in page.annots():
            is_stamp = (annot.type[1] == 'Stamp')
            is_freetext = (annot.type[1] in ('FreeText', 'Text'))
            is_markdown = (annot.info.get('subject') == 'sioyek_markdown')

            if (is_stamp or is_freetext) and annot.rect.intersects(search_rect):
                overlap = annot.rect.intersect(search_rect)
                score = overlap.width * overlap.height
                if is_markdown:
                    score += 100000.0  # High priority to Sioyek Markdown stamps
                candidates.append((score, annot))

        # Fallback for point-click near annot
        if not candidates and is_click:
            click_pt = fitz.Point((search_rect.x0 + search_rect.x1) / 2, (search_rect.y0 + search_rect.y1) / 2)
            for annot in page.annots():
                if annot.type[1] in ('Stamp', 'FreeText', 'Text'):
                    dist = annot.rect.distance_to_point(click_pt)
                    if dist < 28.0:
                        score = 100.0 - dist
                        if annot.info.get('subject') == 'sioyek_markdown':
                            score += 100000.0
                        candidates.append((score, annot))

        existing_annot = None
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            existing_annot = candidates[0][1]

        # Determine mode, anchor and initial width
        if existing_annot is not None:
            is_edit_mode = True
            initial_text = existing_annot.info.get('content', '')
            if is_click or abs(selected_rect[2] - selected_rect[0]) < 30:
                target_x0 = existing_annot.rect.x0
                target_y0 = existing_annot.rect.y0
                target_width = existing_annot.rect.width
            else:
                target_x0 = min(selected_rect[0], selected_rect[2])
                target_y0 = min(selected_rect[1], selected_rect[3])
                target_width = max(120.0, abs(selected_rect[2] - selected_rect[0]))
        else:
            is_edit_mode = False
            initial_text = ""
            target_x0 = min(selected_rect[0], selected_rect[2])
            target_y0 = min(selected_rect[1], selected_rect[3])
            sel_w = abs(selected_rect[2] - selected_rect[0])
            target_width = max(140.0, sel_w) if sel_w > 40 else 260.0

        # Run dialog or CLI input
        if cli_text is not None:
            # Automated / CLI mode
            action = "save"
            new_text = cli_text
            card_width = target_width
        else:
            # Interactive PyQt5 dialog
            app = QApplication.instance()
            if not app:
                app = QApplication(sys.argv)

            dialog = MarkdownEditorDialog(
                initial_text=initial_text,
                is_edit=is_edit_mode,
                initial_width=target_width,
                page_num=selected_page + 1
            )
            dialog.exec_()
            action = dialog.action
            new_text = dialog.get_text()
            card_width = dialog.get_width()

        def safe_notify(msg=None):
            try:
                sioyek.reload()
                if msg:
                    sioyek.set_status_string(msg)
            except Exception:
                pass

        # Handle user actions
        if action == "cancel":
            doc.close()
            return

        if action == "delete":
            if existing_annot is not None:
                content = existing_annot.info.get('content', '')
                page.delete_annot(existing_annot)
                doc.saveIncr()
                safe_notify(f"Deleted note: {content[:20]}")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"Success: deleted Markdown note on page {selected_page}: '{content}'\n")
            doc.close()
            return

        if action == "save":
            stripped_text = new_text.strip()
            if not stripped_text:
                # Text cleared: delete note
                if existing_annot is not None:
                    page.delete_annot(existing_annot)
                    doc.saveIncr()
                    safe_notify("Markdown note deleted")
                doc.close()
                return

            # Render card PNG
            png_bytes, final_w, final_h = render_card_to_png_bytes(stripped_text, card_width)

            # If editing existing annotation, delete previous one first
            if existing_annot is not None:
                page.delete_annot(existing_annot)

            # Insert new stamp annotation
            final_rect = fitz.Rect(target_x0, target_y0, target_x0 + final_w, target_y0 + final_h)
            annot = page.add_stamp_annot(final_rect, stamp=png_bytes)
            annot.set_info({
                'content': stripped_text,
                'subject': 'sioyek_markdown',
                'title': 'Markdown Note'
            })
            annot.update()

            doc.saveIncr()

            msg = "Updated Markdown note" if is_edit_mode else "Added Markdown note"
            safe_notify(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Success: {msg} on page {selected_page} rect {final_rect}\n")

        doc.close()

    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Error in add_markdown: {e}\n")
            traceback.print_exc(file=f)
        raise

if __name__ == '__main__':
    main()
