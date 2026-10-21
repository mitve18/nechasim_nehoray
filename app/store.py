"""מאגר ההסכמים ב-Postgres (Neon) עם pgvector. embeddings דרך OpenAI, תקצירים דרך Agent של Agno."""
import json
import os

import psycopg
from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
SUMMARY_MODEL = "gpt-4o"

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
-- מיגרציה: שמות הקבצים שנוספו/עודכנו בכל ריצה. ריצות ישנות נשארות עם null
alter table sync_runs add column if not exists files jsonb;
"""
TZ = "Asia/Jerusalem"  # ההמרה נעשית ב-Postgres, שיש לו את טבלאות אזורי הזמן גם כשאין tzdata ב-Windows


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def init(conn):
    conn.execute(SCHEMA)


def embed(text):
    v = OpenAI().embeddings.create(model=EMBED_MODEL, input=text[:20000]).data[0].embedding
    return "[" + ",".join(map(str, v)) + "]"


def summarize(name, text):
    """תקציר קצר שמשמש לחיפוש ולהצגת המקורות."""
    agent = Agent(
        model=OpenAIResponses(id=SUMMARY_MODEL),
        instructions=(
            "כתוב בעברית תקציר של 3–4 משפטים למסמך המשפטי הבא: סוג המסמך (הסכם, כתב התחייבות וכו'), "
            "הצדדים, התחום המקצועי, והנושאים והסעיפים העיקריים. בלי הקדמות."
        ),
    )
    result = agent.run(f"שם הקובץ: {name}\n\n{text[:30000]}")
    if result.status != RunStatus.completed or not result.content:  # הקובץ לא נשמר, ולכן ינוסה שוב בריצה הבאה
        raise RuntimeError(f"התקציר נכשל: status={result.status}")
    return result.content.strip()


def indexed(conn):
    """{file_id: modified_time} של מה שכבר במאגר."""
    return dict(conn.execute("select file_id, modified_time from agreements").fetchall())


def upsert(conn, f, method, text):
    summary = summarize(f["name"], text)
    conn.execute(
        """insert into agreements (file_id, name, url, modified_time, method, summary, text, embedding)
           values (%s, %s, %s, %s, %s, %s, %s, %s::vector)
           on conflict (file_id) do update set name=excluded.name, url=excluded.url,
             modified_time=excluded.modified_time, method=excluded.method, summary=excluded.summary,
             text=excluded.text, embedding=excluded.embedding, indexed_at=now()""",
        (f["id"], f["name"], f.get("webViewLink"), f["modifiedTime"], method, summary, text,
         embed(f"{f['name']}\n{summary}")),
    )


def delete(conn, file_ids):
    conn.execute("delete from agreements where file_id = any(%s)", (list(file_ids),))


def search(conn, query, k=3):
    rows = conn.execute(
        "select name, url, summary, text from agreements order by embedding <=> %s::vector limit %s",
        (embed(query), k),
    ).fetchall()
    return [dict(zip(("name", "url", "summary", "text"), r)) for r in rows]


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
