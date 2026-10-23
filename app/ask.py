"""שאלות על המסמכים במאגר: Agent עם כלי חיפוש, רשימה וקריאה, שעונה רק מהמסמכים ומצטט מסמך, סעיף ועמוד.

python -m app.ask "השאלה"   בדיקה מהטרמינל
"""
import json

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from pydantic import BaseModel, Field

from app import store

MODEL = "gpt-4o"
SEARCH_CHARS = 30000  # לכל מסמך בתוצאות החיפוש: 5 מסמכים + השיחה נכנסים בחלון של 128K טוקנים
READ_CHARS = 80000  # מסמך אחד שנקרא במלואו

INSTRUCTIONS = [
    "ענה רק על סמך המסמכים במאגר, שמגיעים אליך דרך הכלים. אל תשתמש בידע כללי על חוזים, ואל תנחש.",
    "שאלה על נושא או על מסמך מסוים: search_documents. שאלה על כל המאגר (רשימות, השוואות, תאריכי סיום, סכומים, "
    "צדדים): קודם list_documents, ואחר כך read_document למסמכים הרלוונטיים כדי לצטט מהם.",
    "כל טענה בתשובה מסתיימת בהפניה בסוגריים מרובעים, למשל [1] או [1][2]. לכל מספר יש רשומה ב-citations "
    "עם file_id, הסעיף, העמוד וציטוט קצר ומדויק מהמסמך.",
    "העמוד הוא המספר מהסימון [עמוד N] האחרון שמופיע לפני הקטע המצוטט. במסמך בלי סימוני עמודים, page הוא null.",
    "אם התשובה לא נמצאת במסמכים, אמור זאת במפורש, ואל תשלים מהדמיון.",
    "ענה בעברית, קצר וענייני: פסקה, או רשימה קצרה כשיש כמה פריטים (שורה לכל פריט, מתחילה ב-'- '). "
    "בלי כותרות ובלי הדגשות.",
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
        """חיפוש סמנטי במאגר. מחזיר עד 5 מסמכים רלוונטיים עם הטקסט שלהם.

        Args:
            query: מה מחפשים: נושא, סוג מסמך, צד או סעיף.
        """
        with store.connect() as conn:
            return _docs(store.search(conn, query, k=5, usage=usage), seen, SEARCH_CHARS)

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

    return Agent(
        model=OpenAIResponses(id=MODEL, timeout=120, max_retries=1),
        instructions=INSTRUCTIONS,
        tools=[search_documents, list_documents, read_document],
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
