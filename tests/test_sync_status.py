"""בדיקות לסיכום הסנכרון, לרשימת הקבצים מ-Drive, ל-/api/status ול-/admin/sync, עם תחליפים ל-Drive ול-Neon.
בלי סודות ובלי רשת.

python tests/test_sync_status.py   (או pytest)
"""
import contextlib
import datetime
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

import sync  # noqa: E402
from app import drive, main, store  # noqa: E402

PDF, PNG = "application/pdf", "image/png"
SHORTCUT, FOLDER = drive.SHORTCUT_MIME, drive.FOLDER_MIME
FILES = [
    {"id": "new", "name": "הסכם חדש.png", "mimeType": PNG, "modifiedTime": "2026-10-21T08:12:33.000Z"},
    {"id": "old", "name": "הסכם | ישן.pdf", "mimeType": PDF, "modifiedTime": "2"},
    {"id": "same", "name": "לא השתנה.pdf", "mimeType": PDF, "modifiedTime": "1"},
    {"id": "bad", "name": "סריקה פגומה.pdf", "mimeType": PDF, "modifiedTime": "1"},
    {"id": "heic", "name": "צילום.heic", "mimeType": "image/heic", "modifiedTime": "1"},
    {"id": "sc", "name": "קיצור", "mimeType": SHORTCUT, "modifiedTime": "1",
     "error": "קיצור דרך לקובץ שאין אליו גישה (404)"},
]


@contextlib.contextmanager
def _fake_backends(logged, downloads, summary_path):
    """Drive ו-Neon מזויפים. mock.patch מחזיר את הפונקציות האמיתיות בסוף, כדי לא לפגוע בבדיקות אחרות."""
    def fake_extract(f, data):
        if f["id"] == "bad":
            raise RuntimeError("OCR נכשל")
        return "t", "ocr" if f["mimeType"] == PNG else "text"

    with mock.patch.multiple(sync.drive, service=lambda: None, list_files=lambda svc, folder: FILES,
                             download=lambda svc, f: downloads.append(f["id"]) or b""), \
            mock.patch.multiple(sync.extract, extract=fake_extract, page_count=lambda f, data: None), \
            mock.patch.multiple(store, connect=lambda: contextlib.nullcontext(), init=lambda conn: None,
                                indexed=lambda conn: {"old": "1", "same": "1", "gone": "1"},
                                retry_ids=lambda conn: set(), delete=lambda conn, ids: None,
                                upsert=lambda conn, f, method, text, pages=None: None,
                                save_files=lambda conn, rows: None, prune_files=lambda conn, ids: None,
                                pending_metadata=lambda conn: [], log_run=lambda conn, stats: logged.append(stats)), \
            mock.patch.dict(os.environ, {"DRIVE_FOLDER_ID": "folder-123", "GITHUB_STEP_SUMMARY": summary_path}), \
            mock.patch.object(sys, "argv", ["sync.py"]):
        yield


def test_sync_summary_and_exit_code():
    logged, downloads = [], []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "summary.md")
        with _fake_backends(logged, downloads, path):
            try:
                sync.main()
                raise AssertionError("קובץ שנכשל חייב להכשיל את הריצה")
            except SystemExit as e:
                assert e.code == 1
        md = Path(path).read_text(encoding="utf-8")

    assert "| 1 | 1 | 1 | 1 | 2 |" in md, md  # נוספו, עודכנו, הוסרו, לא נתמכו, נכשלו
    assert "בתיקייה: 6 קבצים · במאגר אחרי הריצה: 3 מסמכים" in md  # 3 ידועים, 1 נמחק, 1 נוסף
    assert "תיקייה שנסרקה: `folder-123`" in md
    assert "- הסכם חדש.png" in md and "- הסכם \\| ישן.pdf" in md and "סריקה פגומה.pdf: OCR נכשל" in md

    block = md.split("<details>")[1]
    assert "| הסכם חדש.png | image/png | 2026-10-21 08:12 | נוסף |" in block
    assert "| לא השתנה.pdf | application/pdf | 1 | ללא שינוי |" in block
    assert "| צילום.heic | image/heic | 1 | לא נתמך: סוג קובץ לא נתמך: image/heic |" in block
    assert "| קיצור |" in block and "נכשל: קיצור דרך לקובץ שאין אליו גישה (404)" in block
    assert (block.index("נכשל") < block.index("| לא נתמך") < block.index("נוסף") < block.index("עודכן")
            < block.index("ללא שינוי"))
    assert md.rstrip().endswith("</details>")
    assert "הושלמו נתונים מובנים" not in md  # אין מסמכים להשלמה, אין שורה

    assert "heic" not in downloads and "sc" not in downloads  # סוג לא נתמך לא מורד בכלל
    assert logged[0]["files"] == {"added": ["הסכם חדש.png"], "updated": ["הסכם | ישן.pdf"], "backfilled": []}
    assert logged[0]["ocr"] == 1


