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
    QScrollArea, QWidget, QFrame, QSizePolicy,
    QSplitter, QCheckBox, QRadioButton, QButtonGroup,
    QTextBrowser
)
from PyQt5.QtGui import (
    QFont, QFontMetrics, QPixmap, QImage, QPainter,
    QTextDocument, QColor, QPen, QBrush
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

def rect_distance_to_point(rect, pt):
    """Calculates Euclidean distance from a point to a rectangle."""
    dx = max(rect.x0 - pt.x, 0.0, pt.x - rect.x1)
    dy = max(rect.y0 - pt.y, 0.0, pt.y - rect.y1)
    return (dx * dx + dy * dy) ** 0.5

def get_last_mode():
    path = os.path.expanduser("~/.config/sioyek/last_md_mode.txt")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                mode = f.read().strip()
                if mode in ("pill", "card"):
                    return mode
        except Exception:
            pass
    return "card"

def set_last_mode(mode):
    path = os.path.expanduser("~/.config/sioyek/last_md_mode.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(mode)
    except Exception:
        pass

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

def md_to_html(md_text, for_reader=False):
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

    block_fs = 14 if for_reader else 13
    inline_fs = 12 if for_reader else 11

    # 4. Substitute rendered math images back into HTML
    for i, code in enumerate(math_blocks):
        b64 = render_math_to_b64(code, is_block=True, fontsize=block_fs)
        if b64:
            sub = f'<div style="text-align: center; margin: 8px 0;"><img src="data:image/png;base64,{b64}" align="middle" /></div>'
        else:
            sub = f'<pre><code>$${code}$$</code></pre>'
        html = html.replace(f"__PHMATHBLOCK{i}__", sub)

    for i, code in enumerate(math_inlines):
        b64 = render_math_to_b64(code, is_block=False, fontsize=inline_fs)
        if b64:
            sub = f'<img src="data:image/png;base64,{b64}" align="middle" />'
        else:
            sub = f'<code>${code}$</code>'
        html = html.replace(f"__PHMATHINLINE{i}__", sub)

    base_font_size = "14px" if for_reader else "12px"
    line_height = "1.6" if for_reader else "1.45"
    h1_size = "18px" if for_reader else "15px"
    h2_size = "16px" if for_reader else "13.5px"

    css = f"""
    <style>
    body {{
        font-family: "PingFang SC", "Helvetica Neue", Helvetica, "Microsoft YaHei", sans-serif;
        font-size: {base_font_size};
        color: #1e293b;
        line-height: {line_height};
        margin: 0;
        padding: 0;
    }}
    h1 {{ font-size: {h1_size}; font-weight: bold; margin: 4px 0 8px 0; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; color: #0f172a; }}
    h2 {{ font-size: {h2_size}; font-weight: bold; margin: 6px 0 6px 0; color: #1e293b; }}
    h3 {{ font-size: 13.5px; font-weight: bold; margin: 4px 0 4px 0; }}
    p {{ margin: 4px 0; }}
    ul, ol {{ margin: 4px 0; padding-left: 20px; }}
    li {{ margin: 3px 0; }}
    code {{ background: #f1f5f9; color: #dc2626; padding: 2px 4px; font-family: Menlo, Monaco, Consolas, monospace; font-size: 12px; border-radius: 4px; }}
    pre {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px; font-family: Menlo, Monaco, Consolas, monospace; font-size: 12px; margin: 6px 0; }}
    blockquote {{ margin: 6px 0; padding-left: 10px; border-left: 4px solid #3b82f6; color: #64748b; font-style: normal; }}
    table {{ border-collapse: collapse; margin: 6px 0; font-size: 12px; width: 100%; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 4px 8px; }}
    th {{ background: #f1f5f9; font-weight: bold; }}
    hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 8px 0; }}
    </style>
    """
    return css + html

def extract_pill_title(md_text):
    """Extract a short title for the pill badge from the first non-empty line."""
    for line in md_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"[*_`]", "", line)
        line = re.sub(r"\$([^\$]+)\$", r"\1", line) # keep math symbols
        line = line.strip()
        if line:
            if len(line) > 28:
                return line[:27] + "…"
            return line
    return "便签"

def render_pill_badge_to_qimage(md_text):
    """Render a compact Pill Badge (e.g. 📌 定理摘要) to a QImage."""
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    title = extract_pill_title(md_text)
    display_text = f"📌 {title}"

    font = QFont("PingFang SC", 10)
    font.setBold(True)
    fm = QFontMetrics(font)
    text_w = fm.horizontalAdvance(display_text)

    padding_x = 10.0
    badge_w = text_w + 2 * padding_x
    badge_h = 22.0

    scale = 2.5
    img = QImage(int(badge_w * scale), int(badge_h * scale), QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)
    painter.scale(scale, scale)

    # Pill rounded background
    bg_rect = QRectF(0.5, 0.5, badge_w - 1.0, badge_h - 1.0)
    painter.setBrush(QBrush(QColor("#fef3c7"))) # warm amber/yellow
    painter.setPen(QPen(QColor("#d97706"), 1.0)) # amber border
    painter.drawRoundedRect(bg_rect, badge_h / 2, badge_h / 2)

    # Text
    painter.setFont(font)
    painter.setPen(QPen(QColor("#92400e"))) # dark amber text
    painter.drawText(bg_rect, Qt.AlignCenter, display_text)
    painter.end()

    return img, badge_w, badge_h

def render_pill_badge_to_png_bytes(md_text):
    """Render pill badge into PNG bytes and return (png_bytes, width, height)."""
    img, w, h = render_pill_badge_to_qimage(md_text)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return buf.data().data(), w, h

def render_card_to_qimage(md_text, card_width=260.0):
    """Render markdown text into a styled card QImage and return (qimage, width, height)."""
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    padding = 10.0
    content_width = max(60.0, float(card_width) - 2 * padding)

    html = md_to_html(md_text, for_reader=False)
    doc = QTextDocument()
    doc.setHtml(html)
    doc.setTextWidth(content_width)

    content_height = max(16.0, doc.size().height())
    card_height = content_height + 2 * padding

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


class PreviewScrollArea(QScrollArea):
    """ScrollArea that notifies the dialog when its viewport is resized."""
    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog = dialog

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().isValid() and event.oldSize() != event.size():
            self.dialog.on_preview_area_resized()


def setup_macos_accessory():
    """Configure app as macOS accessory so it does not clutter the Dock."""
    try:
        import AppKit
        ns_app = AppKit.NSApplication.sharedApplication()
        ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def activate_macos_window(widget):
    """Brings the dialog to front and forces focus on macOS."""
    widget.raise_()
    widget.activateWindow()
    try:
        import AppKit
        ns_app = AppKit.NSApplication.sharedApplication()
        ns_app.activateIgnoringOtherApps_(True)
    except Exception:
        pass


class MarkdownReaderDialog(QDialog):
    """Large popup window for comfortably reading rendered Markdown with LaTeX formulas."""
    def __init__(self, md_text, page_num=1, parent=None):
        super().__init__(parent)
        self.md_text = md_text
        self.action = "close"

        title_text = extract_pill_title(md_text)
        self.setWindowTitle(f"📖 Markdown 批注阅读 - 第 {page_num} 页 ({title_text})")
        self.resize(780, 560)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Header bar
        header = QHBoxLayout()
        lbl_icon = QLabel(f"<span style='font-size: 15px; font-weight: bold; color: #0969da;'>📖 第 {page_num} 页批注</span>")
        header.addWidget(lbl_icon)

        lbl_summary = QLabel(f"<span style='color: #656d76; font-size: 12px;'>（共 {len(md_text.strip())} 字）</span>")
        header.addWidget(lbl_summary)

        header.addStretch()

        self.btn_edit = QPushButton("✏️ 编辑此笔记 (E)")
        self.btn_edit.setStyleSheet("""
            QPushButton {
                background-color: #0969da;
                color: #ffffff;
                border: 1px solid #0969da;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #085cc0;
            }
        """)
        self.btn_edit.clicked.connect(self.on_edit)
        header.addWidget(self.btn_edit)

        self.btn_close = QPushButton("✕ 关闭 (Esc)")
        self.btn_close.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #24292f;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #f3f4f6;
            }
        """)
        self.btn_close.clicked.connect(self.reject)
        header.addWidget(self.btn_close)

        layout.addLayout(header)

        # Main reading viewer: QTextBrowser
        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(True)
        self.viewer.setStyleSheet("""
            QTextBrowser {
                background-color: #fcfbf7;
                border: 1px solid #d0d7de;
                border-radius: 8px;
                padding: 16px;
                selection-background-color: #b6d4fe;
            }
        """)

        reader_html = md_to_html(md_text, for_reader=True)
        self.viewer.setHtml(reader_html)
        layout.addWidget(self.viewer, 1)

    def showEvent(self, event):
        super().showEvent(event)
        activate_macos_window(self)

    def closeEvent(self, event):
        self.action = "close"
        event.accept()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.action = "close"
            self.reject()
            return
        if event.key() == Qt.Key_W and (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.action = "close"
            self.reject()
            return
        if event.key() == Qt.Key_E and not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.on_edit()
            return
        super().keyPressEvent(event)

    def on_edit(self):
        self.action = "edit"
        self.accept()


class MarkdownPlainTextEdit(QPlainTextEdit):
    """Custom PlainTextEdit supporting Cmd+Enter to submit and Tab to indent."""
    def __init__(self, dialog, parent=None):
        super().__init__(parent)
        self.dialog = dialog

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.dialog.on_save()
            return
        if event.key() == Qt.Key_Tab:
            self.insertPlainText("    ")
            return
        super().keyPressEvent(event)


class MarkdownEditorDialog(QDialog):
    """Dialog for creating and editing Markdown notes with responsive live preview and mode toggle."""
    def __init__(self, initial_text="", is_edit=False, initial_width=260.0, page_num=1, initial_mode=None, parent=None):
        super().__init__(parent)
        self.is_edit = is_edit
        self.action = "cancel"
        self.initial_width = float(initial_width)

        if initial_mode in ("pill", "card"):
            self.mode = initial_mode
        else:
            self.mode = get_last_mode()

        title = f"📝 编辑 Markdown 批注 (第 {page_num} 页)" if is_edit else f"📝 添加 Markdown 批注 (第 {page_num} 页)"
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # Mode Selector Bar: Pill Badge vs Full Card
        mode_bar = QHBoxLayout()
        lbl_mode = QLabel("<b>页面形式:</b>")
        lbl_mode.setStyleSheet("color: #24292f; font-size: 12px;")
        mode_bar.addWidget(lbl_mode)

        self.radio_pill = QRadioButton("📌 胶囊便签 (轻量折叠，Shift+Click看大窗)")
        self.radio_card = QRadioButton("📋 展开卡片 (直接在页面显示完整内容)")
        self.btn_group_mode = QButtonGroup(self)
        self.btn_group_mode.addButton(self.radio_pill)
        self.btn_group_mode.addButton(self.radio_card)

        if self.mode == "pill":
            self.radio_pill.setChecked(True)
        else:
            self.radio_card.setChecked(True)

        mode_bar.addWidget(self.radio_pill)
        mode_bar.addWidget(self.radio_card)
        mode_bar.addStretch()
        main_layout.addLayout(mode_bar)

        # QSplitter between Left Editor and Right Live Preview
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        # --- Left Panel: Editor ---
        left_widget = QWidget()
        left_panel = QVBoxLayout(left_widget)
        left_panel.setContentsMargins(0, 0, 0, 0)
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
        self.width_widget = QWidget()
        width_bar = QHBoxLayout(self.width_widget)
        width_bar.setContentsMargins(0, 0, 0, 0)
        lbl_w = QLabel("卡片宽度:")
        lbl_w.setStyleSheet("color: #444; font-size: 12px;")
        self.spin_width = QSpinBox()
        self.spin_width.setRange(120, 1200)
        self.spin_width.setSingleStep(20)
        self.spin_width.setSuffix(" pt")
        self.spin_width.setValue(int(initial_width))

        self.chk_autofit = QCheckBox("自适应窗口")
        self.chk_autofit.setToolTip("卡片宽度自动跟随右侧预览窗口大小变化；手动调整数值时自动取消勾选")
        self.chk_autofit.setChecked(True)

        width_bar.addWidget(lbl_w)
        width_bar.addWidget(self.spin_width)
        width_bar.addWidget(self.chk_autofit)
        width_bar.addStretch()
        left_panel.addWidget(self.width_widget)

        self.splitter.addWidget(left_widget)

        # --- Right Panel: Live Preview ---
        right_widget = QWidget()
        right_panel = QVBoxLayout(right_widget)
        right_panel.setContentsMargins(0, 0, 0, 0)
        right_panel.setSpacing(6)

        lbl_prev_title = QLabel("<b>效果实时预览</b>")
        self.lbl_prev_sub = QLabel("将直接作为 Stamp 独立批注内嵌至 PDF（可拖动中间分割线改变宽度）")
        self.lbl_prev_sub.setStyleSheet("color: #656d76; font-size: 11px;")
        right_panel.addWidget(lbl_prev_title)
        right_panel.addWidget(self.lbl_prev_sub)

        self.scroll_area = PreviewScrollArea(self)
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

        self.lbl_pill_hint = QLabel("<span style='color: #64748b; font-size: 11px;'>💡 页面将显示该胶囊，按 <b>Shift+Click</b> 即可呼出大窗口完整阅读</span>")
        self.lbl_pill_hint.setAlignment(Qt.AlignCenter)
        container_layout.addWidget(self.lbl_pill_hint)

        self.scroll_area.setWidget(self.preview_container)
        right_panel.addWidget(self.scroll_area)

        self.splitter.addWidget(right_widget)
        main_layout.addWidget(self.splitter, 1)

        # Initial layout sizes
        right_init_w = max(280, int(initial_width + 30))
        self.resize(380 + right_init_w + 30, 520)
        self.splitter.setSizes([380, right_init_w])

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

        # Resizing debounce timer
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.setInterval(30)
        self.resize_timer.timeout.connect(self.update_autofit_width)

        self.spin_width.valueChanged.connect(self.on_spin_changed)
        self.chk_autofit.toggled.connect(self.on_autofit_toggled)
        self.radio_pill.toggled.connect(self.on_mode_changed)

        # Debounced editor typing timer
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(200)
        self.preview_timer.timeout.connect(self.update_preview)
        self.editor.textChanged.connect(self.schedule_preview)

        # Initial UI sync and render
        self.on_mode_changed()

    def on_mode_changed(self):
        if self.radio_pill.isChecked():
            self.mode = "pill"
            self.width_widget.setEnabled(False)
            self.lbl_prev_sub.setText("页面上呈现迷你胶囊便签，点击或 Shift+Click 即可弹窗展开阅读")
            self.lbl_pill_hint.setVisible(True)
        else:
            self.mode = "card"
            self.width_widget.setEnabled(True)
            self.lbl_prev_sub.setText("页面上呈现完整展开卡片（可拖动中间分割线改变宽度）")
            self.lbl_pill_hint.setVisible(False)
        set_last_mode(self.mode)
        self.update_preview()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().isValid() and event.oldSize() != event.size():
            self.on_preview_area_resized()

    def on_preview_area_resized(self):
        if self.mode == "card" and self.chk_autofit.isChecked():
            self.resize_timer.start(30)
        else:
            self.schedule_preview()

    def update_autofit_width(self):
        if self.mode != "card":
            return
        v_w = self.scroll_area.viewport().width()
        new_w = max(140, int(v_w - 24))
        if new_w != self.spin_width.value():
            self.spin_width.blockSignals(True)
            self.spin_width.setValue(new_w)
            self.spin_width.blockSignals(False)
            self.update_preview()

    def on_spin_changed(self, val):
        self.chk_autofit.blockSignals(True)
        self.chk_autofit.setChecked(False)
        self.chk_autofit.blockSignals(False)
        self.schedule_preview()

    def on_autofit_toggled(self, checked):
        if checked:
            self.update_autofit_width()
        else:
            self.schedule_preview()

    def schedule_preview(self):
        self.preview_timer.start()

    def update_preview(self):
        text = self.editor.toPlainText().strip()
        if not text:
            text = "*（空白卡片）*"

        if self.mode == "pill":
            img, w, h = render_pill_badge_to_qimage(text)
        else:
            width = float(self.spin_width.value())
            img, w, h = render_card_to_qimage(text, width)

        pix = QPixmap.fromImage(img)
        preview_pix = pix.scaled(int(w), int(h), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.lbl_preview.setPixmap(preview_pix)
        self.lbl_preview.setFixedSize(preview_pix.size())
        self.preview_container.adjustSize()

    def showEvent(self, event):
        super().showEvent(event)
        activate_macos_window(self)

    def closeEvent(self, event):
        self.action = "cancel"
        event.accept()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.action = "cancel"
            self.reject()
            return
        if event.key() == Qt.Key_W and (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self.action = "cancel"
            self.reject()
            return
        super().keyPressEvent(event)

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

    def get_mode(self):
        return "pill" if self.radio_pill.isChecked() else "card"


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
    setup_macos_accessory()

    sioyek = Sioyek(SIOYEK_PATH, LOCAL_DATABASE_PATH, SHARED_DATABASE_PATH)
    log_path = os.path.expanduser("~/.config/sioyek/add_text.log")

    try:
        doc = fitz.open(FILE_PATH)
        selected_page, selected_rect = parse_rect(rect_string)

        if selected_page < 0 or selected_page >= len(doc):
            app.quit()
            sys.exit(0)

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
            search_rect.x0 -= 16
            search_rect.y0 -= 16
            search_rect.x1 += 16
            search_rect.y1 += 16

        # --- Secondary Edit Detection ---
        candidates = []
        for annot in page.annots():
            is_stamp = (annot.type[1] == 'Stamp')
            is_freetext = (annot.type[1] in ('FreeText', 'Text'))
            subj = annot.info.get('subject', '')
            is_markdown = subj in ('sioyek_markdown', 'sioyek_markdown_pill', 'sioyek_markdown_card')

            if (is_stamp or is_freetext) and annot.rect.intersects(search_rect):
                overlap = annot.rect.intersect(search_rect)
                score = overlap.width * overlap.height
                if is_markdown:
                    score += 100000.0
                candidates.append((score, annot))

        if not candidates and is_click:
            click_pt = fitz.Point((search_rect.x0 + search_rect.x1) / 2, (search_rect.y0 + search_rect.y1) / 2)
            for annot in page.annots():
                if annot.type[1] in ('Stamp', 'FreeText', 'Text'):
                    dist = rect_distance_to_point(annot.rect, click_pt)
                    if dist < 28.0:
                        score = 100.0 - dist
                        if annot.info.get('subject') in ('sioyek_markdown', 'sioyek_markdown_pill', 'sioyek_markdown_card'):
                            score += 100000.0
                        candidates.append((score, annot))

        existing_annot = None
        existing_mode = None
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            existing_annot = candidates[0][1]
            subj = existing_annot.info.get('subject', '')
            if subj == 'sioyek_markdown_pill':
                existing_mode = 'pill'
            elif subj in ('sioyek_markdown_card', 'sioyek_markdown'):
                existing_mode = 'card'

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
            action = "save"
            new_text = cli_text
            card_width = target_width
            mode = existing_mode or "card"
        else:
            dialog = MarkdownEditorDialog(
                initial_text=initial_text,
                is_edit=is_edit_mode,
                initial_width=target_width,
                page_num=selected_page + 1,
                initial_mode=existing_mode
            )
            dialog.exec_()
            action = dialog.action
            new_text = dialog.get_text()
            card_width = dialog.get_width()
            mode = dialog.get_mode()
            dialog.close()
            dialog.deleteLater()

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

        if action == "cancel":
            doc.close()
            app.quit()
            sys.exit(0)

        if action == "delete":
            if existing_annot is not None:
                content = existing_annot.info.get('content', '')
                page.delete_annot(existing_annot)
                doc.saveIncr()
                safe_notify(f"Deleted note: {content[:20]}")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"Success: deleted Markdown note on page {selected_page}: '{content}'\n")
            doc.close()
            app.quit()
            sys.exit(0)

        if action == "save":
            stripped_text = new_text.strip()
            if not stripped_text:
                if existing_annot is not None:
                    page.delete_annot(existing_annot)
                    doc.saveIncr()
                    safe_notify("Markdown note deleted")
                doc.close()
                app.quit()
                sys.exit(0)

            if mode == "pill":
                png_bytes, final_w, final_h = render_pill_badge_to_png_bytes(stripped_text)
                new_subj = "sioyek_markdown_pill"
            else:
                png_bytes, final_w, final_h = render_card_to_png_bytes(stripped_text, card_width)
                new_subj = "sioyek_markdown_card"

            if existing_annot is not None:
                page.delete_annot(existing_annot)

            final_rect = fitz.Rect(target_x0, target_y0, target_x0 + final_w, target_y0 + final_h)
            annot = page.add_stamp_annot(final_rect, stamp=png_bytes)
            annot.set_info({
                'content': stripped_text,
                'subject': new_subj,
                'title': f'Markdown [{mode}]'
            })
            annot.update()

            doc.saveIncr()

            msg = f"Updated Markdown {mode}" if is_edit_mode else f"Added Markdown {mode}"
            safe_notify(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Success: {msg} on page {selected_page} rect {final_rect}\n")

        doc.close()
        app.quit()
        sys.exit(0)

    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"Error in add_markdown: {e}\n")
            traceback.print_exc(file=f)
        try:
            app.quit()
        except Exception:
            pass
        sys.exit(1)

if __name__ == '__main__':
    main()
