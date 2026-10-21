"""בדיקות לסיכום הסנכרון, ל-/api/status ול-/admin/sync, עם תחליפים ל-Drive ול-Neon. בלי סודות ובלי רשת.

python tests/test_sync_status.py   (או pytest)
"""
import contextlib
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import sync  # noqa: E402
from app import main, store  # noqa: E402

FILES = [
    {"id": "new", "name": "הסכם חדש.pdf", "modifiedTime": "2"},
    {"id": "old", "name": "הסכם | ישן.pdf", "modifiedTime": "2"},
    {"id": "same", "name": "לא השתנה.pdf", "modifiedTime": "1"},
    {"id": "bad", "name": "סריקה פגומה.pdf", "modifiedTime": "1"},
]


def _fake_backends(logged):
    sync.drive.service = lambda: None
    sync.drive.list_files = lambda svc: FILES
    sync.drive.download = lambda svc, f: b""
    sync.extract.extract = lambda f, data: (_ for _ in ()).throw(RuntimeError("OCR נכשל")) if f["id"] == "bad" else ("t", "text")
    store.connect = lambda: contextlib.nullcontext()
    store.init = lambda conn: None
    store.indexed = lambda conn: {"old": "1", "same": "1", "gone": "1"}
    store.delete = lambda conn, ids: None
    store.upsert = lambda conn, f, method, text: None
    store.log_run = lambda conn, stats: logged.append(stats)


def test_sync_summary_and_exit_code():
    logged = []
    _fake_backends(logged)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["GITHUB_STEP_SUMMARY"] = path = os.path.join(tmp, "summary.md")
        sys.argv = ["sync.py"]
        try:
            sync.main()
            raise AssertionError("קובץ שנכשל חייב להכשיל את הריצה")
        except SystemExit as e:
            assert e.code == 1
        md = Path(path).read_text(encoding="utf-8")
    del os.environ["GITHUB_STEP_SUMMARY"]
    assert "| 1 | 1 | 1 | 1 |" in md, md  # נוספו, עודכנו, הוסרו, נכשלו
    assert "- הסכם חדש.pdf" in md and "- הסכם \\| ישן.pdf" in md and "סריקה פגומה.pdf: OCR נכשל" in md
    assert "לא השתנה" not in md
    assert logged[0]["files"] == {"added": ["הסכם חדש.pdf"], "updated": ["הסכם | ישן.pdf"]}


def test_status_and_admin_pages():
    store.connect = lambda: contextlib.nullcontext()
    store.init = lambda conn: None
    store.status = lambda conn: {"updated_at": "21/10/2026 06:04", "documents": 42, "failed": True}
    store.recent_runs = lambda conn: [
        {"started_at": datetime.datetime(2026, 10, 21, 6, 0), "added": 1, "updated": 0, "removed": 0, "ocr": 0,
         "failed": 0, "errors": [], "files": {"added": ["<הסכם>.pdf"], "updated": []}},
        {"started_at": datetime.datetime(2026, 10, 20, 6, 0), "added": 0, "updated": 0, "removed": 0, "ocr": 0,
         "failed": 0, "errors": None, "files": None},  # ריצה מלפני המיגרציה
    ]
    c = TestClient(main.app)
    assert c.get("/api/status").json() == {"updated_at": "21/10/2026 06:04", "documents": 42, "failed": True}
    html = c.get("/admin/sync").text
    assert "זמן (שעון ישראל)" in html and "UTC" not in html
    assert "2026-10-21 06:00" in html and "&lt;הסכם&gt;.pdf" in html and "<td>—</td>" in html


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    test_sync_summary_and_exit_code()
    test_status_and_admin_pages()
    print("ok")
