"""מאגר ההסכמים ב-Postgres (Neon) עם pgvector. embeddings דרך OpenAI, תקציר ונתונים מובנים דרך Agent של Agno."""
import json
import os
from datetime import date
from typing import Literal

import psycopg
from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from openai import OpenAI
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

EMBED_MODEL = "text-embedding-3-small"
SUMMARY_MODEL = "gpt-4o"
METADATA_VERSION = 1  # מעלים כשמוסיפים שדות ל-DocInfo; הסנכרון הבא משלים אותם לכל המסמכים הקיימים

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
"""
TZ = "Asia/Jerusalem"  # ההמרה נעשית ב-Postgres, שיש לו את טבלאות אזורי הזמן גם כשאין tzdata ב-Windows
PENDING = "(metadata is null or coalesce(metadata_version, 0) < %s)"  # מסמך שעוד צריך השלמת נתונים מובנים


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def init(conn):
    conn.execute(SCHEMA)


def _dicts(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def embed(text):
    v = OpenAI().embeddings.create(model=EMBED_MODEL, input=text[:20000]).data[0].embedding
    return "[" + ",".join(map(str, v)) + "]"


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


def analyze(name, text):
    """קריאה אחת למודל: התקציר (לחיפוש ולמקורות) והנתונים המובנים. מחזיר (summary, metadata)."""
    agent = Agent(model=OpenAIResponses(id=SUMMARY_MODEL), instructions=ANALYZE_INSTRUCTIONS,
                  output_schema=DocInfo)
    result = agent.run(f"שם הקובץ: {name}\n\n{text[:30000]}")
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


def upsert(conn, f, method, text, pages=None):
    summary, meta = analyze(f["name"], text)
    if pages:  # מספר העמודים האמיתי מהקובץ עדיף על מה שכתוב בטקסט
        meta["pages"] = pages
    conn.execute(
        """insert into agreements (file_id, name, url, modified_time, method, summary, text, embedding,
                                   metadata, end_date, metadata_version)
           values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
           on conflict (file_id) do update set name=excluded.name, url=excluded.url,
             modified_time=excluded.modified_time, method=excluded.method, summary=excluded.summary,
             text=excluded.text, embedding=excluded.embedding, metadata=excluded.metadata,
             end_date=excluded.end_date, metadata_version=excluded.metadata_version, indexed_at=now()""",
        (f["id"], f["name"], f.get("webViewLink"), f["modifiedTime"], method, summary, text,
         embed(f"{f['name']}\n{summary}"), Jsonb(meta), _date(meta.get("end_date")), METADATA_VERSION),
    )


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


def search(conn, query, k=3):
    rows = conn.execute(
        "select name, url, summary, text from agreements order by embedding <=> %s::vector limit %s",
        (embed(query), k),
    ).fetchall()
    return [dict(zip(("name", "url", "summary", "text"), r)) for r in rows]


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
                                  a.summary, a.metadata
                           from files f left join agreements a using (file_id) order by f.name""")


# דוחות: שאילתות SQL בלבד, בלי קריאה למודל

def pending_count(conn):
    return conn.execute(f"select count(*) from agreements where {PENDING}", (METADATA_VERSION,)).fetchone()[0]


def report_expiring(conn, days=90):
    """מסמכים שכבר פגו, ומסמכים שמסתיימים ב-days הימים הקרובים (לפי שעון ישראל)."""
    today = "(now() at time zone %s)::date"
    return _dicts(conn, f"""select file_id, name, url, end_date, metadata->>'doc_type' as doc_type,
                                   end_date - {today} as days_left
                            from agreements where end_date <= {today} + %s order by end_date""",
                  (TZ, TZ, days))


def report_index(conn):
    counts = dict(conn.execute("select status, count(*) from files group by status").fetchall())
    failed = _dicts(conn, "select file_id, name, error from files where status = 'failed' order by name")
    return {"counts": counts, "failed": failed, "last_sync": status(conn)["updated_at"]}


def report_types(conn):
    """ספירה לפי סוג מסמך ולפי מספר תוכנית, עם קישורים. רק מסמכים שכבר יש להם נתונים מובנים."""
    def group(key):
        return _dicts(conn, f"""select metadata->>'{key}' as value, count(*) as count,
                                       json_agg(json_build_object('file_id', file_id, 'name', name, 'url', url)
                                                order by name) as docs
                                from agreements where metadata->>'{key}' is not null
                                group by 1 order by 2 desc, 1""")
    return {"doc_type": group("doc_type"), "plan_number": group("plan_number")}


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
