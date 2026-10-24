"""סנכרון מאגר ההסכמים מ-Drive למאגר הווקטורי.

python sync.py          סנכרון: קבצים חדשים/ששונו (לפי modifiedTime, ובלי שינוי שם בלבד: md5 זהה) או שסומנו "נסה שוב", הסרת קבצים שנמחקו,
                        והשלמת נתונים מובנים למסמכים קיימים שאין להם (backfill)
python sync.py --list   רשימת הקבצים בתיקייה בלבד
"""
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")  # קונסולת Windows לא מציגה עברית בלי זה

from app import drive, extract, store  # noqa: E402

ADDED, UPDATED, UNCHANGED, FAILED, UNSUPPORTED = "נוסף", "עודכן", "ללא שינוי", "נכשל", "לא נתמך"
FILE_STATUS = {ADDED: "indexed", UPDATED: "indexed", UNCHANGED: "indexed", FAILED: "failed",
               UNSUPPORTED: "unsupported"}  # הסטטוס בטבלת files
MAX_REMOVE = 0.3  # מעל החלק הזה מהמאגר לא מוחקים: כנראה תקלה ב-Drive (תיקייה הוזזה, הרשאה בוטלה), לא מחיקה אמיתית


def sync():
    stats = {"started_at": datetime.now(timezone.utc), "added": 0, "updated": 0, "removed": 0, "ocr": 0, "errors": [],
             "unsupported": 0, "files": {"added": [], "updated": [], "backfilled": []},
             "pages_counted": 0, "ocr_scored": 0, "pages_marked": 0, "fill_failed": [],
             "folder": os.environ["DRIVE_FOLDER_ID"], "seen": []}
    svc = drive.service()
    files = drive.list_files(svc, stats["folder"])
    with store.connect() as conn:
        store.init(conn)
        known = store.indexed(conn)
        retry = store.retry_ids(conn)

        gone = set(known) - {f["id"] for f in files}
        if (known and not files) or len(gone) > MAX_REMOVE * len(known):
            stats["aborted"] = (f"הסנכרון נעצר ולא נמחק דבר: מ-Drive התקבלו {len(files)} קבצים, "
                                f"והסנכרון היה מוחק {len(gone)} מתוך {len(known)} מסמכים במאגר (מעל {MAX_REMOVE:.0%}). "
                                "בדוק את התיקייה ואת ההרשאות ב-Drive. אם המחיקה מכוונת, מחק בהדרגה.")
            stats["errors"].append({"file": "מחיקה גורפת", "error": stats["aborted"]})
        elif gone:
            store.delete(conn, gone)
            stats["removed"] = len(gone)

        # כל קובץ שנמצא מקבל סטטוס אחד, כך שאין קובץ שנדלג בשקט. בריצה שנעצרה לא נוגעים בכלום
        rows, md5s = [], store.checksums(conn)
        for f in [] if stats.get("aborted") else files:
            status, reason, attempted = UNCHANGED, "", False
            fid, md5 = f["id"], f.get("md5Checksum")
            # אותו תוכן (md5 זהה), או md5 שעוד לא נשמר למסמך שלא השתנה: רק שם ו-md5, בלי OCR ובלי מודל
            if fid in known and fid not in retry and md5 and (
                    md5s.get(fid) == md5 or (md5s.get(fid) is None and known[fid] == f["modifiedTime"])):
                if (known[fid], md5s.get(fid)) != (f["modifiedTime"], md5):
                    store.touch(conn, f)
            elif known.get(fid) != f["modifiedTime"] or fid in retry:
                attempted = True
                try:
                    if f.get("error"):  # קיצור דרך שאין גישה ליעד שלו
                        raise ValueError(f["error"])
                    if not extract.supported(f["mimeType"]):
                        raise extract.Unsupported(f"סוג קובץ לא נתמך: {f['mimeType']}")
                    data = drive.download(svc, f)
                    # עמוד ש-Tesseract קרא גרוע נקרא שוב ב-OCR של Drive. רק כאן: מסמכים קיימים לא עוברים OCR מחדש
                    text, method, quality = extract.extract(f, data, fallback=lambda img: drive.ocr_image(svc, img))
                    store.upsert(conn, f, method, text, extract.page_count(f, data), quality)
                except extract.Unsupported as e:  # לא תקלה: מסומן "לא נתמך" ולא מכשיל את הריצה
                    status, reason = UNSUPPORTED, str(e)
                    stats["unsupported"] += 1
                except Exception as e:  # קובץ שנכשל לא נרשם במאגר, ולכן ינוסה שוב בריצה הבאה
                    status, reason = FAILED, str(e)[:300]
                    stats["errors"].append({"file": f["name"], "error": reason})
                else:
                    change = "updated" if f["id"] in known else "added"
                    status = UPDATED if change == "updated" else ADDED
                    stats[change] += 1
                    stats["files"][change].append(f["name"])
                    stats["ocr"] += method == "ocr"
            stats["seen"].append({"name": f["name"], "mime": f["mimeType"], "modified": f["modifiedTime"],
                                  "status": status, "reason": reason})
            rows.append({"file_id": f["id"], "name": f["name"], "mime_type": f["mimeType"],
                         "size_bytes": int(f["size"]) if f.get("size") else None,
                         "modified_time": f["modifiedTime"], "status": FILE_STATUS[status],
                         "error": reason or None, "attempted": attempted})
            print(f"{status:9}  {f['mimeType']:<45}  {f['modifiedTime']}  {f['name']}" + (f"  ({reason})" if reason else ""))
        if not stats.get("aborted"):
            store.save_files(conn, rows)
            store.prune_files(conn, [f["id"] for f in files])
            fill_missing(conn, svc, files, stats)
            backfill(conn, stats)
        store.log_run(conn, stats)
    stats["listed"] = len(files)
    stats["in_db"] = len(known) - stats["removed"] + stats["added"]
    if stats.get("aborted"):
        print(stats["aborted"])
    print(f"\nבתיקייה {len(files)} קבצים, במאגר {stats['in_db']}. נוספו {stats['added']}, עודכנו {stats['updated']}, "
          f"הוסרו {stats['removed']}, עברו OCR {stats['ocr']}, לא נתמכו {stats['unsupported']}, "
          f"נכשלו {len(stats['errors'])}, הושלמו נתונים מובנים ל-{len(stats['files']['backfilled'])}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):  # מוצג בעמוד הריצה ב-GitHub Actions
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary(stats))
    return stats


