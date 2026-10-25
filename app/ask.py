"""שאלות על המסמכים במאגר: Agent עם כלי חיפוש, רשימה וקריאה, שעונה רק מהמסמכים ומצטט מסמך, סעיף ועמוד.

שאלה על הרבה מסמכים (map-reduce): הכלי ask_documents מריץ במקביל קורא זול (READER_MODEL) על כל מסמך, והוא
מחזיר רק את הקטעים הרלוונטיים. ה-Agent הראשי מקבל את הקטעים ולא את המסמכים המלאים, וכך מכסה עשרות מסמכים.

search_documents מחזיר קטעים ולא מסמכים (store.search_chunks): חיפוש היברידי בסעיפים, עם הסעיף והעמוד של כל קטע.

python -m app.ask "השאלה"   בדיקה מהטרמינל
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from pydantic import BaseModel, Field

from app import store

log = logging.getLogger(__name__)
MODEL = "gpt-4o"
READER_MODEL = "gpt-4.1-mini"  # הקורא של ask_documents: חלון של מיליון טוקנים, ובערך שישית מהמחיר של MODEL
SEARCH_CHUNKS = 8  # קטעים בתוצאות החיפוש: כ-5,000 טוקנים, במקום מסמכים מלאים
READ_CHARS = 80000  # מסמך אחד שנקרא במלואו
READER_CHARS = 200000  # מסמך אחד אצל הקורא. תקרה לעלות: בערך 3 סנט למסמך ארוך במיוחד
READER_DOCS = 20  # מסמכים בקריאה אחת ל-ask_documents
READER_PARALLEL = 5

INSTRUCTIONS = [
    "ענה רק על סמך המסמכים במאגר, שמגיעים אליך דרך הכלים. אל תשתמש בידע כללי על חוזים, ואל תנחש.",
    "שאלה על נושא או על מסמך מסוים: search_documents. שאלה על כל המאגר (רשימות, השוואות, תאריכי סיום, סכומים, "
    "צדדים): קודם list_documents. אם הנתונים המובנים שם לא מספיקים, ask_documents עם כל המסמכים הרלוונטיים "
    "בקריאה אחת. read_document רק לקריאה מעמיקה של מסמך אחד או שניים.",
    "כל טענה בתשובה מסתיימת בהפניה בסוגריים מרובעים, למשל [1] או [1][2]. לכל מספר יש רשומה ב-citations "
    "עם file_id, הסעיף, העמוד וציטוט קצר ומדויק מהמסמך.",
    "העמוד הוא המספר מהסימון [עמוד N] האחרון שמופיע לפני הקטע המצוטט. בקטע מ-search_documents, העמוד שבו הוא "
    "מתחיל כתוב ב-page שלו, וסימון [עמוד N] בתוך הקטע גובר עליו. בלי סימוני עמודים, page הוא null.",
    "אם התשובה לא נמצאת במסמכים, אמור זאת במפורש, ואל תשלים מהדמיון.",
    "ענה בעברית, קצר וענייני: פסקה, או רשימה קצרה כשיש כמה פריטים (שורה לכל פריט, מתחילה ב-'- '). "
    "בלי כותרות ובלי הדגשות.",
]


READER_INSTRUCTIONS = [
    "אתה קורא מסמך אחד מהמאגר ומחלץ ממנו רק את מה שעונה על השאלה. אתה לא עונה על השאלה בעצמך.",
    "לכל קטע רלוונטי: ציטוט מדויק מהמסמך, מילה במילה, מספר הסעיף, והעמוד: המספר מהסימון [עמוד N] האחרון "
    "שלפני הקטע (אם אין סימוני עמודים, בלי עמוד).",
    "אם המסמך לא עוסק בשאלה, החזר רק: לא רלוונטי.",
    "בלי פרשנות, בלי סיכום ובלי ידע כללי.",
]


class Citation(BaseModel):
    n: int = Field(description="המספר שמופיע בתשובה, למשל 1 עבור [1]")
    file_id: str = Field(description="ה-file_id של המסמך, כפי שהגיע מהכלים")
    section: str = Field(description='מספר הסעיף או הכותרת שלו, למשל "סעיף 7.2"; מחרוזת ריקה אם אין')
    page: int | None = Field(description="המספר מהסימון [עמוד N] שלפני הקטע; null אם אין סימוני עמודים")
    quote: str = Field(description="ציטוט מדויק מהמסמך, עד 25 מילים")


class Answer(BaseModel):
    answer: str
    citations: list[Citation]


def _docs(docs, seen, chars):
    """מסמכים להקשר של המודל. seen אוסף את המסמכים שהמודל ראה, כדי לקשר ציטוטים ל-Drive ולסנן ציטוטים מומצאים."""
    for d in docs:
        seen[d["file_id"]] = {"name": d["name"], "url": d["url"]}
    return "\n\n".join(f'<מסמך file_id="{d["file_id"]}" name="{d["name"]}">\n{d["text"][:chars]}\n</מסמך>'
                       for d in docs) or "לא נמצאו מסמכים."


def build_agent(seen, usage):
    def search_documents(query: str) -> str:
        """חיפוש בסעיפים של כל המסמכים במאגר, לפי משמעות וגם לפי מילים מדויקות (מספר גוש, מספר תוכנית, שם צד).
        מחזיר את הקטעים הרלוונטיים ביותר, כל אחד עם המסמך, הסעיף והעמוד שלו. לקריאת המסמך המלא: read_document.

        Args:
            query: מה מחפשים: נושא, סעיף, צד, מספר או מונח.
        """
        with store.connect() as conn:
            found = store.search_chunks(conn, query, k=SEARCH_CHUNKS, usage=usage)
        for c in found:
            seen[c["file_id"]] = {"name": c["name"], "url": c["url"]}
        return "\n\n".join(f'<קטע file_id="{c["file_id"]}" name="{c["name"]}" section="{c["section"]}" '
                           f'page="{c["page"] or ""}">\n{c["text"]}\n</קטע>' for c in found) or "לא נמצאו קטעים."

    def list_documents() -> str:
        """כל המסמכים במאגר, בלי הטקסט: file_id, שם, תקציר ונתונים מובנים (סוג, צדדים, תאריכים, סכומים, מיקום)."""
        with store.connect() as conn:
            docs = store.catalog(conn)
        for d in docs:
            seen[d["file_id"]] = {"name": d["name"], "url": d["url"]}
        return "\n".join(json.dumps(d, ensure_ascii=False, default=str) for d in docs) or "המאגר ריק."

    def read_document(file_id: str) -> str:
        """הטקסט המלא של מסמך אחד, לפי ה-file_id שלו מ-list_documents או מ-search_documents.

        Args:
            file_id: מזהה המסמך.
        """
        with store.connect() as conn:
            doc = store.get_doc(conn, file_id)
        return _docs([doc], seen, READ_CHARS) if doc else "אין מסמך עם המזהה הזה."

    def ask_documents(file_ids: list[str], question: str) -> str:
        """שאלה אחת על כמה מסמכים (עד 20) במקביל: מכל מסמך חוזרים רק הקטעים שעונים עליה, עם ציטוט, סעיף ועמוד.
        לשאלות על הרבה מסמכים (השוואות, "באילו הסכמים יש..."), במקום read_document לכל מסמך.

        Args:
            file_ids: מזהי המסמכים מ-list_documents או מ-search_documents.
            question: השאלה, כפי שצריך לענות עליה מכל מסמך בנפרד.
        """
        with store.connect() as conn:
            docs = [d for d in (store.get_doc(conn, i) for i in list(dict.fromkeys(file_ids))[:READER_DOCS]) if d]
        for d in docs:
            seen[d["file_id"]] = {"name": d["name"], "url": d["url"]}

        def read(d):
            reader = Agent(model=OpenAIResponses(id=READER_MODEL, timeout=120, max_retries=1),
                           instructions=READER_INSTRUCTIONS)
            try:
                r = reader.run(f"<שאלה>{question}</שאלה>\n\n<מסמך name=\"{d['name']}\">\n{d['text'][:READER_CHARS]}\n</מסמך>")
                usage.append(store.agent_usage(r, READER_MODEL))  # list.append בטוח בין threads
                found = r.content if r.status == RunStatus.completed else "הקריאה נכשלה"
            except Exception as e:  # מסמך אחד שנכשל לא מפיל את התשובה; ה-Agent יודע שהוא חסר
                log.warning(f"קריאת מסמך נכשלה: {d['name']}: {e!r}")
                found = "הקריאה נכשלה"
            return f'<מסמך file_id="{d["file_id"]}" name="{d["name"]}">\n{found}\n</מסמך>'

        with ThreadPoolExecutor(READER_PARALLEL) as pool:
            return "\n\n".join(pool.map(read, docs)) or "לא נמצאו מסמכים עם המזהים האלה."

    return Agent(
        model=OpenAIResponses(id=MODEL, timeout=120, max_retries=1),
        instructions=INSTRUCTIONS,
        tools=[search_documents, list_documents, read_document, ask_documents],
        tool_call_limit=6,  # רשימה ועוד כמה קריאות מספיקים; התקרה מונעת לולאה יקרה
        output_schema=Answer,
    )


def ask(messages, conversation=None):
    """messages: [{role: user|assistant, content}]. מחזיר {answer, citations: [{n, name, url, section, page, quote}]}."""
    seen, usage = {}, []
    try:
        result = build_agent(seen, usage).run(messages)
        usage.append(store.agent_usage(result, MODEL))
    finally:
        store.record("ask", usage, conversation)
    if result.status != RunStatus.completed or not isinstance(result.content, Answer):
        raise RuntimeError(f"המודל לא החזיר תשובה תקינה: {str(result.content)[:300]}")
    citations = [c.model_dump() | seen[c.file_id] for c in result.content.citations if c.file_id in seen]
    return {"answer": result.content.answer.strip(), "citations": citations}


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(ask([{"role": "user", "content": sys.argv[1]}]), ensure_ascii=False, indent=2))
