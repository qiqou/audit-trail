"""固定模板审计问题确认单的 OOXML 写入器。"""

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


def write_confirmation_docx(path: Path, issue: dict, unit_name: str) -> None:
    """写入标准 DOCX 文件包；版式语义不依赖本机 Word。"""
    def paragraph(text: object, *, bold: bool = False) -> str:
        content = escape(str(text or "—"))
        style = "<w:b/>" if bold else ""
        return f"<w:p><w:r><w:rPr>{style}<w:rFonts w:eastAsia=\"Microsoft YaHei\"/></w:rPr><w:t xml:space=\"preserve\">{content}</w:t></w:r></w:p>"

    def row(label: str, value: object) -> str:
        return f"<w:tr><w:tc>{paragraph(label, bold=True)}</w:tc><w:tc>{paragraph(value)}</w:tc></w:tr>"

    fields = (("被审计单位", unit_name), ("问题编号", issue.get("issue_code") or issue.get("seq")), ("所属版块", issue.get("department")), ("问题分类", issue.get("category")), ("问题金额", " ".join(str(issue.get(key) or "") for key in ("amount", "currency", "amount_unit")).strip()), ("编制人 / 审核人", f"{issue.get('author') or '—'} / {issue.get('reviewer') or '—'}"))
    body = [paragraph("审计问题确认单", bold=True), "<w:tbl><w:tblPr><w:tblBorders><w:top w:val=\"single\"/><w:left w:val=\"single\"/><w:bottom w:val=\"single\"/><w:right w:val=\"single\"/><w:insideH w:val=\"single\"/><w:insideV w:val=\"single\"/></w:tblBorders></w:tblPr>"]
    body.extend(row(label, value) for label, value in fields)
    body.append("</w:tbl>")
    for label, key in (("问题定性", "defect_type"), ("问题描述", "defect_desc"), ("制度依据", "regulation_basis"), ("审计建议", "suggestion")):
        body.extend((paragraph(label, bold=True), paragraph(issue.get(key))))
    document = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body>" + "".join(body) + "<w:sectPr/></w:body></w:document>"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/></Types>")
        archive.writestr("_rels/.rels", "<?xml version=\"1.0\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/></Relationships>")
        archive.writestr("word/document.xml", document)
