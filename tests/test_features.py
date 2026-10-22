"""בדיקות ליכולות החדשות: טבלת files בסנכרון, backfill, דוחות, העלאה, מסמך בסיס, הגבלת הסנכרון ו-OCR לתמונות.

בדיקות ה-SQL רצות מול Postgres אמיתי (PGlite, ראה tests/pg.py). Drive, OpenAI ו-GitHub מוחלפים בתחליפים.
בלי סודות ובלי רשת.

python tests/test_features.py   (או pytest)
"""
import contextlib
import io
import os
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agno.agent import Agent  # noqa: E402
from agno.run.base import RunStatus  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pg import postgres  # noqa: E402

import sync  # noqa: E402
from app import draft, drive, extract, github, main, store  # noqa: E402

PDF, PNG = "application/pdf", "image/png"
VEC = "[" + ",".join(["0.1"] * 1536) + "]"
META = {"doc_type": "הסכם", "parties": ["א", "ב"], "plan_number": None, "location": None, "gush_helka": None,
        "start_date": None, "end_date": "2027-01-31", "amounts": None, "pages": None}


def skip(reason):
    try:
        import pytest
        pytest.skip(reason)
    except ImportError:
        print("SKIP:", reason)


def _agreement(conn, file_id, name, metadata=None, version=None, end_date=None):
    conn.execute(
        """insert into agreements (file_id, name, url, modified_time, method, summary, text, embedding,
                                   metadata, metadata_version, end_date)
           values (%s, %s, %s, '1', 'text', 'תקציר קיים', %s, %s::vector, %s, %s, %s)""",
        (file_id, name, f"https://drive/{file_id}", f"טקסט של {name}", VEC,
         None if metadata is None else store.Jsonb(metadata), version, end_date),
    )


@contextlib.contextmanager
def _sync_fakes(conn, listing, downloads):
    """sync.py מול ה-Postgres של הבדיקה, עם Drive, OCR ומודל מזויפים."""
    def fake_extract(f, data):
        if f["id"] == "bad":
            raise RuntimeError("OCR החזיר 3 תווים ל-1 עמודים")
        return "טקסט", "ocr" if f["mimeType"] == PNG else "text"

    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"} | {"DRIVE_FOLDER_ID": "folder"}
    with mock.patch.multiple(store, connect=lambda: contextlib.nullcontext(conn),
                             analyze=lambda name, text: ("תקציר", dict(META)), embed=lambda text: VEC), \
            mock.patch.multiple(sync.drive, service=lambda: None, list_files=lambda svc, folder: listing,
                                download=lambda svc, f: downloads.append(f["id"]) or b"data"), \
            mock.patch.multiple(sync.extract, extract=fake_extract, page_count=lambda f, data: 3), \
            mock.patch.dict(os.environ, env, clear=True):
        yield


