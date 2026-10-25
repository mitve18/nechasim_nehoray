"""מאגר ההסכמים ב-Postgres (Neon) עם pgvector. embeddings דרך OpenAI, תקציר ונתונים מובנים דרך Agent של Agno."""
import atexit
import json
import logging
import os
import threading
from datetime import date
from typing import Literal

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from openai import OpenAI
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from app.extract import chunk

log = logging.getLogger(__name__)
EMBED_MODEL = "text-embedding-3-small"
SUMMARY_MODEL = "gpt-4o"
METADATA_VERSION = 1  # מעלים כשמוסיפים שדות ל-DocInfo; הסנכרון הבא משלים אותם לכל המסמכים הקיימים
CHUNK_VERSION = 1  # מעלים כשמשנים את extract.chunk; הסנכרון הבא חותך מחדש את כל המאגר מהטקסט השמור, בלי OCR

SCHEMA = """
create extension if not exists vector;
create table if not exists agreements (
    file_id text primary key,
    name text not null,
    url text,
    modified_time text not null,
    method text not null,
    summary text not null,
    text text not null,
    embedding vector(1536) not null,
    indexed_at timestamptz not null default now()
);
create table if not exists sync_runs (
    id serial primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    added int, updated int, removed int, ocr int, failed int,
    errors jsonb
);
-- מיגרציות. כולן "if not exists": בטוחות להרצה חוזרת ולא נוגעות בנתונים קיימים
alter table sync_runs add column if not exists files jsonb;
alter table agreements add column if not exists metadata jsonb;
alter table agreements add column if not exists end_date date;
alter table agreements add column if not exists metadata_version int;
-- מספר העמודים האמיתי מהקובץ (PDF ותמונות). null = עוד לא נספר; הסנכרון הבא סופר למסמכים הקיימים
alter table agreements add column if not exists pages int;
-- איכות ה-OCR (0–100) למסמכים סרוקים. null במסמך דיגיטלי, או בסרוק שעוד לא קיבל ציון (הסנכרון הבא משלים)
alter table agreements add column if not exists ocr_quality int;
-- md5 של התוכן מ-Drive: קובץ ש-modifiedTime שלו השתנה אבל התוכן זהה (שינוי שם) לא עובר OCR ומודל שוב
alter table agreements add column if not exists md5 text;
create table if not exists files (
    file_id text primary key,
    name text not null,
    mime_type text,
    size_bytes bigint,
    modified_time text,
    status text not null check (status in ('indexed', 'failed', 'queued', 'unsupported')),
    error text,
    last_attempt_at timestamptz,
    retry_requested boolean not null default false
);
-- היסטוריית ההסכמים שנוסחו באתר: ה-HTML מהעורך (עם העיצוב), הטקסט הנקי לתצוגה מקדימה, והשיחה
create table if not exists drafts (
    id bigserial primary key,
    title text not null,
    html text not null,
    text text not null,
    messages jsonb not null,
    sources jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
-- מזהה השיחה שבה הטיוטה נוצרה. דרכו מחברים לטיוטה את העלויות מ-api_usage, כולל סבבי שאלות ותיקונים
alter table drafts add column if not exists conversation text;
-- המשוב של העובד על הטיוטה: 1 מרוצה, 0 בערך, 1- לא מרוצה. משמש את search לדירוג מסמכי המאגר
alter table drafts add column if not exists rating smallint check (rating between -1 and 1);
-- כל קריאה ל-OpenAI (ניסוח, חיפוש, סנכרון) עם הטוקנים והעלות בדולרים, לעמוד "עלויות"
create table if not exists api_usage (
    id bigserial primary key,
    at timestamptz not null default now(),
    kind text not null,
    model text not null,
    input_tokens int not null,
    cached_tokens int not null,
    output_tokens int not null,
    cost_usd numeric(12, 6) not null,
    conversation text
);
-- 'ask': שאלות על המסמכים. מסד שנוצר לפני כן מקבל את הסוג החדש בבלוק הבא
-- רק כשה-constraint עוד ישן: alter table נועל את הטבלה, ולא צריך לנעול אותה בכל הפעלה
do $$ begin
    if not exists (select 1 from pg_constraint
                   where conname = 'api_usage_kind_check' and pg_get_constraintdef(oid) like '%ask%') then
        alter table api_usage drop constraint if exists api_usage_kind_check;
        alter table api_usage add constraint api_usage_kind_check check (kind in ('draft', 'sync', 'ask'));
    end if;
end $$;
-- המסמכים כקטעים לחיפוש (extract.chunk): embedding לכל קטע, ו-tsv למילים מדויקות (גוש, מספר תוכנית, שם צד).
-- 'simple' ולא שפה: ל-Postgres אין stemmer לעברית. את הצורות (בהסכם/הסכם) מכסה החיפוש הווקטורי
create table if not exists chunks (
    file_id text not null references agreements on delete cascade,
    n int not null,
    section text,
    page int,
    text text not null,
    embedding vector(1536) not null,
    tsv tsvector generated always as (to_tsvector('simple', text)) stored,
    primary key (file_id, n)
);
create index if not exists chunks_embedding on chunks using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_tsv on chunks using gin (tsv);
-- הגרסה של החיתוך. null = עוד לא נחתך; הסנכרון הבא משלים מהטקסט השמור
alter table agreements add column if not exists chunk_version int;
"""
TZ = "Asia/Jerusalem"  # ההמרה נעשית ב-Postgres, שיש לו את טבלאות אזורי הזמן גם כשאין tzdata ב-Windows
PENDING = "(metadata is null or coalesce(metadata_version, 0) < %s)"  # מסמך שעוד צריך השלמת נתונים מובנים


