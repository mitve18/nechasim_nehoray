"""ייצוא הסכם לקובץ Word עם כיווניות RTL תקינה."""
import io

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

FONT = "David"
AI_NOTE = "טיוטה שנוצרה על ידי AI. יש לבדוק אותה לפני שימוש."
NOTE_COLOR = "9A3412"


def _rtl(p, size=12, bold=False):
    # Word דוחה קובץ שבו אלמנטים לא בסדר של הסכמה, ולכן משתמשים ב-get_or_add/addnext ולא ב-append
    ppr = p._p.get_or_add_pPr()
    ppr.insert(1 if ppr.pStyle is not None else 0, OxmlElement("w:bidi"))  # כיוון פסקה מימין לשמאל, אחרי pStyle אם יש
    for r in p.runs:
        r.font.size, r.font.bold, r.font.cs_bold = Pt(size), bold, bold
        rpr = r._r.get_or_add_rPr()
        rpr.get_or_add_rFonts().set(qn("w:cs"), FONT)
        rpr.get_or_add_rtl()
        sz_cs = OxmlElement("w:szCs")  # גודל הגופן לעברית (complex script)
        sz_cs.set(qn("w:val"), str(size * 2))
        rpr.get_or_add_sz().addnext(sz_cs)
    return p


def _note(p, size):
    """הערת ה-AI: ממורכזת, מודגשת ובצבע בולט."""
    p.add_run(AI_NOTE).font.color.rgb = RGBColor.from_string(NOTE_COLOR)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return _rtl(p, size=size, bold=True)


def _box(p):
    """מסגרת ורקע לפסקה. pBdr ו-shd חייבים לבוא לפני bidi לפי סדר הסכמה של pPr."""
    ppr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    for k, v in (("val", "clear"), ("color", "auto"), ("fill", "FFF4E0")):
        shd.set(qn(f"w:{k}"), v)
    bdr = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        e = OxmlElement(f"w:{side}")
        for k, v in (("val", "single"), ("sz", "12"), ("space", "6"), ("color", NOTE_COLOR)):
            e.set(qn(f"w:{k}"), v)
        bdr.append(e)
    ppr.insert(0, shd)
    ppr.insert(0, bdr)


def build(title, body):
    d = docx.Document()
    sec = d.sections[0]
    sec._sectPr.find(qn("w:docGrid")).addprevious(OxmlElement("w:bidi"))  # עמוד RTL
    _note(sec.header.paragraphs[0], size=9)  # בכל עמוד, כדי שההערה לא תימחק בטעות עם התיבה
    box = d.add_paragraph()
    box.paragraph_format.space_after = Pt(18)
    _box(_note(box, size=11))
    t = d.add_paragraph(title)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _rtl(t, size=16, bold=True)
    for line in body.splitlines():
        _rtl(d.add_paragraph(line))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    data = build("הסכם בדיקה", "1. סעיף ראשון בעברית with English 123.\n1.1 תת-סעיף.")
    doc = docx.Document(io.BytesIO(data))
    xml = doc.element.xml
    assert xml.count("<w:bidi/>") >= 4 and "<w:rtl/>" in xml and "הסכם בדיקה" in xml
    paras = [p.text for p in doc.paragraphs]
    assert paras[0] == AI_NOTE and paras[1] == "הסכם בדיקה", paras  # ההערה מעל הכותרת
    assert AI_NOTE not in paras[2:], paras  # ולא בסוף
    assert doc.sections[0].header.paragraphs[0].text == AI_NOTE
    ppr = doc.paragraphs[0]._p.pPr
    assert [c.tag.split("}")[1] for c in ppr][:3] == ["pBdr", "shd", "bidi"], ppr.xml
    print("docx self-check ok")
