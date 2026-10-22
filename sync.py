"""סנכרון מאגר ההסכמים מ-Drive למאגר הווקטורי.

python sync.py          סנכרון: קבצים חדשים/ששונו (לפי modifiedTime) או שסומנו "נסה שוב", הסרת קבצים שנמחקו,
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


def sync():
    stats = {"started_at": datetime.now(timezone.utc), "added": 0, "updated": 0, "removed": 0, "ocr": 0, "errors": [],
             "unsupported": 0, "files": {"added": [], "updated": [], "backfilled": []},
             "pages_counted": 0, "ocr_scored": 0, "fill_failed": [],
             "folder": os.environ["DRIVE_FOLDER_ID"], "seen": []}
    svc = drive.service()
    files = drive.list_files(svc, stats["folder"])
    with store.connect() as conn:
        store.init(conn)
        known = store.indexed(conn)
        retry = store.retry_ids(conn)

        gone = set(known) - {f["id"] for f in files}
        if gone:
            store.delete(conn, gone)
            stats["removed"] = len(gone)

        # כל קובץ שנמצא מקבל סטטוס אחד, כך שאין קובץ שנדלג בשקט
        rows = []
        for f in files:
            status, reason, attempted = UNCHANGED, "", False
            if known.get(f["id"]) != f["modifiedTime"] or f["id"] in retry:
                attempted = True
                try:
                    if f.get("error"):  # קיצור דרך שאין גישה ליעד שלו
                        raise ValueError(f["error"])
                    if not extract.supported(f["mimeType"]):
                        raise extract.Unsupported(f"סוג קובץ לא נתמך: {f['mimeType']}")
                    data = drive.download(svc, f)
                    text, method, quality = extract.extract(f, data)
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
        store.save_files(conn, rows)
        store.prune_files(conn, [f["id"] for f in files])

        fill_missing(conn, svc, files, stats)

        backfill(conn, stats)
        store.log_run(conn, stats)
    stats["in_db"] = len(known) - stats["removed"] + stats["added"]
    print(f"\nבתיקייה {len(files)} קבצים, במאגר {stats['in_db']}. נוספו {stats['added']}, עודכנו {stats['updated']}, "
          f"הוסרו {stats['removed']}, עברו OCR {stats['ocr']}, לא נתמכו {stats['unsupported']}, "
          f"נכשלו {len(stats['errors'])}, הושלמו נתונים מובנים ל-{len(stats['files']['backfilled'])}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):  # מוצג בעמוד הריצה ב-GitHub Actions
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary(stats))
    return stats


def fill_missing(conn, svc, files, stats):
    """משלים למסמכים קיימים מה שעוד לא נשמר: מספר עמודים, וציון איכות OCR למסמך סרוק.
    הורדה אחת לכל מסמך, בלי מודל. ציון ה-OCR דורש להריץ OCR שוב, ולכן הוא לוקח זמן ב-Actions בלבד.
    קורה פעם אחת לכל מסמך. כישלון לא מכשיל את הריצה, והמסמך יושלם בריצה הבאה."""
    no_pages, no_quality = store.missing_pages(conn), store.missing_quality(conn)
    for f in files:
        pages = f["id"] in no_pages and extract.countable(f["mimeType"])
        quality = f["id"] in no_quality
        if not (pages or quality):
            continue
        try:
            data = drive.download(svc, f)
            if pages:
                store.save_pages(conn, f["id"], extract.page_count(f, data))
                stats["pages_counted"] += 1
            if quality:
                store.save_quality(conn, f["id"], extract.extract(f, data)[2])
                stats["ocr_scored"] += 1
        except Exception as e:
            stats["fill_failed"].append(f"{f['name']}: {str(e)[:200]}")
            print(f"השלמה נכשלה  {f['name']}  ({e})")
            continue
        print(f"הושלם  {f['name']}" + ("  · עמודים" if pages else "") + ("  · ציון OCR" if quality else ""))


def backfill(conn, stats):
    """משלים נתונים מובנים למסמכים שאין להם (או שהגרסה שלהם ישנה), מהטקסט ששמור במסד:
    בלי הורדה מ-Drive ובלי OCR. מסמך שנכשל לא עוצר את הסנכרון, ונבחר שוב בריצה הבאה."""
    for doc in store.pending_metadata(conn):
        try:
            _, meta = store.analyze(doc["name"], doc["text"])
            store.save_metadata(conn, doc["file_id"], meta)
        except Exception as e:
            stats["errors"].append({"file": doc["name"], "error": f"השלמת נתונים מובנים: {str(e)[:250]}"})
            print(f"נכשל     השלמת נתונים מובנים  {doc['name']}  ({e})")
            continue
        stats["files"]["backfilled"].append(doc["name"])
        print(f"הושלם    נתונים מובנים  {doc['name']}")


def summary(stats):
    """סיכום הריצה ב-Markdown: טבלת מספרים, שמות הקבצים שהשתנו, ובלוק מתקפל עם כל הקבצים שנמצאו."""
    def esc(s):
        return s.replace("|", "\\|").replace("\n", " ")

    backfilled = stats["files"]["backfilled"]
    lines = ["## סנכרון המאגר", "", "| נוספו | עודכנו | הוסרו | לא נתמכו | נכשלו |", "|---|---|---|---|---|",
             f"| {stats['added']} | {stats['updated']} | {stats['removed']} | {stats['unsupported']} "
             f"| {len(stats['errors'])} |", "",
             f"בתיקייה: {len(stats['seen'])} קבצים · במאגר אחרי הריצה: {stats['in_db']} מסמכים", "",
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