_pool, _pool_lock = None, threading.Lock()


def connect():
    """חיבור מה-pool, ל-with. חיבור חדש ל-Neon לוקח 100–300ms, וכך רק הבקשה הראשונה משלמת עליו.
    check: Neon סוגר חיבורים כשהמסד נרדם, אז כל חיבור נבדק לפני שהוא יוצא מה-pool."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5, max_idle=240,
                                   kwargs={"autocommit": True, "connect_timeout": 15},
                                   check=ConnectionPool.check_connection)
            atexit.register(_pool.close)  # בלי זה sync.py מחכה בסיום לתהליכוני ה-pool
    return _pool.connection(timeout=15)


def init(conn):
    # בלי זה מיגרציה שמחכה לנעילה (שרת ישן, סנכרון באמצע) מחכה לנצח. כך היא נכשלת, נרשמת בלוג, ותרוץ בהפעלה הבאה
    conn.execute("set lock_timeout = '10s'")
    conn.execute(SCHEMA)
    conn.execute("set lock_timeout = 0")


def _dicts(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


# דולר למיליון טוקנים: (קלט, קלט מהמטמון, פלט). מהמחירון של OpenAI, נבדק 10/2026. מחליפים מודל? מוסיפים אותו כאן
PRICES = {"gpt-4o": (2.50, 1.25, 10.00), "gpt-5.5": (5.00, 0.50, 30.00), "gpt-4.1-mini": (0.40, 0.10, 1.60),
          "text-embedding-3-small": (0.02, 0.02, 0.0),
          "web_search": (25_000, 0, 0)}  # web_search: היחידה היא חיפוש ולא טוקן ($25 לאלף חיפושים)


def cost(model, input_tokens, cached_tokens=0, output_tokens=0):
    """input_tokens כולל את הטוקנים מהמטמון (כמו ב-OpenAI), והם מחויבים במחיר המוזל."""
    p_in, p_cached, p_out = PRICES[model]
    return ((input_tokens - cached_tokens) * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1e6


def agent_usage(result, model):
    """השימוש של ריצת Agent (כל הקריאות למודל בה, גם אחרי tool) כשורה ל-log_usage, או None."""
    m = getattr(result, "metrics", None)
    return (model, m.input_tokens or 0, m.cache_read_tokens or 0, m.output_tokens or 0) if m else None


def log_usage(conn, kind, usage, conversation=None):
    """usage: [(model, input_tokens, cached_tokens, output_tokens)]. kind: 'draft' (באתר) או 'sync'."""
    rows = [(kind, *u, cost(*u), conversation) for u in usage if u]
    if rows:
        with conn.cursor() as cur:
            cur.executemany("insert into api_usage (kind, model, input_tokens, cached_tokens, output_tokens, "
                            "cost_usd, conversation) values (%s, %s, %s, %s, %s, %s, %s)", rows)


def record(kind, usage, conversation=None):
    """log_usage בחיבור משלו, לאתר. מסד לא זמין לא מכשיל את הניסוח או התשובה; רק העלות הזו לא תירשם."""
    try:
        with connect() as conn:
            log_usage(conn, kind, usage, conversation)
    except Exception as e:
        log.warning(f"רישום העלות נכשל: {e!r}")


def embed(text, usage=None):
    return embed_many([text], usage)[0]


EMBED_BATCH = 100  # טקסטים בקריאה אחת: 100 קטעים של עד 2,000 תווים, הרבה מתחת לתקרת הטוקנים של בקשה


def embed_many(texts, usage=None):
    """embeddings לכמה טקסטים, בכמה שפחות קריאות. מחזיר מחרוזות בפורמט של pgvector, באותו סדר."""
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        r = OpenAI().embeddings.create(model=EMBED_MODEL, input=[t[:20000] for t in texts[i:i + EMBED_BATCH]])
        if usage is not None:
            usage.append((EMBED_MODEL, r.usage.prompt_tokens, 0, 0))
        out += ["[" + ",".join(map(str, d.embedding)) + "]" for d in r.data]
    return out


class Amount(BaseModel):
    amount: float | None = Field(description="הסכום כמספר, למשל 2 מיליון = 2000000")
    currency: str | None = Field(description="ILS / USD / EUR")


class DocInfo(BaseModel):
    summary: str
    doc_type: Literal["הסכם", "כתב התחייבות", "עמדה", "מכתב", "אחר"] | None
    parties: list[str] | None
    plan_number: str | None = Field(description='מספר תוכנית כפי שהוא כתוב, למשל תת"ל 120')
    location: str | None
    gush_helka: list[str] | None = Field(description="כל גוש וחלקה כמחרוזת, למשל 'גוש 6123 חלקה 45'")
    start_date: str | None = Field(description="YYYY-MM-DD")
    end_date: str | None = Field(description="YYYY-MM-DD")
    amounts: list[Amount] | None
    pages: int | None


ANALYZE_INSTRUCTIONS = [
    "summary: תקציר בעברית של 3–4 משפטים למסמך המשפטי: סוג המסמך, הצדדים, התחום המקצועי, "
    "והנושאים והסעיפים העיקריים. בלי הקדמות.",
    "בשאר השדות: רק מה שכתוב במסמך במפורש. שדה שלא מופיע במסמך הוא null. אל תנחש ואל תסיק.",
    "תאריכים בפורמט YYYY-MM-DD. כשכתוב רק חודש ושנה: start_date הוא היום הראשון בחודש, "
    "ו-end_date הוא היום האחרון בחודש.",
    "pages: רק אם מספר העמודים כתוב במסמך עצמו.",
]


def analyze(name, text, usage=None):
    """קריאה אחת למודל: התקציר (לחיפוש ולמקורות) והנתונים המובנים. מחזיר (summary, metadata)."""
    agent = Agent(model=OpenAIResponses(id=SUMMARY_MODEL), instructions=ANALYZE_INSTRUCTIONS,
                  output_schema=DocInfo)
    result = agent.run(f"שם הקובץ: {name}\n\n{text[:30000]}")
    if usage is not None:  # נרשם גם כשהתשובה לא תקינה: הקריאה כבר חויבה
        usage.append(agent_usage(result, SUMMARY_MODEL))
    if result.status != RunStatus.completed or not isinstance(result.content, DocInfo):
        raise RuntimeError(f"התקציר נכשל: status={result.status}")  # הקובץ לא נשמר, ולכן ינוסה שוב בריצה הבאה
    meta = result.content.model_dump()
    return meta.pop("summary").strip(), meta


def _date(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def indexed(conn):
    """{file_id: modified_time} של מה שכבר במאגר."""
    return dict(conn.execute("select file_id, modified_time from agreements").fetchall())


def checksums(conn):
    """{file_id: md5} של מה שכבר במאגר. null במסמך מלפני העמודה, עד שהסנכרון הבא שומר אותו (touch)."""
    return dict(conn.execute("select file_id, md5 from agreements").fetchall())


def touch(conn, f):
    """קובץ שהתוכן שלו לא השתנה: שם, קישור, modified_time ו-md5 מ-Drive, בלי חילוץ, מודל ו-embedding."""
    conn.execute("update agreements set name = %s, url = %s, modified_time = %s, md5 = %s where file_id = %s",
                 (f["name"], f.get("webViewLink"), f["modifiedTime"], f.get("md5Checksum"), f["id"]))


def upsert(conn, f, method, text, pages=None, ocr_quality=None):
    usage = []
    try:
        _upsert(conn, f, method, text, pages, ocr_quality, usage)
    finally:
        log_usage(conn, "sync", usage)


def _upsert(conn, f, method, text, pages, ocr_quality, usage):
    summary, meta = analyze(f["name"], text, usage)
    if pages:  # מספר העמודים האמיתי מהקובץ עדיף על מה שכתוב בטקסט
        meta["pages"] = pages
    conn.execute(
        """insert into agreements (file_id, name, url, modified_time, method, summary, text, embedding,
                                   metadata, end_date, metadata_version, pages, ocr_quality, md5)
           values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s)
           on conflict (file_id) do update set name=excluded.name, url=excluded.url,
             modified_time=excluded.modified_time, method=excluded.method, summary=excluded.summary,
             text=excluded.text, embedding=excluded.embedding, metadata=excluded.metadata,
             end_date=excluded.end_date, metadata_version=excluded.metadata_version, pages=excluded.pages,
             ocr_quality=excluded.ocr_quality, md5=excluded.md5,
             indexed_at=now()""",
        (f["id"], f["name"], f.get("webViewLink"), f["modifiedTime"], method, summary, text,
         embed(f"{f['name']}\n{summary}", usage), Jsonb(meta), _date(meta.get("end_date")), METADATA_VERSION, pages,
         ocr_quality, f.get("md5Checksum")),
    )
    # אם החיתוך נכשל, המסמך כבר במאגר בלי קטעים (chunk_version null), והסנכרון הבא משלים אותו
    save_chunks(conn, f["id"], f["name"], text, usage)


def save_chunks(conn, file_id, name, text, usage=None):
    """מחליף את הקטעים של מסמך. השם והסעיף נכנסים ל-embedding, כדי שקטע כמו "התמורה תשולם תוך 30 יום"
    יימצא גם בחיפוש לפי סוג ההסכם או הצד."""
    parts = chunk(text)
    vectors = embed_many([f"{name} | {c['section']}\n{c['text']}" for c in parts], usage)
    with conn.transaction():
        conn.execute("delete from chunks where file_id = %s", (file_id,))
        with conn.cursor() as cur:
            cur.executemany("insert into chunks (file_id, n, section, page, text, embedding) "
                            "values (%s, %s, %s, %s, %s, %s::vector)",
                            [(file_id, n, c["section"], c["page"], c["text"], v)
                             for n, (c, v) in enumerate(zip(parts, vectors))])
        conn.execute("update agreements set chunk_version = %s where file_id = %s", (CHUNK_VERSION, file_id))


def pending_chunks(conn):
    """מסמכים שעוד לא נחתכו, או שנחתכו בגרסה ישנה של extract.chunk."""
    return _dicts(conn, "select file_id, name, text from agreements where coalesce(chunk_version, 0) < %s "
                        "order by name", (CHUNK_VERSION,))


def pending_metadata(conn):
    """מסמכים בלי נתונים מובנים או עם גרסה ישנה. מסמך שכבר הושלם לא נבחר, ולכן לא עולה כסף שוב."""
    return _dicts(conn, f"select file_id, name, text from agreements where {PENDING} order by name",
                  (METADATA_VERSION,))


def save_metadata(conn, file_id, meta):
    """השלמת נתונים מובנים בלבד. התקציר וה-embedding לא משתנים."""
    conn.execute("update agreements set metadata = %s, end_date = %s, metadata_version = %s where file_id = %s",
                 (Jsonb(meta), _date(meta.get("end_date")), METADATA_VERSION, file_id))


def delete(conn, file_ids):
    conn.execute("delete from agreements where file_id = any(%s)", (list(file_ids),))


def search(conn, query, k=3, usage=None, learn=False):
    """learn (בניסוח): מבין 3k המסמכים הקרובים, מסמך שטיוטות שנוסחו ממנו קיבלו משוב טוב עולה, ורע יורד."""
    # ponytail: עד 0.06 במרחק הקוסינוס (3 משובים נטו); לכייל אם המשוב דוחק מסמכים רלוונטיים
    rows = conn.execute(
        """with fb as (select s->>'name' as name, sum(d.rating) as score
                       from drafts d, jsonb_array_elements(d.sources) s where d.rating is not null group by 1)
           select a.file_id, a.name, a.url, a.summary, a.text
           from (select *, embedding <=> %s::vector as dist from agreements order by dist limit %s) a
           left join fb on fb.name = a.name
           order by a.dist - %s * greatest(-3, least(3, coalesce(fb.score, 0))) limit %s""",
        (embed(query, usage), k * 3, 0.02 if learn else 0, k),
    ).fetchall()
    return [dict(zip(("file_id", "name", "url", "summary", "text"), r)) for r in rows]


RRF_K = 60  # הקבוע המקובל של Reciprocal Rank Fusion: מקטין את המשקל של ההבדלים בין המקומות הראשונים
RRF_DEPTH = 40  # כמה תוצאות מכל אחד משני החיפושים נכנסות למיזוג


def search_chunks(conn, query, k=8, usage=None):
    """חיפוש היברידי בקטעים: קרבה וקטורית (משמעות) ומילים מדויקות (tsvector), ממוזגים ב-RRF.
    קטע שמופיע גבוה באחד מהם, או בשניהם, עולה. מחזיר [{file_id, name, url, section, page, text}].
    המילים מחוברות ב-OR ולא ב-AND (plainto_tsquery): בשאלה חופשית כמעט אף קטע לא מכיל את כל המילים."""
    return _dicts(conn, """
        with vec as (
            select file_id, n, row_number() over (order by d) as r
            from (select file_id, n, embedding <=> %(v)s::vector as d from chunks order by d limit %(depth)s) x),
        q as (select replace(plainto_tsquery('simple', %(q)s)::text, '&', '|')::tsquery as t),
        kw as (
            select file_id, n, row_number() over (order by rank desc) as r
            from (select file_id, n, ts_rank_cd(tsv, q.t) as rank from chunks, q where tsv @@ q.t
                  order by rank desc limit %(depth)s) x),
        fused as (
            select file_id, n, sum(1.0 / (%(rrf)s + r)) as score
            from (select * from vec union all select * from kw) u group by file_id, n)
        select c.file_id, a.name, a.url, c.section, c.page, c.text
        from fused join chunks c using (file_id, n) join agreements a using (file_id)
        order by fused.score desc, c.file_id, c.n limit %(k)s""",
        {"v": embed(query, usage), "q": query, "depth": RRF_DEPTH, "rrf": RRF_K, "k": k})


def catalog(conn):
    """כל המסמכים בלי הטקסט, לשאלות על כל המאגר ("אילו הסכמים מסתיימים השנה")."""
    return _dicts(conn, "select file_id, name, url, summary, metadata from agreements order by name")


def missing_page_marks(conn):
    """מסמכים מרובי עמודים שנשמרו לפני סימוני [עמוד N]. בלעדיהם התשובות לא יכולות לציין עמוד."""
    return {r[0] for r in conn.execute(
        "select file_id from agreements where coalesce(pages, 2) > 1 and position('[עמוד 1]' in text) = 0").fetchall()}


def save_text(conn, file_id, text):
    """הטקסט בלבד. התקציר וה-embedding לא משתנים, ולכן בלי קריאה למודל. הקטעים נחתכים מחדש בסנכרון הבא."""
    conn.execute("update agreements set text = %s, chunk_version = null where file_id = %s", (text, file_id))


def get_doc(conn, file_id):
    """מסמך אחד מהמאגר (למסמך בסיס בניסוח), או None."""
    rows = _dicts(conn, "select file_id, name, url, summary, text from agreements where file_id = %s", (file_id,))
    return rows[0] if rows else None


# טבלת files: סטטוס לכל קובץ שהסנכרון ראה ב-Drive, כולל קבצים שנכשלו או שסוגם לא נתמך

def retry_ids(conn):
    return {r[0] for r in conn.execute("select file_id from files where retry_requested").fetchall()}


def save_files(conn, rows):
    """rows: dicts עם file_id, name, mime_type, size_bytes, modified_time, status, error, attempted.
    קובץ שעובד בריצה הזו מקבל last_attempt_at חדש, ובקשת ה-retry שלו מתאפסת."""
    with conn.cursor() as cur:
        cur.executemany(
            """insert into files (file_id, name, mime_type, size_bytes, modified_time, status, error,
                                  last_attempt_at, retry_requested)
               values (%(file_id)s, %(name)s, %(mime_type)s, %(size_bytes)s, %(modified_time)s, %(status)s,
                       %(error)s, case when %(attempted)s then now() end, false)
               on conflict (file_id) do update set name=excluded.name, mime_type=excluded.mime_type,
                 size_bytes=excluded.size_bytes, modified_time=excluded.modified_time, status=excluded.status,
                 error=excluded.error, last_attempt_at=coalesce(excluded.last_attempt_at, files.last_attempt_at),
                 retry_requested = files.retry_requested and not %(attempted)s""",
            rows,
        )


def prune_files(conn, seen_ids):
    """קובץ שנמחק מ-Drive נמחק גם מהטבלה."""
    conn.execute("delete from files where file_id <> all(%s)", (list(seen_ids),))


def add_queued(conn, f):
    """קובץ שהועלה מהאתר: בתור עד שהסנכרון יעבד אותו."""
    conn.execute(
        """insert into files (file_id, name, mime_type, size_bytes, modified_time, status)
           values (%s, %s, %s, %s, %s, 'queued')
           on conflict (file_id) do update set status = 'queued', error = null""",
        (f["id"], f["name"], f["mimeType"], int(f["size"]) if f.get("size") else None, f.get("modifiedTime")),
    )


def mark_retry(conn, file_id):
    """מסמן קובץ לעיבוד מחדש בסנכרון הבא. מחזיר את שמו, או None אם הוא לא בטבלה."""
    row = conn.execute("update files set retry_requested = true, status = 'queued' where file_id = %s "
                       "returning name", (file_id,)).fetchone()
    return row[0] if row else None


def files_list(conn):
    """כל הקבצים לפאנל באתר, עם הנתונים המובנים והקישור כשהקובץ במאגר."""
    return _dicts(conn, """select f.file_id, f.name, f.mime_type, f.size_bytes, f.modified_time, f.status, f.error,
                                  f.last_attempt_at, f.retry_requested,
                                  coalesce(a.url, 'https://drive.google.com/file/d/' || f.file_id || '/view') as url,
                                  a.summary, a.metadata, a.method, a.ocr_quality
                           from files f left join agreements a using (file_id) order by f.name""")


def missing_pages(conn):
    """מסמכים שאונדקסו לפני שמספר העמודים נשמר."""
    return {r[0] for r in conn.execute("select file_id from agreements where pages is null").fetchall()}


def save_pages(conn, file_id, pages):
    conn.execute("update agreements set pages = %s where file_id = %s", (pages, file_id))


def missing_quality(conn):
    """מסמכים סרוקים שעברו OCR לפני שנשמר ציון איכות."""
    return {r[0] for r in conn.execute(
        "select file_id from agreements where method = 'ocr' and ocr_quality is null").fetchall()}


def save_quality(conn, file_id, quality):
    conn.execute("update agreements set ocr_quality = %s where file_id = %s", (quality, file_id))


def status_rows(conn):
    """דוח "סטטוס מאגר ההסכמים": שורות של מדד וערך, מ-SQL בלבד. ערך שעוד לא ידוע מוצג "—" ולא 0."""
    total, failed = conn.execute("select count(*), count(*) filter (where status = 'failed') from files").fetchone()
    # OCR מוחלט לכל הקובץ, ולכן סופרים הסכמים ולא עמודים. method נשמר לכל מסמך, כך שהמספר ידוע תמיד.
    # עמודים: סכום מה שכבר נספר. PDF או תמונה שעוד לא נספרו מסומנים "(חלקי)". ל-DOCX ול-Google Docs אין עמודים
    pages, ocr_docs, uncounted = conn.execute(
        """select sum(a.pages), count(*) filter (where a.method = 'ocr'),
                  count(*) filter (where a.pages is null and (f.mime_type = 'application/pdf'
                                                               or f.mime_type like 'image/%'))
           from agreements a left join files f using (file_id)"""
    ).fetchone()
    s = status(conn)
    dash = "—"
    pages_value = None if pages is None else f"{pages:,} (חלקי)" if uncounted else pages

    def row(label, value, tone=""):
        return {"label": label, "value": dash if value is None else f"{value:,}" if isinstance(value, int) else value,
                "tone": "muted" if value is None else tone}

    return [
        row('סה"כ מסמכים בתיקייה', total if total else None),  # טבלה ריקה = הסנכרון עוד לא רץ
        row("מאונדקסים", s["documents"]),
        row("נכשלו", failed if total else None, "bad" if failed else ""),
        row("עמודים", pages_value, "muted" if uncounted else ""),
        row("הסכמים שעברו OCR", ocr_docs),
        row("אינדוקס אחרון", s["updated_at"]),
    ]


def log_run(conn, stats):
    conn.execute(
        """insert into sync_runs (started_at, finished_at, added, updated, removed, ocr, failed, errors, files)
           values (%s, now(), %s, %s, %s, %s, %s, %s, %s)""",
        (stats["started_at"], stats["added"], stats["updated"], stats["removed"], stats["ocr"],
         len(stats["errors"]), json.dumps(stats["errors"], ensure_ascii=False),
         json.dumps(stats["files"], ensure_ascii=False)),
    )


def recent_runs(conn, n=20):
    """הריצות האחרונות, עם הזמנים בשעון ישראל."""
    rows = conn.execute(
        "select started_at at time zone %s, finished_at at time zone %s, added, updated, removed, ocr, failed, "
        "errors, files from sync_runs order by id desc limit %s", (TZ, TZ, n)
    ).fetchall()
    keys = ("started_at", "finished_at", "added", "updated", "removed", "ocr", "failed", "errors", "files")
    return [dict(zip(keys, r)) for r in rows]


def status(conn):
    """לשורת הסטטוס בראש האתר: זמן הסיום של הריצה האחרונה בשעון ישראל, מספר המסמכים, והאם היו שגיאות."""
    run = conn.execute(
        "select finished_at at time zone %s, failed from sync_runs order by id desc limit 1", (TZ,)
    ).fetchone()
    documents = conn.execute("select count(*) from agreements").fetchone()[0]
    return {"updated_at": f"{run[0]:%d/%m/%Y %H:%M}" if run else None, "documents": documents,
            "failed": bool(run and run[1])}


COST_DAYS = (7, 30, 90)  # הטווחים לבחירה בעמוד "עלויות"
HISTORY_FIELDS = (f"id, title, left(text, 300) as preview, rating, "
                  f"to_char(created_at at time zone '{TZ}', 'DD/MM/YYYY HH24:MI') as created_at, "
                  f"to_char(updated_at at time zone '{TZ}', 'DD/MM/YYYY HH24:MI') as updated_at")


def save_draft(conn, draft_id, title, html, text, messages, sources, conversation=None):
    """שומר הסכם בהיסטוריה: עדכון של הקיים, או חדש (גם כשהקיים כבר נמחק). טיוטות נמחקות רק ידנית."""
    values = (title, html, text, Jsonb(messages), Jsonb(sources), conversation)
    if draft_id:
        row = conn.execute("update drafts set title = %s, html = %s, text = %s, messages = %s, sources = %s, conversation = %s, "
                           "updated_at = now() where id = %s returning id", (*values, draft_id)).fetchone()
        if row:
            return row[0]
    return conn.execute("insert into drafts (title, html, text, messages, sources, conversation) "
                        "values (%s, %s, %s, %s, %s, %s) "
                        "returning id", values).fetchone()[0]


def drafts_list(conn):
    """עמוד ההיסטוריה: מהחדש לישן, בלי ה-HTML והשיחה (נטענים רק כשפותחים הסכם)."""
    return _dicts(conn, f"select {HISTORY_FIELDS} from drafts order by drafts.updated_at desc")


def get_draft(conn, draft_id):
    rows = _dicts(conn, f"select {HISTORY_FIELDS}, html, messages, sources, conversation from drafts where id = %s", (draft_id,))
    return rows[0] if rows else None


def delete_draft(conn, draft_id):
    return conn.execute("delete from drafts where id = %s returning id", (draft_id,)).fetchone() is not None


def rate_draft(conn, draft_id, rating):
    return conn.execute("update drafts set rating = %s where id = %s returning id",
                        (rating, draft_id)).fetchone() is not None


def costs(conn, days=7):
    """עמוד "עלויות": סה"כ ב-days הימים האחרונים לפי סוג, העלות של כל טיוטה מהתקופה, ושיחות שלא נשמרו כטיוטה."""
    week = "now() - make_interval(days => %s)"
    kinds = {r["kind"]: r for r in _dicts(
        conn, f"select kind, sum(cost_usd)::float as cost, count(*) as calls from api_usage where at >= {week} "
              "group by kind", (days,))}
    drafts = _dicts(conn, f"""
        select d.id, d.title, to_char(d.created_at at time zone '{TZ}', 'DD/MM/YYYY HH24:MI') as created_at,
               sum(u.cost_usd)::float as cost, count(u.id) filter (where u.model <> %s) as runs
        from drafts d left join api_usage u on u.conversation = d.conversation
        where d.updated_at >= {week}
        group by d.id order by d.created_at desc""", (EMBED_MODEL, days))
    unsaved = conn.execute(f"""
        select coalesce(sum(cost_usd), 0)::float from api_usage u
        where kind = 'draft' and at >= {week}
          and not exists (select 1 from drafts d where d.conversation = u.conversation)""",
                           (days,)).fetchone()[0]
    since = conn.execute(f"select to_char(min(at) at time zone '{TZ}', 'DD/MM/YYYY') from api_usage").fetchone()[0]
    return {"days": days, "total": sum(k["cost"] for k in kinds.values()),
            **{k: kinds.get(k, {"cost": 0, "calls": 0}) for k in ("draft", "ask", "sync")},
            "drafts": drafts, "unsaved": unsaved, "since": since}