def test_sync_files_table_failed_unsupported_retry():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        for fid, name in (("same", "ללא שינוי.pdf"), ("old", "ישן.pdf"), ("gone", "נמחק מ-Drive.pdf")):
            _agreement(conn, fid, name, metadata=META, version=store.METADATA_VERSION)
            conn.execute("insert into files (file_id, name, status) values (%s, %s, 'indexed')", (fid, name))
        conn.execute("update files set retry_requested = true, status = 'queued' where file_id = 'same'")
        listing = [
            {"id": "new", "name": "סריקה.png", "mimeType": PNG, "modifiedTime": "1", "size": "2048"},
            {"id": "bad", "name": "פגום.pdf", "mimeType": PDF, "modifiedTime": "1", "size": "10"},
            {"id": "heic", "name": "צילום.heic", "mimeType": "image/heic", "modifiedTime": "1"},
            {"id": "same", "name": "ללא שינוי.pdf", "mimeType": PDF, "modifiedTime": "1"},
            {"id": "old", "name": "ישן.pdf", "mimeType": PDF, "modifiedTime": "2"},
        ]
        downloads = []
        with _sync_fakes(conn, listing, downloads):
            stats = sync.sync()

        rows = {r["file_id"]: r for r in store._dicts(conn, "select * from files")}
        assert set(rows) == {"new", "bad", "heic", "same", "old"}, rows.keys()  # מה שנמחק מ-Drive נמחק מהטבלה
        assert rows["new"]["status"] == "indexed" and rows["new"]["size_bytes"] == 2048 and rows["new"]["last_attempt_at"]
        assert rows["bad"]["status"] == "failed" and rows["bad"]["error"].startswith("OCR החזיר")
        assert rows["heic"]["status"] == "unsupported" and rows["heic"]["error"] == "סוג קובץ לא נתמך: image/heic"
        assert rows["same"]["status"] == "indexed" and rows["same"]["retry_requested"] is False
        assert rows["same"]["last_attempt_at"] is not None
        assert sorted(downloads) == ["bad", "new", "old", "same"]  # same עובד מחדש בגלל retry; heic לא הורד
        assert [e["file"] for e in stats["errors"]] == ["פגום.pdf"]  # "לא נתמך" אינו שגיאה
        assert stats["unsupported"] == 1 and stats["files"]["added"] == ["סריקה.png"]
        meta = conn.execute("select metadata->>'pages', end_date from agreements where file_id = 'new'").fetchone()
        assert meta == ("3", date(2027, 1, 31)), meta  # מספר העמודים האמיתי מהקובץ

        downloads.clear()
        with _sync_fakes(conn, listing, downloads):
            sync.sync()
        assert downloads == ["bad"], downloads  # בריצה הבאה רק מה שנכשל מנוסה שוב; ה-retry כבר התאפס


def test_backfill_selects_only_missing_or_stale():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        _agreement(conn, "none", "בלי נתונים.pdf")
        _agreement(conn, "done", "הושלם.pdf", metadata=META, version=store.METADATA_VERSION)
        _agreement(conn, "stale", "גרסה ישנה.pdf", metadata=META, version=store.METADATA_VERSION - 1)
        _agreement(conn, "fail", "ייכשל.pdf")
        assert {d["file_id"] for d in store.pending_metadata(conn)} == {"none", "stale", "fail"}

        def fake_analyze(name, text):
            assert text == f"טקסט של {name}"  # מהמסד, בלי Drive ובלי OCR
            if name == "ייכשל.pdf":
                raise RuntimeError("timeout")
            return "תקציר חדש", dict(META)

        stats = {"errors": [], "files": {"backfilled": []}}
        with mock.patch.object(store, "analyze", fake_analyze):
            sync.backfill(conn, stats)
        assert sorted(stats["files"]["backfilled"]) == ["בלי נתונים.pdf", "גרסה ישנה.pdf"]
        assert stats["errors"] == [{"file": "ייכשל.pdf", "error": "השלמת נתונים מובנים: timeout"}]
        assert {d["file_id"] for d in store.pending_metadata(conn)} == {"fail"}  # ינוסה שוב בריצה הבאה
        row = conn.execute("select summary, end_date, metadata_version from agreements where file_id = 'none'").fetchone()
        assert row == ("תקציר קיים", date(2027, 1, 31), store.METADATA_VERSION)  # התקציר וה-embedding לא משתנים


def test_files_panel_queries():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        _agreement(conn, "doc", "הסכם.pdf", META | {"plan_number": 'תת"ל 120'}, 1)
        for fid, status, error in (("doc", "indexed", None), ("broken", "failed", "OCR נכשל")):
            conn.execute("insert into files (file_id, name, status, error) values (%s, %s, %s, %s)",
                         (fid, fid + ".pdf", status, error))
        store.add_queued(conn, {"id": "up", "name": "הועלה.pdf", "mimeType": PDF, "size": "99", "modifiedTime": "t"})

        listed = {f["file_id"]: f for f in store.files_list(conn)}
        assert listed["up"]["url"] == "https://drive.google.com/file/d/up/view" and listed["up"]["status"] == "queued"
        assert listed["doc"]["metadata"]["plan_number"] == 'תת"ל 120' and listed["doc"]["url"] == "https://drive/doc"
        assert listed["doc"]["method"] == "text" and listed["up"]["method"] is None  # לחלון הפרטים: OCR או ישירות
        assert listed["broken"]["error"] == "OCR נכשל"
        assert store.mark_retry(conn, "broken") == "broken.pdf" and store.mark_retry(conn, "missing") is None
        assert store.retry_ids(conn) == {"broken"}

        with mock.patch.object(store, "connect", lambda: contextlib.nullcontext(conn)):
            files = TestClient(main.app).get("/api/files").json()
        assert [f["name"] for f in files] == ["broken.pdf", "doc.pdf", "הועלה.pdf"]