def fill_missing(conn, svc, files, stats):
    """משלים למסמכים קיימים מה שעוד לא נשמר: מספר עמודים, ציון איכות OCR למסמך סרוק, וסימוני עמודים בטקסט.
    הורדה אחת לכל מסמך, בלי מודל. ציון ה-OCR דורש להריץ OCR שוב, ולכן הוא לוקח זמן ב-Actions בלבד.
    קורה פעם אחת לכל מסמך. כישלון לא מכשיל את הריצה, והמסמך יושלם בריצה הבאה."""
    no_pages, no_quality, no_marks = store.missing_pages(conn), store.missing_quality(conn), store.missing_page_marks(conn)
    for f in files:
        pages = f["id"] in no_pages and extract.countable(f["mimeType"])
        quality = f["id"] in no_quality
        marks = f["id"] in no_marks and extract.countable(f["mimeType"])
        if not (pages or quality or marks):
            continue
        try:
            data = drive.download(svc, f)
            if pages:
                store.save_pages(conn, f["id"], extract.page_count(f, data))
                stats["pages_counted"] += 1
            if quality or marks:  # חילוץ אחד לשניהם: במסמך סרוק זה OCR מלא
                text, _, score = extract.extract(f, data)
                if quality:
                    store.save_quality(conn, f["id"], score)
                    stats["ocr_scored"] += 1
                if marks:
                    store.save_text(conn, f["id"], text)
                    stats["pages_marked"] += 1
        except Exception as e:
            stats["fill_failed"].append(f"{f['name']}: {str(e)[:200]}")
            print(f"השלמה נכשלה  {f['name']}  ({e})")
            continue
        print(f"הושלם  {f['name']}" + ("  · עמודים" if pages else "") + ("  · ציון OCR" if quality else "")
              + ("  · סימוני עמודים" if marks else ""))


