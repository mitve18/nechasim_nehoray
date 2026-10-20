"""שרת הווב: כניסה בסיסמה משותפת, ניסוח הסכמים, ייצוא ל-Word.

uvicorn app.main:app --reload
"""
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

load_dotenv()

from app import docx_export, draft, store  # noqa: E402

PASSWORD = os.environ.get("APP_PASSWORD", "")  # ריק = האתר פתוח לכל מי שיש לו את הקישור
TOKEN = hmac.new(PASSWORD.encode(), b"agreements-drafter-session", hashlib.sha256).hexdigest()
STATIC = Path(__file__).parent / "static"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def authed(request):
    return not PASSWORD or hmac.compare_digest(request.cookies.get("session", ""), TOKEN)


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path not in ("/login", "/healthz") and not authed(request):
        if request.url.path.startswith("/api/"):
            return Response(status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        await asyncio.sleep(1)  # מאט ניחוש סיסמאות
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    https = request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie("session", TOKEN, httponly=True, secure=https, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


class Message(BaseModel):
    role: str
    content: str


class DraftRequest(BaseModel):
    messages: list[Message]


@app.post("/api/draft")
async def api_draft(req: DraftRequest):
    msgs = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "ההודעה האחרונה חייבת להיות של המשתמש")
    return await run_in_threadpool(draft.draft, msgs)


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


@app.get("/admin/sync", response_class=HTMLResponse)
def sync_log():
    with store.connect() as conn:
        store.init(conn)
        runs = store.recent_runs(conn)
    rows = "".join(
        f"<tr><td>{r['started_at']:%Y-%m-%d %H:%M}</td><td>{r['added']}</td><td>{r['updated']}</td>"
        f"<td>{r['removed']}</td><td>{r['ocr']}</td><td>{r['failed']}</td>"
        f"<td>{'<br>'.join(_esc(e['file'] + ': ' + e['error']) for e in r['errors'] or [])}</td></tr>"
        for r in runs
    )
    return f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><title>לוג סנכרון</title>
<style>body{{font-family:system-ui,Arial;margin:24px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px 10px;text-align:right}}</style>
<h1>לוג סנכרון</h1><table><tr><th>זמן (UTC)</th><th>נוספו</th><th>עודכנו</th><th>הוסרו</th><th>OCR</th><th>נכשלו</th><th>שגיאות</th></tr>{rows}</table></html>"""


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
