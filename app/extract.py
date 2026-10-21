"""חילוץ טקסט מהסכמים: שכבת טקסט כשיש, OCR עם Tesseract לסריקות, וניקוי לפני אינדוקס.

python -m app.extract            טבלת סיכום לכל הקבצים בתיקייה
python -m app.extract <fileId>   הטקסט המלא של קובץ אחד
python -m app.extract --ocr <file.pdf>   OCR מקומי לקובץ, בלי Drive (לבדיקת ההתקנה של Tesseract)
"""
import io
import os
import re
import shutil
from collections import Counter

from pypdf import PdfReader

MIN_CHARS_PER_PAGE = 50  # פחות מזה בממוצע = סריקה בלי שכבת טקסט
OCR_LANG = "heb+eng"
OCR_DPI = 300
WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC = "application/vnd.google-apps.document"


class Unsupported(ValueError):
    """סוג קובץ שאי אפשר לחלץ ממנו טקסט. בטבלת files הוא מסומן "לא נתמך" ולא "נכשל"."""


def supported(mime):
    """האם אפשר לחלץ טקסט מהסוג הזה. נבדק לפני ההורדה, כדי שסוג לא נתמך ייכשל עם סיבה ברורה."""
    return mime in ("application/pdf", DOCX, GDOC) or mime in IMAGE_TYPES or mime.startswith("text/")


def extract(f, data):
    """מחזיר (טקסט נקי, שיטה). f הוא ה-dict מ-drive.list_files, data הם ה-bytes של הקובץ."""
    mime = f["mimeType"]
    if not supported(mime):
        # ponytail: DOC ישן/HEIC לא נתמכים; להוסיף המרה עם LibreOffice אם יופיעו במאגר
        raise Unsupported(f"סוג קובץ לא נתמך: {mime}")
    if mime == "application/pdf":
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        # ponytail: החלטה לכל הקובץ לפי ממוצע; עמודים סרוקים בודדים בתוך PDF טקסטואלי לא יעברו OCR
        if sum(len(p.strip()) for p in pages) >= MIN_CHARS_PER_PAGE * len(pages):
            return clean(pages), "text"
        return _checked(clean(ocr(_pdf_images(data))), len(pages)), "ocr"
    if mime in IMAGE_TYPES:
        frames = _image_frames(data)  # TIFF סרוק יכול להכיל כמה עמודים
        return _checked(clean(ocr(frames)), len(frames)), "ocr"
    if mime == DOCX:
        import docx
        return clean(["\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)]), "docx"
    return clean([data.decode("utf-8-sig")]), "text"  # GDOC או text/*


def _image_frames(data):
    from PIL import Image, ImageSequence
    return [frame.convert("RGB") for frame in ImageSequence.Iterator(Image.open(io.BytesIO(data)))]


def page_count(f, data):
    """מספר העמודים האמיתי, ל-PDF ולתמונות. לסוגים אחרים None."""
    if f["mimeType"] == "application/pdf":
        return len(PdfReader(io.BytesIO(data)).pages)
    if f["mimeType"] in IMAGE_TYPES:
        from PIL import Image
        return getattr(Image.open(io.BytesIO(data)), "n_frames", 1)
    return None


def _checked(text, pages):
    """תמלול קצר מדי ביחס למספר העמודים = OCR נכשל (סריקה ריקה/לא קריאה). לא נכניס אותו למאגר."""
    if len(text) < MIN_CHARS_PER_PAGE * pages:
        raise RuntimeError(f"OCR החזיר {len(text)} תווים ל-{pages} עמודים: {text[:80]!r}")
    return text


def _pdf_images(data):
    """עמודי ה-PDF כתמונות, אחד-אחד כדי לא להחזיק את כל הסריקה בזיכרון."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    for page in pdf:
        yield page.render(scale=OCR_DPI / 72).to_pil()


def ocr(images):
    """תמלול סריקה עם Tesseract. מחזיר רשימת עמודים."""
    import pytesseract
    # ב-Linux tesseract ב-PATH; ב-Windows ה-installer לא מוסיף אותו ל-PATH, ולכן נופלים לנתיב ברירת המחדל
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract") or WINDOWS_TESSERACT
    pytesseract.pytesseract.tesseract_cmd = cmd
    return [pytesseract.image_to_string(img, lang=OCR_LANG) for img in images]


PAGE_NUM = re.compile(r"^\s*[-–]?\s*(עמוד|page)?\s*\d+\s*((מתוך|of|/)\s*\d+)?\s*[-–]?\s*$", re.I)
BLOCK_START = re.compile(r"^\s*(\d+(\.\d+)*[.)]|[א-ת][.)]|[-•●▪])\s")
SENTENCE_END = (".", ":", ";", "!", "?", ")", '"')


def clean(pages):
    """מסיר כותרות עליונות/תחתונות חוזרות ומספרי עמודים, ומאחד שבירות שורה באמצע משפט."""
    pages = [[l.strip() for l in p.splitlines()] for p in pages]
    repeated = set()
    if len(pages) >= 3:
        counts = Counter(l for p in pages for l in set(p) if l)
        repeated = {l for l, n in counts.items() if n >= len(pages) * 0.6}
    paras = []
    for p in pages:
        for line in p:
            if not line:
                paras.append("")
            elif line in repeated or PAGE_NUM.match(line):
                continue
            elif paras and paras[-1] and not paras[-1].endswith(SENTENCE_END) and not BLOCK_START.match(line):
                paras[-1] += " " + line
            else:
                paras.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paras)).strip()


def _demo():
    pages = ["כותרת החברה\nעמוד 1\nהצדדים מסכימים\nכי השכירות תחל.\n1. סעיף ראשון.", "כותרת החברה\n2\nהמשך", "כותרת החברה\n- 3 -\nסוף."]
    out = clean(pages)
    assert "כותרת החברה" not in out and "עמוד 1" not in out and "- 3 -" not in out, out
    assert "הצדדים מסכימים כי השכירות תחל." in out, out
    assert "\n1. סעיף ראשון." in out, out


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    _demo()
    if sys.argv[1:2] == ["--ocr"]:
        print(extract({"mimeType": "application/pdf"}, open(sys.argv[2], "rb").read())[0])
        sys.exit()
    from app import drive
    svc = drive.service()
    files = drive.list_files(svc)
    if len(sys.argv) > 1:
        f = next(f for f in files if f["id"] == sys.argv[1])
        print(extract(f, drive.download(svc, f))[0])
        sys.exit()
    for f in files:
        try:
            text, method = extract(f, drive.download(svc, f))
            sample = next((l for l in text.splitlines() if len(l) > 30), "")[:70]
            print(f"{method:5} | {len(text):6} | {f['name'][:45]} | {sample}")
        except Exception as e:
            print(f"ERROR | {f['name'][:45]} | {e}")