def test_summary_backfill_line():
    stats = {"added": 0, "updated": 0, "removed": 0, "unsupported": 0, "errors": [], "in_db": 2, "folder": "f",
             "seen": [], "files": {"added": [], "updated": [], "backfilled": ["א.pdf", "ב.pdf"]}}
    md = sync.summary(stats)
    assert "**הושלמו נתונים מובנים ל-2 מסמכים**\n- א.pdf\n- ב.pdf" in md, md


class _Req:
    def __init__(self, fn):
        self.execute = fn


class FakeDrive:
    """Drive מזויף: תוכן תיקיות לפי parent (בשני עמודים), ו-get שמחזיר את היעד של קיצור דרך."""
    TREE = {
        "root": [[{"id": "a", "name": "א.pdf", "mimeType": PDF},
                  {"id": "sub", "name": "תת-תיקייה", "mimeType": FOLDER}],
                 [{"id": "s1", "name": "קיצור לא", "mimeType": SHORTCUT, "shortcutDetails": {"targetId": "a"}},
                  {"id": "s2", "name": "קיצור לחוץ", "mimeType": SHORTCUT, "shortcutDetails": {"targetId": "ext"}},
                  {"id": "s3", "name": "קיצור לשורש", "mimeType": SHORTCUT, "shortcutDetails": {"targetId": "root"}},
                  {"id": "s4", "name": "אין גישה", "mimeType": SHORTCUT, "shortcutDetails": {"targetId": "secret"}}]],
        "sub": [[{"id": "b", "name": "ב.png", "mimeType": PNG},
                 {"id": "s5", "name": "קיצור לתיקייה", "mimeType": SHORTCUT, "shortcutDetails": {"targetId": "sub2"}}]],
        "sub2": [[{"id": "c", "name": "ג.pdf", "mimeType": PDF}]],
    }
    TARGETS = {"a": TREE["root"][0][0], "ext": {"id": "ext", "name": "חיצוני.pdf", "mimeType": PDF},
               "root": {"id": "root", "name": "שורש", "mimeType": FOLDER},
               "sub2": {"id": "sub2", "name": "תיקייה אחרת", "mimeType": FOLDER}}

    def files(self):
        return self

    def list(self, q, pageToken=None, **kw):
        pages = self.TREE[q.split("'")[1]]
        i = int(pageToken or 0)
        resp = {"files": pages[i]} | ({"nextPageToken": str(i + 1)} if i + 1 < len(pages) else {})
        return _Req(lambda: resp)

    def get(self, fileId, **kw):
        def execute():
            if fileId not in self.TARGETS:
                raise HttpError(type("Resp", (), {"status": 404, "reason": "Not Found"})(), b"")
            return self.TARGETS[fileId]
        return _Req(execute)


def test_list_files_shortcuts():
    files = drive.list_files(FakeDrive(), "root")
    names = sorted(f["name"] for f in files)
    # א.pdf פעם אחת למרות הקיצור אליו, קיצור לתיקייה נסרק, קיצור לשורש לא יוצר לולאה, עמוד שני נקרא
    assert names == sorted(["א.pdf", "ב.png", "ג.pdf", "חיצוני.pdf", "אין גישה"]), names
    assert next(f for f in files if f["name"] == "אין גישה")["error"] == "קיצור דרך לקובץ שאין אליו גישה (404)"


def test_status_and_admin_pages():
    runs = [
        {"started_at": datetime.datetime(2026, 10, 21, 6, 0), "added": 1, "updated": 0, "removed": 0, "ocr": 0,
         "failed": 0, "errors": [], "files": {"added": ["<הסכם>.pdf"], "updated": [], "backfilled": ["ישן.pdf"]}},
        {"started_at": datetime.datetime(2026, 10, 20, 6, 0), "added": 0, "updated": 0, "removed": 0, "ocr": 0,
         "failed": 0, "errors": None, "files": None},  # ריצה מלפני המיגרציה
    ]
    with mock.patch.multiple(store, connect=lambda: contextlib.nullcontext(), init=lambda conn: None,
                             status=lambda conn: {"updated_at": "21/10/2026 06:04", "documents": 42, "failed": True},
                             recent_runs=lambda conn: runs):
        c = TestClient(main.app)
        assert c.get("/api/status").json() == {"updated_at": "21/10/2026 06:04", "documents": 42, "failed": True}
        html = c.get("/admin/sync").text
    assert "זמן (שעון ישראל)" in html and "UTC" not in html
    assert "2026-10-21 06:00" in html and "&lt;הסכם&gt;.pdf" in html and "<td>—</td>" in html
    assert "הושלמו נתונים מובנים" in html and "<td>ישן.pdf</td>" in html


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    test_sync_summary_and_exit_code()
    test_summary_backfill_line()
    test_list_files_shortcuts()
    test_status_and_admin_pages()
    print("ok")
