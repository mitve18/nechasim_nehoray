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


def sync():
    stats = {"started_at": datetime.now(timezone.utc), "added": 0, "updated": 0, "removed": 0, "ocr": 0, "errors": [],
             "files": {"added": [], "updated": []}}
    svc = drive.service()
    files = drive.list_files(svc)
    with store.connect() as conn:
        store.init(conn)
        known = store.indexed(conn)

        gone = set(known) - {f["id"] for f in files}
        if gone:
            store.delete(conn, gone)
            stats["removed"] = len(gone)

        for f in files:
            if known.get(f["id"]) == f["modifiedTime"]:
                continue
            try:
                text, method = extract.extract(f, drive.download(svc, f))
                store.upsert(conn, f, method, text)
            except Exception as e:  # קובץ שנכשל לא נרשם במאגר, ולכן ינוסה שוב בריצה הבאה
                stats["errors"].append({"file": f["name"], "error": str(e)[:300]})
                print(f"נכשל: {f['name']}: {e}")
                continue
            change = "updated" if f["id"] in known else "added"
            stats[change] += 1
            stats["files"][change].append(f["name"])
            stats["ocr"] += method == "ocr"
            print(f"{method:5} {f['name']}")

        store.log_run(conn, stats)
    print(f"\nנוספו {stats['added']}, עודכנו {stats['updated']}, הוסרו {stats['removed']}, "
          f"עברו OCR {stats['ocr']}, נכשלו {len(stats['errors'])}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):  # מוצג בעמוד הריצה ב-GitHub Actions
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary(stats))
    return stats


def summary(stats):
    """סיכום הריצה ב-Markdown: טבלת מספרים, ואחריה שמות הקבצים שנוספו, עודכנו או נכשלו."""
    def esc(s):
        return s.replace("|", "\\|").replace("\n", " ")

    lines = ["## סנכרון המאגר", "", "| נוספו | עודכנו | הוסרו | נכשלו |", "|---|---|---|---|",
             f"| {stats['added']} | {stats['updated']} | {stats['removed']} | {len(stats['errors'])} |"]
    for title, names in (("נוספו", stats["files"]["added"]), ("עודכנו", stats["files"]["updated"])):
        if names:
            lines += ["", f"### {title}", *(f"- {esc(n)}" for n in names)]
    if stats["errors"]:
        lines += ["", "### נכשלו", *(f"- {esc(e['file'])}: {esc(e['error'])}" for e in stats["errors"])]
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
