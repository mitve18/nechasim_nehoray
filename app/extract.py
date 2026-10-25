"""חילוץ טקסט מהסכמים: שכבת טקסט כשיש, OCR עם Tesseract לסריקות, וניקוי לפני אינדוקס.

OCR בשתי שכבות: Tesseract על כל עמוד סרוק (אחרי זיהוי כיוון וחידוד ניגודיות), ועמוד שקיבל ציון נמוך
נקרא שוב ב-OCR של Google Drive (fallback, חינם). הסנכרון מעביר את ה-fallback רק לקובץ חדש או ששונה.

python -m app.extract            טבלת סיכום לכל הקבצים בתיקייה
python -m app.extract <fileId>   הטקסט המלא של קובץ אחד, כמו בסנכרון (כולל OCR של Drive לעמודים קשים)
python -m app.extract --ocr <file.pdf>   OCR מקומי לקובץ, בלי Drive (לבדיקת ההתקנה של Tesseract)
"""
import io
import logging
import os
import re
import shutil
import textwrap
from collections import Counter

import pillow_heif
from pypdf import PdfReader

pillow_heif.register_heif_opener()  # תמונות HEIC/HEIF מאייפון נפתחות כמו כל תמונה

log = logging.getLogger(__name__)
MIN_CHARS_PER_PAGE = 50  # פחות מזה בממוצע = סריקה בלי שכבת טקסט
DOCX_MIN_TEXT = 200  # מסמך Word עם פחות טקסט מזה ועם תמונות = עמודים סרוקים שהודבקו, ועוברים OCR
OCR_LANG = "heb+eng"
OCR_DPI = 300
# ponytail: סף משוער. עמוד עברי נקי מקבל בדרך כלל 85+; לכייל לפי הציונים במאגר אם Drive נקרא מעט מדי או יותר מדי
FALLBACK_BELOW = 75  # עמוד עם ציון Tesseract נמוך מזה נקרא שוב ב-fallback
MIN_OSD_CONF = 2  # מתחת לזה זיהוי הכיוון של Tesseract לא אמין, והעמוד נשאר כמו שהוא
UPSCALE_BELOW = 1600  # תמונה צרה מזה (סריקה ב-150 DPI, צילום מסך) מוגדלת פי 2: באותיות קטנות Tesseract טועה יותר
WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff", "image/heic", "image/heif"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC = "application/vnd.google-apps.document"
DOC = "application/msword"  # Word 97-2003. Drive ממיר אותו לטקסט בהורדה (drive.download)


class Unsupported(ValueError):
    """סוג קובץ שאי אפשר לחלץ ממנו טקסט. בטבלת files הוא מסומן "לא נתמך" ולא "נכשל"."""


def supported(mime):
    """האם אפשר לחלץ טקסט מהסוג הזה. נבדק לפני ההורדה, כדי שסוג לא נתמך ייכשל עם סיבה ברורה."""
    return mime in ("application/pdf", DOCX, GDOC, DOC) or mime in IMAGE_TYPES or mime.startswith("text/")


def extract(f, data, fallback=None):
    """מחזיר (טקסט נקי, שיטה, איכות OCR). f הוא ה-dict מ-drive.list_files, data הם ה-bytes של הקובץ.
    איכות OCR: 0–100 כשהטקסט זוהה מתמונה, ו-None כשהוא נקרא ישירות מהקובץ.
    fallback: פונקציה תמונה ← טקסט, לעמודים ש-Tesseract קרא גרוע (ראו ocr)."""
    mime = f["mimeType"]
    if not supported(mime):
        # ponytail: DOC ישן/HEIC לא נתמכים; להוסיף המרה עם LibreOffice אם יופיעו במאגר
        raise Unsupported(f"סוג קובץ לא נתמך: {mime}")
    if mime == "application/pdf":
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        # ההחלטה לכל עמוד: נספח או עמוד חתימות סרוקים בתוך PDF דיגיטלי עוברים OCR, ושאר העמודים לא
        scanned = [i for i, p in enumerate(pages) if len(p.strip()) < MIN_CHARS_PER_PAGE]
        if not scanned:
            return clean(pages), "text", None
        ocr_pages, quality = ocr(_pdf_images(data, scanned), fallback)
        used = False
        for i, t in zip(scanned, ocr_pages):
            if len(t.strip()) > len(pages[i].strip()):  # עמוד שער דיגיטלי דליל נשאר עם הטקסט המקורי
                pages[i], used = t, True
        if len(scanned) == len(pages):  # סריקה מלאה: OCR שהחזיר מעט מדי = סריקה לא קריאה
            return _checked(clean(pages), len(pages)), "ocr", quality
        return (clean(pages), "ocr", quality) if used else (clean(pages), "text", None)
    if mime in IMAGE_TYPES:
        frames = _image_frames(data)  # TIFF סרוק יכול להכיל כמה עמודים
        text, quality = ocr(frames, fallback)
        return _checked(clean(text), len(frames)), "ocr", quality
    if mime == DOCX:
        import docx
        d = docx.Document(io.BytesIO(data))
        text = clean(["\n".join(p.text for p in d.paragraphs)])
        images = _docx_images(d) if len(text) < DOCX_MIN_TEXT else []
        if not images:
            return text, "docx", None
        # מסמך Word שבתוכו עמודים סרוקים כתמונות, וכמעט בלי טקסט: הטקסט מזוהה מהתמונות
        ocr_text, quality = ocr(images, fallback)
        return _checked(clean([text, *ocr_text], mark_pages=False), len(images)), "ocr", quality  # תמונות, לא עמודים
    return clean([data.decode("utf-8-sig")]), "text", None  # GDOC, DOC (כבר הומר לטקסט) או text/*


