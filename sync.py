"""סנכרון מאגר ההסכמים מ-Drive למאגר הווקטורי.

python sync.py          סנכרון: רק קבצים חדשים/ששונו (לפי modifiedTime), והסרת קבצים שנמחקו
python sync.py --list   רשימת הקבצים בתיקייה בלבד
"""
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")  # קונסולת Windows לא מציגה עברית בלי זה

from app import drive, extract, store  # noqa: E402

ADDED, UPDATED, UNCHANGED, FAILED = "נוסף", "עודכן", "ללא שינוי", "נכשל"


def sync():
    stats = {"started_at": datetime.now(timezone.utc), "added": 0, "updated": 0, "removed": 0, "ocr": 0, "errors": [],
             "files": {"added": [], "updated": []}, "folder": os.environ["DRIVE_FOLDER_ID"], "seen": []}
    svc = drive.service()
    files = drive.list_files(svc, stats["folder"])
    with store.connect() as conn:
        store.init(conn)
        known = store.indexed(conn)

        gone = set(known) - {f["id"] for f in files}
        if gone:
            store.delete(conn, gone)
            stats["removed"] = len(gone)

        # כל קובץ שנמצא מקבל סטטוס אחד מארבעה, כך שאין קובץ שנדלג בשקט
        for f in files:
            status, reason = UNCHANGED, ""
            if known.get(f["id"]) != f["modifiedTime"]:
                try:
                    if f.get("error"):  # קיצור דרך שאין גישה ליעד שלו
                        raise ValueError(f["error"])
                    if not extract.supported(f["mimeType"]):
                        raise ValueError(f"סוג קובץ לא נתמך: {f['mimeType']}")
                    text, method = extract.extract(f, drive.download(svc, f))
                    store.upsert(conn, f, method, text)
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
            print(f"{status:9}  {f['mimeType']:<45}  {f['modifiedTime']}  {f['name']}" + (f"  ({reason})" if reason else ""))

        store.log_run(conn, stats)
    stats["in_db"] = len(known) - stats["removed"] + stats["added"]
    print(f"\nבתיקייה {len(files)} קבצים, במאגר {stats['in_db']}. נוספו {stats['added']}, עודכנו {stats['updated']}, "
          f"הוסרו {stats['removed']}, עברו OCR {stats['ocr']}, נכשלו {len(stats['errors'])}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):  # מוצג בעמוד הריצה ב-GitHub Actions
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary(stats))
    return stats


def summary(stats):
    """סיכום הריצה ב-Markdown: טבלת מספרים, שמות הקבצים שהשתנו, ובלוק מתקפל עם כל הקבצים שנמצאו."""
    def esc(s):
        return s.replace("|", "\\|").replace("\n", " ")

    lines = ["## סנכרון המאגר", "", "| נוספו | עודכנו | הוסרו | נכשלו |", "|---|---|---|---|",
             f"| {stats['added']} | {stats['updated']} | {stats['removed']} | {len(stats['errors'])} |", "",
             f"בתיקייה: {len(stats['seen'])} קבצים · במאגר אחרי הריצה: {stats['in_db']} מסמכים", "",
             f"תיקייה שנסרקה: `{stats['folder']}`"]
    for title, names in (("נוספו", stats["files"]["added"]), ("עודכנו", stats["files"]["updated"])):
        if names:
            lines += ["", f"### {title}", *(f"- {esc(n)}" for n in names)]
    if stats["errors"]:
        lines += ["", "### נכשלו", *(f"- {esc(e['file'])}: {esc(e['error'])}" for e in stats["errors"])]

    order = [FAILED, ADDED, UPDATED, UNCHANGED]  # מה שדורש תשומת לב קודם
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
    sys.exit(1 if stats["errors"] else 0)  # קובץ שנכשל מכשיל את ה-workflow, ו-GitHub שולח מייל


if __name__ == "__main__":
    main()
