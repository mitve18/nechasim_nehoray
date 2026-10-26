"""שרת הווב: ניסוח הסכמים, ייצוא ל-Word, פאנל קבצים ודוחות.

כל האתר מאחורי כניסה אישית (אימייל וסיסמה, ראה app/auth.py), ועוגייה שזוכרת את המשתמש ל-30 יום.
פתוחים בלי כניסה רק /login, /register, /healthz (בדיקת התקינות של Render) והקבצים הסטטיים.
בלי SECRET_KEY האתר חסום ("לא מוגדר"), כדי שלא ייפתח לכולם בטעות.
טיוטות ושיחות שייכות למשתמש. ארכיון ההסכמים, הסנכרון וההעלאה משותפים. עלויות, לוג סנכרון וניהול משתמשים: admin.

הרשמה עצמית: כתובת מדומיין ב-SIGNUP_DOMAINS נפתחת מיד, וכל כתובת אחרת ממתינה לאישור של admin.

uvicorn app.main:app --reload
"""
import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

from app import ask, auth, docx_export, draft, drive, github, store, xlsx_export  # noqa: E402

STATIC = Path(__file__).parent / "static"
SIGNUP_DOMAINS = {d.strip().lower().lstrip("@") for d in os.environ.get("SIGNUP_DOMAINS", "").split(",") if d.strip()}

@asynccontextmanager
async def lifespan(app):
    """מיגרציות פעם אחת בהפעלת השרת, כדי שטבלאות חדשות (כמו files) יהיו קיימות עוד לפני הסנכרון הראשון.
    רצות ברקע: uvicorn פותח את הפורט רק אחרי ה-startup, ומסד איטי או נעול חסם את העלייה ב-Render (Port scan timeout)."""
    task = asyncio.create_task(asyncio.to_thread(_migrate))
    yield
    task.cancel()


def _migrate():
    try:
        with store.connect() as conn:
            store.init(conn)
            _bootstrap_admin(conn)
        log.info("המיגרציות הסתיימו")
    except Exception as e:  # מסד לא זמין לא מונע מהאתר לעלות
        log.warning(f"המיגרציות בהפעלת השרת נכשלו: {e}")


def _bootstrap_admin(conn):
    """admin ראשון מ-ADMIN_EMAIL ו-ADMIN_PASSWORD, רק כשעוד אין admin במסד. אחרי שנוצר, אפשר למחוק את המשתנים."""
    email, password = auth.clean_email(os.environ.get("ADMIN_EMAIL", "")), os.environ.get("ADMIN_PASSWORD", "")
    if not (email and password) or not store.needs_admin(conn):
        return
    if problem := auth.password_problem(password):
        log.warning(f"ADMIN_PASSWORD לא תקינה ({problem}). ה-admin לא נוצר")
        return
    store.bootstrap_admin(conn, email, email.split("@")[0], auth.hash_password(password))
    log.info(f"נוצר admin: {email}")


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # לוגו ו-favicon


def _session_user(request):
    """המשתמש המחובר: עוגייה חתומה בתוקף, ומשתמש פעיל במסד עם אותו session_version."""
    parsed = auth.parse(request.cookies.get("session"))
    if not parsed:
        return None
    with store.connect() as conn:
        return store.user_for_session(conn, *parsed)


def admin(request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "הפעולה זמינה רק למנהל")


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",  # האתר לא נטען בתוך אתר אחר (clickjacking)
    # בלי script-src: index.html בנוי על סקריפטים inline. מה שכאן לא שובר כלום
    "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
OPEN_PATHS = ("/login", "/register", "/healthz")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """כל דף ו-API דורשים משתמש מחובר. המשתמש נשמר ב-request.state.user לשימוש ה-endpoints."""
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not auth.SECRET:  # בלי מפתח חתימה אין כניסה, ולא פותחים את האתר לכולם בטעות
        return HTMLResponse("<h1 dir='rtl'>האתר לא מוגדר: חסר SECRET_KEY ב-Render ← Environment</h1>", 503)
    try:
        request.state.user = await run_in_threadpool(_session_user, request)
    except Exception:
        log.exception("בדיקת המשתמש נכשלה")
        return JSONResponse({"detail": "מסד הנתונים לא זמין כרגע. נסה שוב בעוד דקה"}, 503)
    if request.state.user:
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "נדרשת כניסה"}, 401)
    return RedirectResponse("/login?next=" + quote(path), 303)


