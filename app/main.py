"""שרת הווב: ניסוח הסכמים, ייצוא ל-Word, פאנל קבצים ודוחות.

כל האתר מאחורי סיסמה (APP_PASSWORD): מסך כניסה בפעם הראשונה, ועוגייה שזוכרת את המשתמש ל-30 יום.
פתוחים בלי כניסה רק /login, /healthz (בדיקת התקינות של Render) והקבצים הסטטיים (לוגו ו-favicon למסך הכניסה).
בלי APP_PASSWORD: ב-Render האתר חסום ("לא מוגדר"), כדי שלא ייפתח לכולם בטעות. במחשב מקומי הוא פתוח לפיתוח.

uvicorn app.main:app --reload
"""
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from app import ask, docx_export, draft, drive, github, store  # noqa: E402

STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("APP_PASSWORD", "")
TOKEN = hmac.new(PASSWORD.encode(), b"agreements-drafter-session", hashlib.sha256).hexdigest()

@asynccontextmanager
async def lifespan(app):
    """מיגרציות פעם אחת בהפעלת השרת, כדי שטבלאות חדשות (כמו files) יהיו קיימות עוד לפני הסנכרון הראשון."""
    try:
        with store.connect() as conn:
            store.init(conn)
    except Exception as e:  # מסד לא זמין לא מונע מהאתר לעלות; הניסוח והייצוא עדיין עובדים
        print(f"המיגרציות בהפעלת השרת נכשלו: {e}")
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # לוגו ו-favicon


SESSION_DAYS = 30


def is_writer(request: Request):
    """מחובר: APP_PASSWORD מוגדר ובדפדפן יש את עוגיית הכניסה."""
    return bool(PASSWORD) and hmac.compare_digest(request.cookies.get("session", ""), TOKEN)


def writer(request: Request):
    """תנאי לפעולות שכותבות ל-Drive או מפעילות סנכרון. כשהאתר פתוח במחשב מקומי (בלי סיסמה) הן חסומות."""
    if not is_writer(request):
        raise HTTPException(403, "הפעולה זמינה רק אחרי כניסה עם סיסמה")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/healthz") or path.startswith("/static/"):  # כל השאר דורש כניסה
        return await call_next(request)
    if not PASSWORD:
        if os.environ.get("RENDER"):  # Render מגדיר את המשתנה הזה. בלי סיסמה לא פותחים את האתר לכולם בטעות
            return HTMLResponse("<h1 dir='rtl'>האתר לא מוגדר: חסר APP_PASSWORD ב-Render ← Environment</h1>", 503)
        return await call_next(request)  # פיתוח מקומי
    if is_writer(request):
        return await call_next(request)
    if path.startswith(("/api/", "/admin/")):
        return JSONResponse({"detail": "נדרשת כניסה"}, 401)
    return RedirectResponse("/login?next=" + quote(path), 303)


