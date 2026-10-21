"""שרת הווב: ניסוח הסכמים וייצוא ל-Word. האתר פתוח לכל מי שיש לו את הקישור.

uvicorn app.main:app --reload
"""
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from app import docx_export, draft, store  # noqa: E402

STATIC = Path(__file__).parent / "static"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # לוגו ו-favicon


@app.get("/healthz")
def healthz():
    return {"ok": True}


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


@app.get("/api/status")
def api_status():
    """שורת הסטטוס בראש האתר. אם המסד לא זמין, הממשק פשוט לא מציג אותה."""
    with store.connect() as conn:
        return store.status(conn)


@app.get("/admin/sync", response_class=HTMLResponse)
def sync_log():
    with store.connect() as conn:
        store.init(conn)
        runs = store.recent_runs(conn)
    rows = "".join(
        f"<tr><td>{r['started_at']:%Y-%m-%d %H:%M}</td><td>{r['added']}</td><td>{r['updated']}</td>"
        f"<td>{r['removed']}</td><td>{r['ocr']}</td><td>{r['failed']}</td>"
        f"<td>{'<br>'.join(_esc(n) for n in (r['files'] or {}).get('added', [])) or '—'}</td>"
        f"<td>{'<br>'.join(_esc(e['file'] + ': ' + e['error']) for e in r['errors'] or [])}</td></tr>"
        for r in runs
    )
    return f"""<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><title>לוג סנכרון</title>
<style>body{{font-family:system-ui,Arial;margin:24px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px 10px;text-align:right}}</style>
<h1>לוג סנכרון</h1><table><tr><th>זמן (שעון ישראל)</th><th>נוספו</th><th>עודכנו</th><th>הוסרו</th><th>OCR</th><th>נכשלו</th><th>קבצים שנוספו</th><th>שגיאות</th></tr>{rows}</table></html>"""


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
