"""底稿富文本的最小安全格式层。

数据库保留两个视图：富文本 HTML 仅用于编辑器还原排版；对应的纯文本列继续
承担检索、导出、交流和审计判断。这里不接受任意 HTML，避免本地项目文件在
再次打开时把脚本或远程资源带入编辑器。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_ALLOWED_TAGS = {
    "p", "div", "br", "strong", "b", "em", "i", "u", "span",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
}
_BLOCK_TAGS = {"p", "div", "li", "tr"}
_VOID_TAGS = {"br"}
_COLOR_RE = re.compile(r"^(?:#[0-9a-fA-F]{3,8}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\))$")
_FONT_RE = re.compile(r"^[A-Za-z0-9\s,\-\"']{1,120}$")
_FONT_SIZE_RE = re.compile(r"^(?:[89]|[1-9]\d|1[0-4]\d)px$")
_FONT_WEIGHT_RE = re.compile(r"^(?:normal|bold|[1-9]00)$")
_TABLE_WIDTH_RE = re.compile(r"^(?:(?:[3-9]\d|100)%|(?:1[6-9]\d|[2-9]\d\d|1[0-6]\d\d)px)$")
_TABLE_HEIGHT_RE = re.compile(r"^(?:4[8-9]|[5-9]\d|[1-9]\d\d|1[0-6]\d\d)px$")


def _safe_style(raw: str) -> str:
    allowed: list[str] = []
    for item in str(raw or "").split(";"):
        if ":" not in item:
            continue
        name, value = (part.strip() for part in item.split(":", 1))
        lower_name, lower_value = name.lower(), value.lower()
        if lower_name == "color" and _COLOR_RE.fullmatch(value):
            allowed.append(f"color:{value}")
        elif lower_name == "font-family" and _FONT_RE.fullmatch(value):
            allowed.append(f"font-family:{value}")
        elif lower_name == "font-size" and _FONT_SIZE_RE.fullmatch(lower_value):
            allowed.append(f"font-size:{lower_value}")
        elif lower_name == "font-weight" and _FONT_WEIGHT_RE.fullmatch(lower_value):
            allowed.append(f"font-weight:{lower_value}")
        elif lower_name == "text-decoration" and lower_value in {"none", "underline"}:
            allowed.append(f"text-decoration:{lower_value}")
    return ";".join(allowed)


def _safe_table_style(raw: str) -> str:
    """表格只允许保存用户手动调整的宽度和高度；不接受任意 CSS。"""
    allowed: list[str] = []
    for item in str(raw or "").split(";"):
        if ":" not in item:
            continue
        name, value = (part.strip() for part in item.split(":", 1))
        if name.lower() == "width" and _TABLE_WIDTH_RE.fullmatch(value.lower()):
            allowed.append(f"width:{value.lower()}")
        elif name.lower() == "height" and _TABLE_HEIGHT_RE.fullmatch(value.lower()):
            allowed.append(f"height:{value.lower()}")
    return ";".join(allowed)


class _RichTextSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.text_parts: list[str] = []
        self._open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}
        # 浏览器 execCommand 常生成 <font>；规范化成受控 span，避免保存旧标签。
        if tag == "font":
            style_bits: list[str] = []
            if _COLOR_RE.fullmatch(attr_map.get("color", "")):
                style_bits.append(f"color:{attr_map['color']}")
            if _FONT_RE.fullmatch(attr_map.get("face", "")):
                style_bits.append(f"font-family:{attr_map['face']}")
            size_map = {"1": "10px", "2": "12px", "3": "14px", "4": "16px", "5": "18px", "6": "24px", "7": "32px"}
            if attr_map.get("size") in size_map:
                style_bits.append(f"font-size:{size_map[attr_map['size']]}")
            self.parts.append(f'<span style="{html.escape(";".join(style_bits), quote=True)}">' if style_bits else "<span>")
            self._open_tags.append("span")
            return
        if tag not in _ALLOWED_TAGS:
            return
        style = _safe_style(attr_map.get("style", "")) if tag == "span" else ""
        if tag == "table":
            style = _safe_table_style(attr_map.get("style", ""))
        if tag == "br":
            self.parts.append("<br>")
            self.text_parts.append("\n")
            return
        attrs_out = f' style="{html.escape(style, quote=True)}"' if style else ""
        if tag in {"td", "th"}:
            for name in ("colspan", "rowspan"):
                value = attr_map.get(name, "")
                if value.isdigit() and 1 <= int(value) <= 20:
                    attrs_out += f' {name}="{value}"'
        if attrs_out:
            self.parts.append(f"<{tag}{attrs_out}>")
        else:
            self.parts.append(f"<{tag}>")
        self._open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = "span" if tag.lower() == "font" else tag.lower()
        if tag not in self._open_tags:
            return
        # 只关闭与输入对应的最近一个同名标签，容忍粘贴内容的交叉嵌套。
        index = len(self._open_tags) - 1 - self._open_tags[::-1].index(tag)
        for open_tag in reversed(self._open_tags[index:]):
            self.parts.append(f"</{open_tag}>")
            if open_tag in _BLOCK_TAGS:
                self.text_parts.append("\n")
            elif open_tag in {"td", "th"}:
                self.text_parts.append(" | ")
        del self._open_tags[index:]

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))
        self.text_parts.append(data)

    def result(self) -> str:
        for tag in reversed(self._open_tags):
            self.parts.append(f"</{tag}>")
            if tag in _BLOCK_TAGS:
                self.text_parts.append("\n")
        return "".join(self.parts)

    def plain_text(self) -> str:
        text = "".join(self.text_parts).replace("\xa0", " ")
        lines = [
            re.sub(r"\s*\|\s*$", "", re.sub(r"[ \t\r\f\v]+", " ", line).strip())
            for line in text.splitlines()
        ]
        return "\n".join(line for line in lines if line).strip()


def sanitize_rich_html(value: str) -> str:
    """返回仅含本地编辑器允许标签和样式的 HTML。"""
    parser = _RichTextSanitizer()
    parser.feed(str(value or ""))
    parser.close()
    return parser.result()


def rich_html_to_plain_text(value: str) -> str:
    """将富文本转换为用于检索、导出和交流的稳定纯文本投影。"""
    parser = _RichTextSanitizer()
    parser.feed(str(value or ""))
    parser.close()
    return parser.plain_text()
