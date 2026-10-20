"""חילוץ טקסט מהסכמים: שכבת טקסט כשיש, OCR דרך OpenAI לסריקות, וניקוי לפני אינדוקס.

python -m app.extract            טבלת סיכום לכל הקבצים בתיקייה
python -m app.extract <fileId>   הטקסט המלא של קובץ אחד
"""
import base64
import io
import re
from collections import Counter

from openai import OpenAI
from pypdf import PdfReader

MODEL = "gpt-5.5"
MIN_CHARS_PER_PAGE = 50  # פחות מזה בממוצע = סריקה בלי שכבת טקסט
PAGE_BREAK = "<<<PAGE>>>"
OCR_PROMPT = (
    "תמלל את המסמך הסרוק במדויק, מילה במילה, בשפת המקור (עברית, וכן אנגלית ומספרים כפי שהם). "
    "שמור על מספור סעיפים ועל מעברי פסקאות. אל תתרגם, אל תסכם ואל תוסיף הערות משלך. "
    f"בין עמוד לעמוד כתוב שורה שמכילה רק {PAGE_BREAK}. "
    "חתימות, חותמות ולוגואים: כתוב [חתימה] / [חותמת] במקומם. החזר את התמלול בלבד."
)
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC = "application/vnd.google-apps.document"

ocr_tokens = {"input": 0, "output": 0}  # מצטבר לאורך הריצה, לצורך דיווח עלות


def extract(f, data):
    """מחזיר (טקסט נקי, שיטה). f הוא ה-dict מ-drive.list_files, data הם ה-bytes של הקובץ."""
    mime = f["mimeType"]
    if mime == "application/pdf":
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        # ponytail: החלטה לכל הקובץ לפי ממוצע; עמודים סרוקים בודדים בתוך PDF טקסטואלי לא יעברו OCR
        if sum(len(p.strip()) for p in pages) >= MIN_CHARS_PER_PAGE * len(pages):
            return clean(pages), "text"
        part = {"type": "input_file", "filename": "scan.pdf", "file_data": _data_url(data, mime)}
        return _checked(clean(ocr(part)), len(pages)), "ocr"
    if mime in IMAGE_TYPES:
        return _checked(clean(ocr({"type": "input_image", "image_url": _data_url(data, mime)})), 1), "ocr"
    if mime == DOCX:
        import docx
        return clean(["\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)]), "docx"
    if mime == GDOC or mime.startswith("text/"):
        return clean([data.decode("utf-8-sig")]), "text"
    # ponytail: TIFF/HEIC/DOC ישן לא נתמכים; להוסיף המרה עם Pillow/LibreOffice אם יופיעו במאגר
    raise ValueError(f"סוג קובץ לא נתמך: {mime}")


def _checked(text, pages):
    """תמלול קצר מדי ביחס למספר העמודים = המודל סירב או נכשל. לא נכניס אותו למאגר."""
    if len(text) < MIN_CHARS_PER_PAGE * pages:
        raise RuntimeError(f"OCR החזיר {len(text)} תווים ל-{pages} עמודים: {text[:80]!r}")
    return text


def _data_url(data, mime):
    return f"data:{mime};base64,{base64.standard_b64encode(data).decode()}"


def ocr(part):
    """תמלול סריקה עם OpenAI. מחזיר רשימת עמודים."""
    resp = OpenAI().responses.create(
        model=MODEL,
        reasoning={"effort": "low"},  # תמלול, לא הסקה
        max_output_tokens=64000,
        input=[{"role": "user", "content": [part, {"type": "input_text", "text": OCR_PROMPT}]}],
    )
    if resp.status != "completed":
        raise RuntimeError(f"OCR נכשל: status={resp.status} {resp.incomplete_details}")
    ocr_tokens["input"] += resp.usage.input_tokens
    ocr_tokens["output"] += resp.usage.output_tokens
    return resp.output_text.split(PAGE_BREAK)


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
    from app import drive

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    _demo()
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
    print(f"\nטוקנים ל-OCR בריצה זו: input={ocr_tokens['input']:,} output={ocr_tokens['output']:,}")