def backfill(conn, stats):
    """משלים נתונים מובנים למסמכים שאין להם (או שהגרסה שלהם ישנה), מהטקסט ששמור במסד:
    בלי הורדה מ-Drive ובלי OCR. מסמך שנכשל לא עוצר את הסנכרון, ונבחר שוב בריצה הבאה."""
    for doc in store.pending_metadata(conn):
        usage = []
        try:
            _, meta = store.analyze(doc["name"], doc["text"], usage)
            store.save_metadata(conn, doc["file_id"], meta)
        except Exception as e:
            stats["errors"].append({"file": doc["name"], "error": f"השלמת נתונים מובנים: {str(e)[:250]}"})
            print(f"נכשל     השלמת נתונים מובנים  {doc['name']}  ({e})")
            continue
        finally:
            store.log_usage(conn, "sync", usage)
        stats["files"]["backfilled"].append(doc["name"])
        print(f"הושלם    נתונים מובנים  {doc['name']}")


def summary(stats):
    """סיכום הריצה ב-Markdown: טבלת מספרים, שמות הקבצים שהשתנו, ובלוק מתקפל עם כל הקבצים שנמצאו."""
    def esc(s):
        return s.replace("|", "\\|").replace("\n", " ")

    backfilled = stats["files"]["backfilled"]
    lines = ["## סנכרון המאגר", ""]
    if stats.get("aborted"):
        lines += ["> [!CAUTION]", f"> {stats['aborted']}", ""]
    lines += ["| נוספו | עודכנו | הוסרו | לא נתמכו | נכשלו |", "|---|---|---|---|---|",
              f"| {stats['added']} | {stats['updated']} | {stats['removed']} | {stats['unsupported']} "
              f"| {len(stats['errors'])} |", "",
              f"בתיקייה: {stats.get('listed', len(stats['seen']))} קבצים · במאגר אחרי הריצה: {stats['in_db']} מסמכים", "",
              f"תיקייה שנסרקה: `{stats['folder']}`"]
    if backfilled:
        lines += ["", f"**הושלמו נתונים מובנים ל-{len(backfilled)} מסמכים**", *(f"- {esc(n)}" for n in backfilled)]
    counted, scored, failed = stats.get("pages_counted", 0), stats.get("ocr_scored", 0), stats.get("fill_failed", [])
    if counted or scored or failed:  # השלמות למסמכים ותיקים: קורות פעם אחת לכל מסמך
        lines += ["", f"**נספרו עמודים ל-{counted} מסמכים · חושב ציון איכות OCR ל-{scored} מסמכים**"]
        if failed:
            lines += [f"לא הושלמו ({len(failed)}, ינוסו שוב בריצה הבאה):", *(f"- {esc(n)}" for n in failed)]
    for title, names in (("נוספו", stats["files"]["added"]), ("עודכנו", stats["files"]["updated"])):
        if names:
            lines += ["", f"### {title}", *(f"- {esc(n)}" for n in names)]
    if stats["errors"]:
        lines += ["", "### נכשלו", *(f"- {esc(e['file'])}: {esc(e['error'])}" for e in stats["errors"])]

    order = [FAILED, UNSUPPORTED, ADDED, UPDATED, UNCHANGED]  # מה שדורש תשומת לב קודם
    seen = sorted(stats["seen"], key=lambda s: (order.index(s["status"]), s["name"]))
    lines += ["", f"<details><summary>כל הקבצים שנמצאו בתיקייה ({len(seen)})</summary>", "",
              "| קובץ | סוג | תאריך שינוי (UTC) | סטטוס |", "|---|---|---|---|",
              *(f"| {esc(s['name'])} | {esc(s['mime'])} | {s['modified'][:16].replace('T', ' ')} | "
                f"{s['status']}{': ' + esc(s['reason']) if s['reason'] else ''} |" for s in seen),
              "", "</details>"]
    return "\n".join(lines) + "\n"


def main():
    if "--list" in sys.argv:
        files = drive.list_files(drive.service())
        for f in files:
            print(f"{f['modifiedTime']}  {f['mimeType']:<45}  {f['name']}")
        print(f"\nסה\"כ {len(files)} קבצים")
        return
    stats = sync()
    sys.exit(1 if stats["errors"] else 0)  # קובץ שנכשל מכשיל את ה-workflow, ו-GitHub שולח מייל. "לא נתמך" לא


if __name__ == "__main__":
    main()