def _docx_images(d):
    """התמונות שבמסמך Word (למשל עמודים סרוקים שהודבקו), לפי הסדר במסמך."""
    from docx.oxml.ns import qn
    from PIL import Image
    images = []
    for blip in d.element.body.iter(qn("a:blip")):
        part = d.part.related_parts.get(blip.get(qn("r:embed")))
        try:
            images.append(Image.open(io.BytesIO(part.blob)).convert("RGB"))
        except Exception:  # ציור וקטורי (EMF/WMF) או תמונה מקושרת מבחוץ
            continue
    return images


def _image_frames(data):
    """עמודי התמונה, מסובבים לפי תג הסיבוב של המצלמה (EXIF). אייפון שומר תמונה עומדת כשוכבת + תג,
    ובלי הסיבוב ה-OCR קורא טקסט על הצד."""
    from PIL import Image, ImageOps, ImageSequence
    return [ImageOps.exif_transpose(frame).convert("RGB") for frame in ImageSequence.Iterator(Image.open(io.BytesIO(data)))]


def countable(mime):
    """לסוגים האלה יש מספר עמודים. ל-DOCX ול-Google Docs אין עמודים עד שמציגים אותם."""
    return mime == "application/pdf" or mime in IMAGE_TYPES


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


def _pdf_images(data, indexes):
    """העמודים שנבחרו מה-PDF כתמונות, אחד-אחד כדי לא להחזיק את כל הסריקה בזיכרון."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    for i in indexes:
        yield pdf[i].render(scale=OCR_DPI / 72).to_pil()


def _prepare(img):
    """לפני Tesseract: גווני אפור עם ניגודיות מלאה (רקע אפור של צילום או סריקה ישנה), הגדלה של תמונה קטנה,
    וסיבוב לפי זיהוי הכיוון של Tesseract (OSD), לעמוד שנסרק הפוך או על הצד."""
    import pytesseract
    from PIL import Image, ImageOps
    img = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    if img.width < UPSCALE_BELOW:
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        if osd["rotate"] and osd["orientation_conf"] >= MIN_OSD_CONF:
            img = img.rotate(-osd["rotate"], expand=True)  # rotate של PIL הוא נגד כיוון השעון
    except pytesseract.TesseractError:  # מעט מדי טקסט לזיהוי כיוון (עמוד ריק, חותמת בלבד)
        pass
    return img


def _score(tsv):
    """(סכום הביטחון המשוקלל, מספר האותיות) מפלט ה-tsv של Tesseract."""
    weighted, letters = 0.0, 0
    for row in tsv.splitlines()[1:]:  # עמודות: ... conf, text
        cols = row.split("\t")
        word = cols[-1].strip()
        if len(cols) == 12 and word and cols[10] != "-1":
            weighted += float(cols[10]) * len(word)
            letters += len(word)
    return weighted, letters


def ocr(images, fallback=None):
    """תמלול סריקה עם Tesseract. מחזיר (רשימת עמודים, ציון איכות 0–100).

    הציון הוא ממוצע הביטחון של Tesseract בכל מילה שזיהה, משוקלל לפי אורך המילה.
    הטקסט והציון מגיעים מאותה הרצה (txt ו-tsv), כך שהציון לא מאריך את ה-OCR.
    עמוד עם ציון מתחת ל-FALLBACK_BELOW (או בלי מילים בכלל) נקרא שוב ב-fallback, כשיש. הציון נשאר של Tesseract:
    הוא מתאר כמה הסריקה קשה, ומסמך עם ציון נמוך עדיין כדאי לבדוק מול המקור.
    """
    import pytesseract
    # ב-Linux tesseract ב-PATH; ב-Windows ה-installer לא מוסיף אותו ל-PATH, ולכן נופלים לנתיב ברירת המחדל
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract") or WINDOWS_TESSERACT
    pytesseract.pytesseract.tesseract_cmd = cmd
    pages, weighted, letters = [], 0.0, 0
    for img in images:
        img = _prepare(img)
        text, tsv = pytesseract.run_and_get_multiple_output(img, extensions=["txt", "tsv"], lang=OCR_LANG)
        w, n = _score(tsv)
        weighted, letters = weighted + w, letters + n
        if fallback and (not n or w / n < FALLBACK_BELOW):
            try:
                text = fallback(img) or text
            except Exception as e:  # Drive לא זמין: נשארים עם Tesseract, והסנכרון ממשיך
                log.warning(f"OCR חלופי נכשל, נשאר Tesseract: {e!r}")
        pages.append(text)
    return pages, round(weighted / letters) if letters else 0


PAGE_MARK = re.compile(r"\n*\[עמוד \d+\]\n*")  # תחילת עמוד בטקסט השמור: ממנו הציטוטים בתשובות לוקחים מספר עמוד
PAGE_NUM = re.compile(r"^\s*[-–]?\s*(עמוד|page)?\s*\d+\s*((מתוך|of|/)\s*\d+)?\s*[-–]?\s*$", re.I)
BLOCK_START = re.compile(r"^\s*(\d+(\.\d+)*[.)]|[א-ת][.)]|[-•●▪])\s")
SENTENCE_END = (".", ":", ";", "!", "?", ")", '"')


def clean(pages, mark_pages=True):
    """מסיר כותרות עליונות/תחתונות חוזרות ומספרי עמודים, ומאחד שבירות שורה באמצע משפט."""
    pages = [[l.strip() for l in p.splitlines()] for p in pages]
    repeated = set()
    if len(pages) >= 3:
        counts = Counter(l for p in pages for l in set(p) if l)
        repeated = {l for l, n in counts.items() if n >= len(pages) * 0.6}
    paras = []
    for n, p in enumerate(pages, 1):
        if mark_pages and len(pages) > 1:  # Word וטקסט הם "עמוד" אחד, בלי עמודים קבועים. השורה הריקה מונעת מיזוג לתוך הסימון
            paras += ["", f"[עמוד {n}]", ""]
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


def strip_pages(text):
    """הטקסט בלי סימוני העמודים, לדוגמאות שהמודל מנסח מהן (שלא יעתיק [עמוד 3] לטיוטה)."""
    return PAGE_MARK.sub("\n\n", text).strip()


# ponytail: גודל בתווים ולא בטוקנים. בעברית בערך 3 תווים לטוקן, כלומר קטעים של עד כ-700 טוקנים
CHUNK_MAX = 2000
CHUNK_MIN = 400  # אחרי סעיף קצר מזה, הסעיף הבא מצטרף אליו: "5. סודיות" לבד לא אומר כלום לחיפוש
SECTION_START = re.compile(r"^\d{1,3}\.\s+\S")  # סעיף ראשי ("7. "), לא תת-סעיף ("7.2"): הוא נשאר בתוך הסעיף שלו
PAGE_LINE = re.compile(r"\[עמוד (\d+)\]")


def chunk(text):
    """הטקסט השמור (אחרי clean) כקטעים לחיפוש: [{section, page, text}].
    גבול בין קטעים בתחילת כל סעיף ראשי. סעיף ארוך מ-CHUNK_MAX מתפצל בגבול פסקה, והפסקה האחרונה חוזרת
    בתחילת הקטע הבא (חפיפה), כדי שמשפט שנחתך בגבול יימצא. page: העמוד שבו הקטע מתחיל (null בלי סימוני עמודים).
    section: השורה הראשונה של הסעיף, כמו "7. שיפוי". במסמך בלי מספור כל הקטעים תחת השורה הראשונה במסמך."""
    sections, page = [], None
    for line in text.split("\n"):
        line = line.strip()
        if m := PAGE_LINE.fullmatch(line):
            page = int(m[1])
            continue
        if not line:
            continue
        if not sections or (SECTION_START.match(line) and sum(len(l) for _, l in sections[-1]["lines"]) >= CHUNK_MIN):
            sections.append({"section": line[:80], "lines": []})
        # פסקה ארוכה במיוחד (Word, OCR בלי שבירות שורה) נחתכת ברווחים, כדי שכל קטע יישאר עד CHUNK_MAX
        sections[-1]["lines"] += [(page, l) for l in textwrap.wrap(line, CHUNK_MAX // 2)]
    chunks = []
    for s in sections:
        piece = []
        for pg, line in s["lines"]:
            if piece and sum(len(l) + 1 for _, l in piece) + len(line) > CHUNK_MAX:
                chunks.append((s["section"], piece))
                piece = piece[-1:] if len(piece[-1][1]) + len(line) < CHUNK_MAX else []
            piece.append((pg, line))
        chunks.append((s["section"], piece))
    return [{"section": s, "page": p[0][0], "text": "\n".join(l for _, l in p)} for s, p in chunks]


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
        print(extract(f, drive.download(svc, f), fallback=lambda img: drive.ocr_image(svc, img))[0])
        sys.exit()
    for f in files:
        try:
            text, method, _ = extract(f, drive.download(svc, f))
            sample = next((l for l in text.splitlines() if len(l) > 30), "")[:70]
            print(f"{method:5} | {len(text):6} | {f['name'][:45]} | {sample}")
        except Exception as e:
            print(f"ERROR | {f['name'][:45]} | {e}")