def test_server_start_creates_tables_before_first_sync():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        with mock.patch.object(store, "connect", lambda: contextlib.nullcontext(conn)):
            with TestClient(main.app) as c:  # with מריץ את ה-lifespan, כמו הפעלת השרת ב-Render
                r = c.get("/api/files")
                assert r.status_code == 200 and r.json() == [], r.text  # "הרשימה תתמלא אחרי הסנכרון הבא"
                assert c.get("/api/reports/index").status_code == 404  # הדוחות הוסרו


def _docx_bytes():
    import docx
    buf = io.BytesIO()
    docx.Document().save(buf)
    return buf.getvalue()


def test_upload_validation():
    ok = {"a.pdf": b"%PDF-1.7", "a.PNG": b"\x89PNG\r\n\x1a\n...", "a.jpg": b"\xff\xd8\xff\xe0", "a.jpeg": b"\xff\xd8\xff",
          "a.tif": b"II*\x00..", "a.tiff": b"MM\x00*..", "a.webp": b"RIFF\x00\x00\x00\x00WEBPVP8 ", "a.docx": _docx_bytes()}
    for name, data in ok.items():
        assert drive.check_upload(name, data)[0] == name
    assert drive.check_upload("C:\\Users\\x\\חוזה.pdf", b"%PDF-")[0] == "חוזה.pdf"  # בלי נתיב
    zip_not_docx = io.BytesIO()
    zipfile.ZipFile(zip_not_docx, "w").writestr("x.txt", "x")
    bad = {"a.exe": b"MZ", "a": b"%PDF-", "a.pdf": b"\x89PNG\r\n\x1a\n", "a.png": b"%PDF-", "b.pdf": b"",
           "big.pdf": b"%PDF-" + b"0" * drive.MAX_UPLOAD, "a.docx": zip_not_docx.getvalue()}
    for name, data in bad.items():
        try:
            drive.check_upload(name, data)
            raise AssertionError(f"היה צריך לדחות: {name}")
        except ValueError:
            pass
    assert drive.unique_name("הסכם.pdf", set()) == "הסכם.pdf"
    assert drive.unique_name("הסכם.pdf", {"הסכם.pdf"}) == "הסכם (1).pdf"
    assert drive.unique_name("הסכם.pdf", {"הסכם.pdf", "הסכם (1).pdf"}) == "הסכם (2).pdf"


def test_upload_api_requires_password_and_renames():
    def fake_upload(svc, name, data, mime):
        return {"id": "x", "name": drive.unique_name(name, {"הסכם.pdf"}), "mimeType": mime, "size": str(len(data))}

    queued = []
    with mock.patch.multiple(main, PASSWORD="", TOKEN="t"), mock.patch.dict(os.environ, {"RENDER": ""}):
        c = TestClient(main.app)  # פיתוח מקומי בלי סיסמה: האתר פתוח, אבל פעולות כתיבה חסומות
        assert c.get("/api/me").json() == {"writer": False, "sync": github.configured()}
        assert c.post("/api/upload?name=הסכם.pdf", content=b"%PDF-1.7").status_code == 403
        assert c.post("/api/sync").status_code == 403
    with mock.patch.multiple(main, PASSWORD="secret", TOKEN="token-of-secret"), \
            mock.patch.multiple(drive, service=lambda: None, upload=fake_upload), \
            mock.patch.multiple(store, connect=lambda: contextlib.nullcontext(None),
                                add_queued=lambda conn, f: queued.append(f["name"])), \
            mock.patch.object(github, "configured", lambda: False):
        c = TestClient(main.app)
        assert c.post("/api/upload?name=הסכם.pdf", content=b"%PDF-1.7").status_code == 401  # לא מחובר
        assert c.post("/login", data={"password": "secret"}, follow_redirects=False).status_code == 303
        assert c.get("/api/me").json()["writer"] is True
        r = c.post("/api/upload?name=הסכם.pdf", content=b"%PDF-1.7")
        assert r.json() == {"name": "הסכם (1).pdf", "renamed": True, "sync": None}, r.text
        assert queued == ["הסכם (1).pdf"]
        assert c.post("/api/upload?name=a.pdf", content=b"\x89PNG\r\n\x1a\n").status_code == 400  # תוכן לא תואם
        big = c.post("/api/upload?name=a.pdf", content=b"%PDF-" + b"0" * drive.MAX_UPLOAD)
        assert big.status_code == 413