@app.middleware("http")
async def same_origin(request: Request, call_next):
    """CSRF: בקשה שמשנה משהו ונשלחה מאתר אחר נחסמת. דפדפנים שולחים Origin בכל POST, PUT, PATCH ו-DELETE.
    בנוסף ל-SameSite=Lax של העוגייה."""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None \
            and urlparse(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "בקשה ממקור לא מורשה"}, 403)
    return await call_next(request)


@app.middleware("http")  # נרשם אחרון ולכן עוטף את השאר: הכותרות נוספות גם להפניות, ל-401 ול-403
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update(SECURITY_HEADERS)
    if request.headers.get("x-forwarded-proto") == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def _safe_next(target):
    """חזרה לדף שביקשו, רק בתוך האתר: לא "//אתר-אחר" ולא כתובת מלאה."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else "/"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
@app.get("/register")
def login_page():
    return FileResponse(STATIC / "login.html")  # אותו דף לכניסה ולהרשמה


LOGIN_FAILS, LOGIN_WINDOW = 10, 15 * 60
REGISTRATIONS = 5  # הרשמות לכל IP ב-LOGIN_WINDOW
# ponytail: בזיכרון התהליך, מתאפס בהפעלה מחדש. מספיק לשרת אחד ב-Render
_login_fails = defaultdict(deque)  # IP, "email:..." או "reg:IP" ← זמני הניסיונות ב-LOGIN_WINDOW האחרון


def _client_ip(request):
    """ה-IP האחרון ב-X-Forwarded-For הוא זה שהפרוקסי של Render הוסיף. את הראשונים הדפדפן יכול לזייף.
    ponytail: אם Render יוסיף עוד פרוקסי בדרך, כל המשתמשים יחלקו מגבלה אחת. אז לעבור לכותרת ש-Render מתעד"""
    return request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or request.client.host


def _attempts(key, now):
    times = _login_fails[key]
    while times and times[0] <= now - LOGIN_WINDOW:
        times.popleft()
    return times


async def _form(request):
    """טופס HTML רגיל (בלי תלות ב-python-multipart)."""
    form = parse_qs((await request.body()).decode())
    return lambda name: form.get(name, [""])[0]


def _login_response(request, target, user_id, version):
    resp = RedirectResponse(target, 303)
    resp.set_cookie("session", auth.cookie(user_id, version), httponly=True, samesite="lax",
                    secure=request.headers.get("x-forwarded-proto") == "https",
                    max_age=auth.SESSION_DAYS * 24 * 3600)
    return resp


def _check_login(email, password):
    """המשתמש אם הסיסמה נכונה, אחרת None. bcrypt רץ גם כשאין משתמש כזה (auth.check_password)."""
    with store.connect() as conn:
        user = store.user_for_login(conn, email)
        if not auth.check_password(password, user and user["password_hash"]):
            return None
        if user["active"]:
            store.touch_login(conn, user["id"])
        return user


@app.post("/login")
async def login(request: Request):
    """email, password ו-next. עד LOGIN_FAILS ניסיונות כושלים לכל IP ולכל כתובת ב-15 דקות.
    השהייה לבדה לא עוצרת ניסיונות שנשלחים במקביל."""
    field = await _form(request)
    email, password, target = auth.clean_email(field("email")) or "", field("password"), _safe_next(field("next") or "/")
    ip, now = _client_ip(request), time.time()
    tries = [_attempts(ip, now), _attempts("email:" + email, now)]
    if any(len(t) >= LOGIN_FAILS for t in tries):
        return RedirectResponse("/login?error=locked&next=" + quote(target), 303)
    user = await run_in_threadpool(_check_login, email, password) if email else None
    if not user:
        for t in tries:
            t.append(now)
        await asyncio.sleep(1)  # מאט ניחוש סיסמאות
        return RedirectResponse("/login?error=1&next=" + quote(target), 303)
    if not user["active"]:  # רק אחרי סיסמה נכונה, כך שזה לא מגלה אילו כתובות רשומות
        return RedirectResponse("/login?error=pending", 303)
    _login_fails.pop(ip, None)
    _login_fails.pop("email:" + email, None)
    return _login_response(request, target, user["id"], user["session_version"])


@app.post("/register")
async def register(request: Request):
    """הרשמה עצמית: email, name ו-password. כתובת מ-SIGNUP_DOMAINS נכנסת מיד; כל אחת אחרת ממתינה ל-admin."""
    field = await _form(request)
    email, name, password = auth.clean_email(field("email")), field("name").strip()[:100], field("password")
    tries = _attempts("reg:" + _client_ip(request), time.time())
    if len(tries) >= REGISTRATIONS:
        return RedirectResponse("/register?error=locked", 303)
    if not email:
        return RedirectResponse("/register?error=email", 303)
    if not name:
        return RedirectResponse("/register?error=name", 303)
    if auth.password_problem(password):
        return RedirectResponse("/register?error=password", 303)
    tries.append(time.time())
    active = email.rsplit("@", 1)[1] in SIGNUP_DOMAINS

    def create():
        with store.connect() as conn:
            return store.create_user(conn, email, name, auth.hash_password(password), active=active)

    user_id = await run_in_threadpool(create)
    if not user_id:
        return RedirectResponse("/register?error=exists", 303)
    log.info(f"משתמש נרשם: {email} ({'פעיל' if active else 'ממתין לאישור'})")
    return _login_response(request, "/", user_id, 1) if active else RedirectResponse("/login?registered=1", 303)


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie("session")
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/me")
def me(request: Request):
    """מי מחובר, ומה הממשק מציג: העלאה (לכל מחובר), סנכרון (כשהוא מוגדר), עלויות וניהול (admin)."""
    return {"writer": True, "sync": github.configured(), "user": request.state.user}


class Message(BaseModel):
    role: str
    content: str


class DraftRequest(BaseModel):
    messages: list[Message]
    focus_file_ids: list[str] = []  # "נסח על בסיס המסמך הזה", אפשר כמה מסמכים
    conversation: str | None = Field(None, max_length=64)  # מזהה השיחה, לחיבור העלות לטיוטה
    web_check: bool = False  # "בדיקת דרישות חוק עדכניות באינטרנט"
    deep_review: bool = False  # "בדיקה משפטית מעמיקה"
    revise: bool = False  # תיקון בצ'אט להסכם שכבר נוסח: ישר למנסח, בלי חיפוש ושאלות (draft.draft)


DRAFTS_PER_HOUR = 30
DAILY_DRAFT_LIMIT = int(os.environ.get("DAILY_DRAFT_LIMIT", "200"))
# ponytail: בזיכרון התהליך, מתאפס בהפעלה מחדש ולא משותף בין תהליכים. מספיק לשרת אחד ב-Render
_drafts = defaultdict(deque)  # user_id ← זמני הניסוחים בשעה האחרונה
_daily = {"date": None, "count": 0}


def _draft_quota(user_id):
    """עד DRAFTS_PER_HOUR ניסוחים בשעה לכל משתמש, ו-DAILY_DRAFT_LIMIT לכל האתר ביום (UTC). מעבר לזה 429."""
    now, today = time.time(), date.today()
    if _daily["date"] != today:
        _daily.update(date=today, count=0)
    if _daily["count"] >= DAILY_DRAFT_LIMIT:
        raise HTTPException(429, "הגענו לתקרת הניסוחים היומית של האתר. אפשר לנסות שוב מחר")
    times = _drafts[user_id]
    while times and times[0] <= now - 3600:
        times.popleft()
    if len(times) >= DRAFTS_PER_HOUR:
        wait = int(times[0] + 3600 - now) // 60 + 1
        raise HTTPException(429, f"הגעת למגבלה של {DRAFTS_PER_HOUR} ניסוחים בשעה. אפשר לנסות שוב בעוד {wait} דקות")
    times.append(now)
    _daily["count"] += 1


DRAFT_FAILED = "הניסוח נכשל: שירות הניסוח לא הגיב או החזיר שגיאה. נסה שוב בעוד כמה דקות"


@app.post("/api/draft")
async def api_draft(req: DraftRequest, request: Request):
    msgs = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "ההודעה האחרונה חייבת להיות של המשתמש")
    user_id = request.state.user["id"]
    _draft_quota(user_id)
    try:  # מסמכי הבסיס נבדקים כאן, לפני ההזרמה, כדי שמסמך חסר יחזיר 404 ולא 200 עם שגיאה
        events = await run_in_threadpool(draft.stream, msgs, req.focus_file_ids, req.conversation, req.web_check,
                                          req.deep_review, req.revise, user_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception:  # הפרטים ללוג של השרת בלבד, לא לממשק
        log.exception("הניסוח נכשל")
        raise HTTPException(502, DRAFT_FAILED)

    def ndjson():
        """שורת JSON לכל אירוע (draft.stream). הסטטוס 200 כבר נשלח, ולכן שגיאה באמצע היא אירוע error.
        Starlette מריץ generator רגיל ב-threadpool, כך שהקריאות למודל לא חוסמות את השרת."""
        try:
            for ev in events:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except draft.Truncated as e:
            yield json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n"
        except Exception:
            log.exception("הניסוח נכשל")
            yield json.dumps({"type": "error", "message": DRAFT_FAILED}, ensure_ascii=False) + "\n"

    # בלי no-cache ו-X-Accel-Buffering, פרוקסי בדרך (Render) עלול לצבור את כל התשובה ולשלוח אותה רק בסוף
    return StreamingResponse(ndjson(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class AskRequest(BaseModel):
    messages: list[Message] = Field(max_length=40)
    conversation: str | None = Field(None, max_length=64)
    chat_id: int | None = None  # המשך של שיחה שמורה. None: שיחה חדשה, שנשמרת אחרי התשובה הראשונה


ASK_HISTORY = 40  # הודעות קודמות שנשלחות למודל משיחה שמורה


def _ask_history(user_id, chat_id):
    with store.connect() as conn:
        chat = store.get_chat(conn, user_id, chat_id)
    if not chat:
        raise HTTPException(404, "השיחה לא נמצאה. ייתכן שהיא נמחקה")
    return chat["messages"]


def _save_chat(user_id, chat_id, messages, conversation):
    """None אם השמירה נכשלה: התשובה עדיין מגיעה לממשק, רק בלי היסטוריה."""
    try:
        with store.connect() as conn:
            return store.save_chat(conn, user_id, chat_id, messages[0]["content"][:80], messages, conversation)
    except Exception:
        log.exception("שמירת השיחה נכשלה")
        return None


@app.post("/api/ask")
async def api_ask(req: AskRequest, request: Request):
    """עמוד "שאלו על המסמכים". אותה מכסה כמו הניסוח, כי גם כאן כל שאלה היא קריאה למודל.
    בשיחה שמורה ההיסטוריה נלקחת מהמסד (של המשתמש בלבד) ולא מהדפדפן; מהבקשה נלקחת רק השאלה החדשה."""
    msgs = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "ההודעה האחרונה חייבת להיות שאלה")
    user_id = request.state.user["id"]
    history = await run_in_threadpool(_ask_history, user_id, req.chat_id) if req.chat_id else msgs[:-1]
    _draft_quota(user_id)
    context = [{"role": m["role"], "content": m["content"]} for m in history[-ASK_HISTORY:]] + msgs[-1:]
    try:
        r = await run_in_threadpool(ask.ask, context, req.conversation, user_id)
    except Exception:
        log.exception("התשובה נכשלה")
        raise HTTPException(502, "לא הצלחתי לענות: שירות המודל לא הגיב או החזיר שגיאה. נסה שוב בעוד כמה דקות")
    saved = history + msgs[-1:] + [{"role": "assistant", "content": r["answer"], "citations": r["citations"]}]
    return r | {"chat_id": await run_in_threadpool(_save_chat, user_id, req.chat_id, saved, req.conversation)}


@app.get("/api/chats")
def api_chats(request: Request):
    """רשימת השיחות של המשתמש בצד המסך, מהאחרונה לראשונה."""
    with store.connect() as conn:
        return store.chats_list(conn, request.state.user["id"])


@app.get("/api/chats/{chat_id}")
def api_chat(chat_id: int, request: Request):
    with store.connect() as conn:
        chat = store.get_chat(conn, request.state.user["id"], chat_id)
    if not chat:
        raise HTTPException(404, "השיחה לא נמצאה. ייתכן שהיא נמחקה")
    return chat


@app.delete("/api/chats/{chat_id}")
def api_chat_delete(chat_id: int, request: Request):
    with store.connect() as conn:
        if not store.delete_chat(conn, request.state.user["id"], chat_id):
            raise HTTPException(404, "השיחה לא נמצאה")
    return {"ok": True}


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


def _days(days):
    if days not in store.COST_DAYS:
        raise HTTPException(400, "טווח לא נתמך")
    return days


@app.get("/api/costs", dependencies=[Depends(admin)])
def api_costs(days: int = 7, user: int | None = None):
    """עמוד "עלויות". מ-SQL בלבד, בלי קריאה למודל. user: רק העלויות של משתמש אחד; בלעדיו כל המשתמשים."""
    with store.connect() as conn:
        return store.costs(conn, _days(days), user)


@app.get("/api/costs/xlsx", dependencies=[Depends(admin)])
def api_costs_xlsx(days: int = 7, user: int | None = None):
    with store.connect() as conn:
        c = store.costs(conn, _days(days), user)
    name = quote(f"עלויות {date.today():%d-%m-%Y}.xlsx")
    return Response(
        xlsx_export.build_costs(c),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@app.get("/api/history")
def api_history(request: Request):
    """עמוד "טיוטות": הטיוטות של המשתמש, מהחדשה לישנה."""
    with store.connect() as conn:
        return store.drafts_list(conn, request.state.user["id"])


@app.post("/api/history")
def api_history_save(req: HistoryRequest, request: Request):
    """נקרא אחרי כל ניסוח ובזמן עריכה (בהשהיה), כך שההסכם בהיסטוריה תמיד בגרסה האחרונה שלו."""
    with store.connect() as conn:
        return {"id": store.save_draft(conn, request.state.user["id"], req.id, req.title, req.html, req.text,
                                       req.messages, req.sources, req.conversation)}


@app.get("/api/history/{draft_id}")
def api_history_get(draft_id: int, request: Request):
    with store.connect() as conn:
        d = store.get_draft(conn, request.state.user["id"], draft_id)
    if not d:
        raise HTTPException(404, "ההסכם לא נמצא. ייתכן שהוא נמחק")
    return d


class RatingRequest(BaseModel):
    rating: int = Field(ge=-1, le=1)


@app.put("/api/history/{draft_id}/rating")
def api_history_rate(draft_id: int, req: RatingRequest, request: Request):
    """המשוב "מרוצה מהנוסח?" מתחת לשיחה. אופציונלי, ואפשר לשנות אותו."""
    with store.connect() as conn:
        if not store.rate_draft(conn, request.state.user["id"], draft_id, req.rating):
            raise HTTPException(404, "ההסכם לא נמצא")
    return {"ok": True}


@app.delete("/api/history/{draft_id}")
def api_history_delete(draft_id: int, request: Request):
    with store.connect() as conn:
        if not store.delete_draft(conn, request.state.user["id"], draft_id):
            raise HTTPException(404, "ההסכם לא נמצא")
    return {"ok": True}


# ---- ניהול משתמשים (admin) ----

class NewUser(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(max_length=200)
    role: Literal["admin", "user"] = "user"


class UserChange(BaseModel):
    active: bool | None = None  # False: השבתה, True: אישור משתמש שנרשם או הפעלה מחדש
    role: Literal["admin", "user"] | None = None
    password: str | None = Field(None, max_length=200)  # סיסמה חדשה למי ששכח


def _new_password(password):
    if problem := auth.password_problem(password):
        raise HTTPException(400, problem)
    return auth.hash_password(password)


@app.get("/admin/users", dependencies=[Depends(admin)])
def admin_users_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/api/admin/users", dependencies=[Depends(admin)])
def api_users():
    with store.connect() as conn:
        return store.users_list(conn)


@app.post("/api/admin/users", dependencies=[Depends(admin)])
def api_user_create(req: NewUser):
    email = auth.clean_email(req.email)
    if not email:
        raise HTTPException(400, "כתובת האימייל לא תקינה")
    hashed = _new_password(req.password)
    with store.connect() as conn:
        user_id = store.create_user(conn, email, req.name.strip(), hashed, req.role)
    if not user_id:
        raise HTTPException(409, "הכתובת הזו כבר רשומה")
    return {"id": user_id}


@app.patch("/api/admin/users/{user_id}", dependencies=[Depends(admin)])
def api_user_update(user_id: int, req: UserChange, request: Request):
    """השבתה, אישור, תפקיד או סיסמה חדשה. כל שינוי מנתק את המשתמש מכל הדפדפנים (session_version)."""
    if user_id == request.state.user["id"] and (req.active is False or req.role == "user"):
        raise HTTPException(400, "אי אפשר להשבית את עצמך או להסיר ממך את הרשאות המנהל")
    hashed = _new_password(req.password) if req.password is not None else None
    with store.connect() as conn:
        if not store.update_user(conn, user_id, req.active, req.role, hashed):
            raise HTTPException(404, "המשתמש לא נמצא")
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(admin)])
def api_user_reject(user_id: int):
    """דחיית בקשת הרשמה שממתינה לאישור. הכתובת יכולה להירשם שוב, ושוב תמתין לאישור."""
    with store.connect() as conn:
        if not store.reject_user(conn, user_id):
            raise HTTPException(404, "אין בקשת הרשמה ממתינה של המשתמש הזה")
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


@app.get("/api/status/xlsx")
def api_status_xlsx():
    with store.connect() as conn:
        rows = store.status_rows(conn)
    return Response(
        xlsx_export.build_table(STATUS_TITLE, STATUS_HEADER, [[r["label"], r["value"]] for r in rows]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(STATUS_TITLE + '.xlsx')}"},
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
    except Exception:  # הפרטים (תשובת GitHub) ללוג בלבד
        log.exception("הפעלת הסנכרון נכשלה")
        return {"started": False, "error": "הפעלת הסנכרון נכשלה. נסה שוב בעוד כמה דקות"}


@app.post("/api/files/{file_id}/retry")
def api_retry(file_id: str):
    with store.connect() as conn:
        name = store.mark_retry(conn, file_id)
    if name is None:
        raise HTTPException(404, "הקובץ לא נמצא")
    return {"name": name, "sync": _sync(defer=True)}


@app.post("/api/sync")
def api_sync():
    if not github.configured():
        raise HTTPException(404, "הסנכרון מהאתר לא מוגדר")
    try:
        return github.trigger()
    except Exception:
        log.exception("הפעלת הסנכרון נכשלה")
        raise HTTPException(502, "הפעלת הסנכרון נכשלה. נסה שוב בעוד כמה דקות")


@app.get("/api/sync")
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


@app.post("/api/upload")
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


@app.get("/admin/sync", response_class=HTMLResponse, dependencies=[Depends(admin)])
def sync_log():
    with store.connect() as conn:  # המיגרציות כבר רצו בהפעלת השרת (lifespan)
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
