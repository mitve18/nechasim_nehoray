"""שרת הווב: ניסוח הסכמים, ייצוא ל-Word, פאנל קבצים ודוחות. האתר פתוח לכל מי שיש לו את הקישור.

העלאת קבצים, "נסה שוב" והפעלת סנכרון דורשים כניסה עם APP_PASSWORD. בלי APP_PASSWORD הם חסומים (403).

uvicorn app.main:app --reload
"""
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from app import docx_export, draft, drive, github, store  # noqa: E402

STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("APP_PASSWORD", "")
TOKEN = hmac.new(PASSWORD.encode(), b"agreements-drafter-session", hashlib.sha256).hexdigest()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # לוגו ו-favicon


def is_writer(request: Request):
    return bool(PASSWORD) and hmac.compare_digest(request.cookies.get("session", ""), TOKEN)


def writer(request: Request):
    """תנאי לפעולות שכותבות ל-Drive או מפעילות סנכרון: APP_PASSWORD מוגדר והמשתמש מחובר."""
    if not is_writer(request):
        raise HTTPException(403, "הפעולה זמינה רק אחרי כניסה עם סיסמה")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/me")
def me(request: Request):
    """מה הממשק מציג: כפתור כניסה, העלאה, סנכרון."""
    return {"login": bool(PASSWORD), "writer": is_writer(request), "sync": github.configured()}


class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: Login, request: Request, response: Response):
    if not PASSWORD or not hmac.compare_digest(req.password.encode(), PASSWORD.encode()):
        await asyncio.sleep(1)  # מאט ניחוש סיסמאות
        raise HTTPException(401, "סיסמה שגויה")
    https = request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie("session", TOKEN, httponly=True, secure=https, samesite="lax", max_age=30 * 24 * 3600)
    return {"ok": True}


class Message(BaseModel):
    role: str
    content: str


class DraftRequest(BaseModel):
    messages: list[Message]
    focus_file_id: str | None = None  # "נסח על בסיס המסמך הזה"


@app.post("/api/draft")
async def api_draft(req: DraftRequest):
    msgs = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "ההודעה האחרונה חייבת להיות של המשתמש")
    try:
        return await run_in_threadpool(draft.draft, msgs, req.focus_file_id)
    except LookupError as e:
        raise HTTPException(404, str(e))


class DocxRequest(BaseModel):
    title: str
    body: str


@app.post("/api/docx")
def api_docx(req: DocxRequest):
    name = quote((req.title.strip() or "הסכם")[:80] + ".docx")
    return Response(
        docx_export.build(req.title, req.body),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@app.get("/api/status")
def api_status():
    """שורת הסטטוס בראש האתר. אם המסד לא זמין, הממשק פשוט לא מציג אותה."""
    with store.connect() as conn:
        return store.status(conn)


@app.get("/api/files")
def api_files():
    with store.connect() as conn:
        return store.files_list(conn)


REPORTS = {"expiring": store.report_expiring, "index": store.report_index, "types": store.report_types}


@app.get("/api/reports/{kind}")
def api_report(kind: str):
    """דוחות מ-SQL בלבד, בלי קריאה למודל."""
    if kind not in REPORTS:
        raise HTTPException(404, "דוח לא קיים")
    with store.connect() as conn:
        return {"rows": REPORTS[kind](conn), "pending": store.pending_count(conn)}


def _sync(defer):
    """מפעיל סנכרון אם הוא מוגדר. מחזיר None כשאין token, ולא מכשיל את הפעולה שקראה לו."""
    if not github.configured():
        return None
    try:
        return github.trigger(defer)
    except Exception as e:
        return {"started": False, "error": f"הפעלת הסנכרון נכשלה: {e}"}


@app.post("/api/files/{file_id}/retry", dependencies=[Depends(writer)])
def api_retry(file_id: str):
    with store.connect() as conn:
        name = store.mark_retry(conn, file_id)
    if name is None:
        raise HTTPException(404, "הקובץ לא נמצא")
    return {"name": name, "sync": _sync(defer=True)}


@app.post("/api/sync", dependencies=[Depends(writer)])
def api_sync():
    if not github.configured():
        raise HTTPException(404, "הסנכרון מהאתר לא מוגדר")
    try:
        return github.trigger()
    except Exception as e:
        raise HTTPException(502, f"הפעלת הסנכרון נכשלה: {e}")


@app.get("/api/sync", dependencies=[Depends(writer)])
def api_sync_status():
    if not github.configured():
        return {"running": False}
    return {"running": github.running()}


async def _read_body(request, limit):
    """גוף הבקשה, עם עצירה מוקדמת אם הוא גדול מהמותר (בלי לקרוא קובץ ענק לזיכרון)."""
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, "הקובץ גדול מ-20MB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, "הקובץ גדול מ-20MB")
    return bytes(data)


@app.post("/api/upload", dependencies=[Depends(writer)])
async def api_upload(request: Request, name: str):
    """קובץ אחד לכל בקשה: הגוף הוא תוכן הקובץ, והשם ב-?name=. נבדק בשרת לפני ההעלאה ל-Drive."""
    data = await _read_body(request, drive.MAX_UPLOAD)
    try:
        clean, mime = drive.check_upload(name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def save():
        f = drive.upload(drive.service(), clean, data, mime)
        with store.connect() as conn:
            store.add_queued(conn, f)
        return f

    f = await run_in_threadpool(save)
    return {"name": f["name"], "renamed": f["name"] != clean, "sync": _sync(defer=True)}


@app.get("/admin/sync", response_class=HTMLResponse)
def sync_log():
    with store.connect() as conn:
        store.init(conn)
        runs = store.recent_runs(conn)

    def names(r, key):
        return "<br>".join(_esc(n) for n in (r["files"] or {}).get(key, [])) or "—"

    rows = "".join(
        f"<tr><td>{r['started_at']:%Y-%m-%d %H:%M}</td><td>{r['added']}</td><td>{r['updated']}</td>"
        f"<td>{r['removed']}</td><td>{r['ocr']}</td><td>{r['failed']}</td>"
        f"<td>{names(r, 'added')}</td><td>{names(r, 'backfilled')}</td>"
        f"<td>{'<br>'.join(_esc(e['file'] + ': ' + e['error']) for e in r['errors'] or [])}</td></tr>"
        for r in runs
    )
    return f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><title>לוג סנכרון</title>
<style>body{{font-family:system-ui,Arial;margin:24px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px 10px;text-align:right;vertical-align:top}}</style>
<h1>לוג סנכרון</h1><table><tr><th>זמן (שעון ישראל)</th><th>נוספו</th><th>עודכנו</th><th>הוסרו</th><th>OCR</th><th>נכשלו</th><th>קבצים שנוספו</th><th>הושלמו נתונים מובנים</th><th>שגיאות</th></tr>{rows}</table></html>"""


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