def _safe_next(target):
    """חזרה לדף שביקשו, רק בתוך האתר: לא "//אתר-אחר" ולא כתובת מלאה."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
async def login(request: Request):
    """טופס HTML רגיל (בלי תלות ב-python-multipart): password ו-next."""
    form = parse_qs((await request.body()).decode())
    password, target = form.get("password", [""])[0], _safe_next(form.get("next", ["/"])[0])
    if not PASSWORD or not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        await asyncio.sleep(1)  # מאט ניחוש סיסמאות
        return RedirectResponse("/login?error=1&next=" + quote(target), 303)
    resp = RedirectResponse(target, 303)
    https = request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie("session", TOKEN, httponly=True, secure=https, samesite="lax", max_age=SESSION_DAYS * 24 * 3600)
    # מזהה לכל דפדפן, למגבלת הניסוחים. עוגיית session זהה לכולם (סיסמה אחת), ולכן היא לא מבדילה בין משתמשים
    resp.set_cookie("sid", secrets.token_urlsafe(16), httponly=True, secure=https, samesite="lax",
                    max_age=SESSION_DAYS * 24 * 3600)
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/me")
def me(request: Request):
    """מה הממשק מציג: העלאה וסנכרון (רק למחובר, ורק כשהסנכרון מהאתר מוגדר)."""
    return {"writer": is_writer(request), "sync": github.configured()}


class Message(BaseModel):
    role: str
    content: str


class DraftRequest(BaseModel):
    messages: list[Message]
    focus_file_ids: list[str] = []  # "נסח על בסיס המסמך הזה", אפשר כמה מסמכים
    conversation: str | None = Field(None, max_length=64)  # מזהה השיחה, לחיבור העלות לטיוטה
    web_check: bool = False  # "בדיקת דרישות חוק עדכניות באינטרנט"
    deep_review: bool = False  # "בדיקה משפטית מעמיקה"


DRAFTS_PER_HOUR = 30
DAILY_DRAFT_LIMIT = int(os.environ.get("DAILY_DRAFT_LIMIT", "200"))
# ponytail: בזיכרון התהליך, מתאפס בהפעלה מחדש ולא משותף בין תהליכים. מספיק לשרת אחד ב-Render
_drafts = defaultdict(deque)  # sid ← זמני הניסוחים בשעה האחרונה
_daily = {"date": None, "count": 0}


def _draft_quota(request: Request):
    """עד DRAFTS_PER_HOUR ניסוחים בשעה לכל דפדפן, ו-DAILY_DRAFT_LIMIT לכל האתר ביום (UTC). מעבר לזה 429."""
    now, today = time.time(), date.today()
    if _daily["date"] != today:
        _daily.update(date=today, count=0)
    if _daily["count"] >= DAILY_DRAFT_LIMIT:
        raise HTTPException(429, "הגענו לתקרת הניסוחים היומית של האתר. אפשר לנסות שוב מחר")
    key = request.cookies.get("sid") or request.headers.get("x-forwarded-for", "").split(",")[0] or request.client.host
    times = _drafts[key]
    while times and times[0] <= now - 3600:
        times.popleft()
    if len(times) >= DRAFTS_PER_HOUR:
        wait = int(times[0] + 3600 - now) // 60 + 1
        raise HTTPException(429, f"הגעת למגבלה של {DRAFTS_PER_HOUR} ניסוחים בשעה. אפשר לנסות שוב בעוד {wait} דקות")
    times.append(now)
    _daily["count"] += 1


@app.post("/api/draft")
async def api_draft(req: DraftRequest, request: Request):
    msgs = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "ההודעה האחרונה חייבת להיות של המשתמש")
    _draft_quota(request)
    try:
        return await run_in_threadpool(draft.draft, msgs, req.focus_file_ids, req.conversation, req.web_check,
                                       req.deep_review)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # הפרטים ללוג של השרת בלבד, לא לממשק
        print(f"הניסוח נכשל: {e!r}")
        raise HTTPException(502, "הניסוח נכשל: שירות הניסוח לא הגיב או החזיר שגיאה. נסה שוב בעוד כמה דקות")


class AskRequest(BaseModel):
    messages: list[Message] = Field(max_length=40)
    conversation: str | None = Field(None, max_length=64)


@app.post("/api/ask")
async def api_ask(req: AskRequest, request: Request):
    """עמוד "שאלו על המסמכים". אותה מכסה כמו הניסוח, כי גם כאן כל שאלה היא קריאה למודל."""
    msgs = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "ההודעה האחרונה חייבת להיות שאלה")
    _draft_quota(request)
    try:
        return await run_in_threadpool(ask.ask, msgs, req.conversation)
    except Exception as e:
        print(f"התשובה נכשלה: {e!r}")
        raise HTTPException(502, "לא הצלחתי לענות: שירות המודל לא הגיב או החזיר שגיאה. נסה שוב בעוד כמה דקות")


class DocxRequest(BaseModel):
    title: str
    body: str
    html: str | None = None  # מהעורך באתר: עם הדגשות, גודל, יישור וכיוון כתב


@app.post("/api/docx")
def api_docx(req: DocxRequest):
    name = quote((req.title.strip() or "הסכם")[:80] + ".docx")
    return Response(
        docx_export.build_html(req.title, req.html) if req.html else docx_export.build(req.title, req.body),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


class HistoryRequest(BaseModel):
    id: int | None = None  # None: הסכם חדש
    title: str = Field(max_length=500)
    html: str = Field(max_length=1_000_000)
    text: str = Field(max_length=500_000)
    messages: list[dict] = Field(max_length=200)
    sources: list[dict] = Field(max_length=50)
    conversation: str | None = Field(None, max_length=64)


@app.get("/api/costs")
def api_costs():
    """עמוד "עלויות". מ-SQL בלבד, בלי קריאה למודל."""
    with store.connect() as conn:
        return store.costs(conn)


@app.get("/api/history")
def api_history():
    """עמוד "טיוטות": הטיוטות מהשבוע האחרון."""
    with store.connect() as conn:
        return store.drafts_list(conn)


@app.post("/api/history")
def api_history_save(req: HistoryRequest):
    """נקרא אחרי כל ניסוח ובזמן עריכה (בהשהיה), כך שההסכם בהיסטוריה תמיד בגרסה האחרונה שלו."""
    with store.connect() as conn:
        return {"id": store.save_draft(conn, req.id, req.title, req.html, req.text, req.messages, req.sources,
                                       req.conversation)}


@app.get("/api/history/{draft_id}")
def api_history_get(draft_id: int):
    with store.connect() as conn:
        d = store.get_draft(conn, draft_id)
    if not d:
        raise HTTPException(404, "ההסכם לא נמצא. ייתכן שעבר שבוע והוא נמחק")
    return d


@app.delete("/api/history/{draft_id}")
def api_history_delete(draft_id: int):
    with store.connect() as conn:
        if not store.delete_draft(conn, draft_id):
            raise HTTPException(404, "ההסכם לא נמצא")
    return {"ok": True}


STATUS_TITLE = "סטטוס מאגר ההסכמים"
STATUS_HEADER = ["מדד", "ערך"]


def status_tsv(rows):
    """"העתקה": עמודות מופרדות בטאב. באקסל נדבק כטבלה, ובכל מקום אחר כשורות קריאות."""
    return "\n".join("\t".join(cells) for cells in [STATUS_HEADER, *([r["label"], r["value"]] for r in rows)])


@app.get("/api/status")
def api_status():
    """טבלת "סטטוס מאגר ההסכמים" בפאנל הצדדי. מ-SQL בלבד, בלי מודל.
    אם המסד לא זמין, הממשק פשוט לא מציג אותה."""
    with store.connect() as conn:
        rows = store.status_rows(conn)
        return store.status(conn) | {"title": STATUS_TITLE, "rows": rows, "tsv": status_tsv(rows)}


@app.get("/api/status/docx")
def api_status_docx():
    with store.connect() as conn:
        rows = store.status_rows(conn)
    return Response(
        docx_export.build_table(STATUS_TITLE, STATUS_HEADER, [[r["label"], r["value"]] for r in rows]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(STATUS_TITLE + '.docx')}"},
    )


@app.get("/api/files")
def api_files():
    with store.connect() as conn:
        return store.files_list(conn)


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
        svc = drive.service()
        f = drive.verify_upload(svc, drive.upload(svc, clean, data, mime), data)
        with store.connect() as conn:  # רק אחרי שווידאנו שהקובץ נשמר בתיקייה, כמו שהוא
            store.add_queued(conn, f)
        return f

    try:
        f = await run_in_threadpool(save)
    except drive.UploadMismatch as e:
        raise HTTPException(502, str(e))
    return {"name": f["name"], "renamed": f["name"] != clean, "webViewLink": f["webViewLink"],
            "sync": _sync(defer=True)}


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
