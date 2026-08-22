"""确认单 DOCX 写入器：标准 OOXML 包、字段来自底稿数据且转义安全。

v1.3 下线确认单 DOCX（2026-08-22，移入 v1.4 候选池），API 路由已移除；
此处直接测试数据层写入器，供 v1.4 恢复入口时复用。
"""

from zipfile import ZipFile

from infra.exporters.confirmation_docx import write_confirmation_docx


def test_write_confirmation_docx_produces_valid_ooxml_package(tmp_path):
    issue = {
        "issue_code": "A-001",
        "seq": 1,
        "department": "财务",
        "category": "收入确认",
        "defect_type": "收入确认不完整",
        "defect_desc": "描述内容",
        "regulation_basis": "制度依据",
        "suggestion": "整改建议",
        "amount": "1,234.50",
        "currency": "CNY",
        "amount_unit": "元",
        "author": "张三",
        "reviewer": "李四",
    }
    target = tmp_path / "确认单.docx"
    write_confirmation_docx(target, issue, "甲单位")

    with ZipFile(target) as archive:
        names = archive.namelist()
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}.issubset(names)
        document = archive.read("word/document.xml").decode("utf-8")
        # 字段来自底稿数据
        assert "甲单位" in document
        assert "A-001" in document
        assert "收入确认不完整" in document
        assert "描述内容" in document
        # XML 转义：内容含特殊字符不会破坏文档结构
        assert "<w:document" in document


def test_write_confirmation_docx_escapes_xml_special_characters(tmp_path):
    issue = {
        "defect_type": "含 <b>标签</b> 与 & 符号",
        "defect_desc": "金额 <100 且 >50，含 & 字符",
        "regulation_basis": "",
        "suggestion": "",
    }
    target = tmp_path / "escape.docx"
    write_confirmation_docx(target, issue, "单位&名称")

    with ZipFile(target) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "&lt;b&gt;" in document
    assert "&amp;" in document
    # 原始标签不应出现在 XML 中（会破坏结构）
    assert "<b>标签</b>" not in document