def test_whole_site_requires_login():
    with mock.patch.multiple(main, PASSWORD="secret", TOKEN="token-of-secret"):
        c = TestClient(main.app, follow_redirects=False)
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/"  # מסך כניסה בפעם הראשונה
        for path in ("/api/files", "/api/status", "/api/status/docx", "/admin/sync"):
            assert c.get(path).status_code == 401, path
        assert c.post("/api/draft", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401
        assert c.get("/healthz").status_code == 200  # בדיקת התקינות של Render
        assert c.get("/static/logo.png").status_code == 200 and c.get("/static/favicon.png").status_code == 200
        assert "סיסמה" in c.get("/login").text

        wrong = c.post("/login", data={"password": "nope", "next": "/admin/sync"})
        assert wrong.status_code == 303 and wrong.headers["location"] == "/login?error=1&next=/admin/sync"
        assert "session" not in c.cookies
        for evil in ("//evil.com", "https://evil.com", "/\\evil.com"):  # לא מפנים לאתר אחר אחרי הכניסה
            assert c.post("/login", data={"password": "secret", "next": evil}).headers["location"] == "/"
        ok = c.post("/login", data={"password": "secret", "next": "/admin/sync"})
        assert ok.status_code == 303 and ok.headers["location"] == "/admin/sync"
        cookie = ok.headers["set-cookie"].lower()
        assert "max-age=2592000" in cookie and "httponly" in cookie  # 30 יום
        assert c.get("/").status_code == 200

    with mock.patch.multiple(main, PASSWORD="", TOKEN="t"):
        with mock.patch.dict(os.environ, {"RENDER": "true"}):  # ב-Render בלי סיסמה: חסום, לא פתוח לכולם
            c = TestClient(main.app, follow_redirects=False)
            assert c.get("/").status_code == 503 and c.get("/healthz").status_code == 200
        with mock.patch.dict(os.environ, {"RENDER": ""}):  # פיתוח מקומי: פתוח
            assert TestClient(main.app).get("/").status_code == 200


def test_focus_file_in_agent_context():
    base = {"file_id": "b1", "name": "בסיס.pdf", "url": "https://drive/b1", "summary": "תקציר", "text": "טקסט הבסיס"}
    sources = []
    agent = draft.build_agent(sources, base)
    assert "טקסט הבסיס" in agent.additional_context and "הדוגמה העיקרית" in agent.additional_context
    assert sources == [{"name": "בסיס.pdf", "url": "https://drive/b1", "summary": "תקציר", "base": True}]
    found = [base, {"name": "אחר.pdf", "url": "u", "summary": "s", "text": "t"}]
    with mock.patch.multiple(store, connect=lambda: contextlib.nullcontext(None), search=lambda conn, q: found):
        agent.tools[0]("שאילתה")
    assert [s["name"] for s in sources] == ["בסיס.pdf", "אחר.pdf"]  # הבסיס לא נכפל, ונשאר ראשון

    def fake_run(self, messages):
        assert "טקסט הבסיס" in self.additional_context
        return SimpleNamespace(status=RunStatus.completed,
                               content=draft.Draft(status="agreement", questions=[], title="הסכם", body="גוף"))

    docs = {"b1": base}
    with mock.patch.multiple(store, connect=lambda: contextlib.nullcontext(None),
                             get_doc=lambda conn, fid: docs.get(fid)), mock.patch.object(Agent, "run", fake_run):
        out = draft.draft([{"role": "user", "content": "נסח"}], "b1")
        assert out["sources"][0]["base"] is True and out["title"] == "הסכם"
        r = TestClient(main.app).post("/api/draft", json={"messages": [{"role": "user", "content": "נסח"}],
                                                          "focus_file_id": "missing"})
        assert r.status_code == 404 and "לא נמצא" in r.json()["detail"]


def test_sync_trigger_rate_limit():
    calls = []
    with mock.patch.object(github, "_api", lambda method, path, body=None: calls.append(path)), \
            mock.patch.multiple(github, _last=None, _timer=None):
        assert github.trigger()["started"] is True
        second = github.trigger()
        assert second["started"] is False and 290 < second["retry_after"] <= 301
        deferred = github.trigger(defer=True)
        assert deferred["deferred"] is True and github._timer is not None
        github._timer.cancel()
        assert calls == ["actions/workflows/sync.yml/dispatches"]  # רק הפעלה אחת בפועל


def test_image_ocr_with_tesseract():
    if not (shutil.which("tesseract") or Path(extract.WINDOWS_TESSERACT).exists()):
        return skip("Tesseract לא מותקן")
    from PIL import Image, ImageDraw, ImageFont
    font = next((ImageFont.truetype(f, 36) for f in ("arial.ttf", "DejaVuSans.ttf")
                 if _font_exists(ImageFont, f)), None)
    if font is None:
        return skip("אין גופן TrueType לציור טקסט")

    def page(text):  # לפחות 50 תווים לעמוד, אחרת extract מסמן את ה-OCR ככושל (בכוונה)
        img = Image.new("RGB", (1400, 360), "white")
        ImageDraw.Draw(img).multiline_text((40, 40), text, fill="black", font=font, spacing=20)
        return img

    p1 = page("This agreement is made between\nthe Ministry and the Company\nfor the night parking project.")
    p2 = page("The term of this agreement ends\nin December 2030, unless both\nparties agree otherwise in writing.")
    png, tif = io.BytesIO(), io.BytesIO()
    p1.save(png, "PNG")
    p1.save(tif, "TIFF", save_all=True, append_images=[p2])
    text, method = extract.extract({"mimeType": PNG}, png.getvalue())
    assert method == "ocr" and "agreement" in text, text
    text, method = extract.extract({"mimeType": "image/tiff"}, tif.getvalue())
    assert "agreement" in text and "December" in text, text  # שני העמודים של ה-TIFF
    assert extract.page_count({"mimeType": "image/tiff"}, tif.getvalue()) == 2


def _text_pdf(text):
    """PDF דיגיטלי מינימלי עם שכבת טקסט אמיתית (כמו קובץ שנוצר מ-Word)."""
    stream = f"BT /F1 11 Tf 40 750 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
            b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream"]
    out, offsets = io.BytesIO(b"%PDF-1.4\n"), []
    out.seek(0, 2)
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets))
    out.write(b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def test_pdf_scanned_vs_digital_detection():
    """PDF עם שכבת טקסט נקרא ישירות, בלי OCR. PDF שהוא רק תמונה (סריקה פיזית) מזוהה ועובר OCR."""
    from PIL import Image
    pdf = {"mimeType": PDF}
    digital = _text_pdf("This agreement is made between the Ministry and the Company for the night parking project.")
    scanned = io.BytesIO()
    Image.new("RGB", (1240, 1754), "white").save(scanned, "PDF")  # עמוד סרוק: תמונה בלבד, אין טקסט לסמן

    ocr_calls = []

    def fake_ocr(images):
        pages = list(images)
        ocr_calls.append(len(pages))
        return ["טקסט שזוהה מהסריקה, ארוך מספיק כדי לעבור את בדיקת האיכות של 50 תווים לעמוד."] * len(pages)

    with mock.patch.object(extract, "ocr", fake_ocr):
        text, method = extract.extract(pdf, digital)
        assert method == "text" and "Ministry" in text and ocr_calls == []  # לא נשלח ל-OCR
        text, method = extract.extract(pdf, scanned.getvalue())
        assert method == "ocr" and ocr_calls == [1] and "סריקה" in text  # זוהה כסרוק


def _font_exists(image_font, name):
    try:
        image_font.truetype(name, 10)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for test in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        test()
        print("ok ", test.__name__)
