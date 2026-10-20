"""ייצוא הסכם לקובץ Word עם כיווניות RTL תקינה."""
import io

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

FONT = "David"


def _rtl(p, size=12, bold=False):
    # Word דוחה קובץ שבו אלמנטים לא בסדר של הסכמה, ולכן משתמשים ב-get_or_add/addnext ולא ב-append
    p._p.get_or_add_pPr().insert(0, OxmlElement("w:bidi"))  # כיוון פסקה מימין לשמאל (אין pStyle לפניו)
    for r in p.runs:
        r.font.size, r.font.bold, r.font.cs_bold = Pt(size), bold, bold
        rpr = r._r.get_or_add_rPr()
        rpr.get_or_add_rFonts().set(qn("w:cs"), FONT)
        rpr.get_or_add_rtl()
        sz_cs = OxmlElement("w:szCs")  # גודל הגופן לעברית (complex script)
        sz_cs.set(qn("w:val"), str(size * 2))
        rpr.get_or_add_sz().addnext(sz_cs)
    return p


def build(title, body):
    d = docx.Document()
    d.sections[0]._sectPr.find(qn("w:docGrid")).addprevious(OxmlElement("w:bidi"))  # עמוד RTL
    t = d.add_paragraph(title)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _rtl(t, size=16, bold=True)
    for line in body.splitlines():
        _rtl(d.add_paragraph(line))
    d.add_paragraph()
    _rtl(d.add_paragraph("טיוטה שנוצרה על ידי AI. יש לבדוק אותה לפני שימוש."), size=10)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    data = build("הסכם בדיקה", "1. סעיף ראשון בעברית with English 123.\n1.1 תת-סעיף.")
    xml = docx.Document(io.BytesIO(data)).element.xml
    assert xml.count("<w:bidi/>") >= 3 and "<w:rtl/>" in xml and "הסכם בדיקה" in xml
    print("docx self-check ok")
