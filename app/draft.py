"""מנוע הניסוח: Agent של Agno שמחפש הסכמים דומים במאגר (tool), ומנסח הסכם או שואל שאלות השלמה.

python -m app.draft "תיאור ההסכם"   בדיקה מהטרמינל
"""
import json
from typing import Literal

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from pydantic import BaseModel

from app import store

MODEL = "gpt-4o"
MAX_OUTPUT_TOKENS = 16384  # התקרה של gpt-4o; הסכם עברי ארוך במיוחד ייחתך ויחזיר שגיאה ברורה
EXAMPLE_CHARS = 40000  # לכל מסמך מהמאגר, כדי ששלוש דוגמאות + השיחה ייכנסו בחלון של 128K טוקנים

INSTRUCTIONS = """אתה עורך דין ישראלי מנוסה שמנסח הסכמים ומסמכים משפטיים בעברית עבור עובדי החברה.
העובד מתאר את המסמך שהוא צריך. לרשותך מסמכים קיימים מהמאגר של החברה, ככל שנמצאו דומים.

כללים:
1. אם חסרים פרטים מהותיים שבלעדיהם אי אפשר לנסח מסמך שמיש (למשל: זהות הצדדים, נושא ההתקשרות,
   סכומים, מועדים, תקופה), החזר status="questions" ועד 6 שאלות ממוקדות וקצרות. אל תשאל על פרטים שוליים.
2. אחרת, החזר status="agreement" עם title ו-body: מסמך מלא, מוכן לחתימה, בעברית משפטית תקנית.
   - אמץ את הנוסח, המבנה, סדר הסעיפים והמינוח של המסמכים מהמאגר כשהם רלוונטיים, והתאם אותם לתחום המקצועי.
   - מספור סעיפים: 1., 1.1, 1.2 וכו'. כותרות סעיפים בשורה נפרדת.
   - פרט שחסר אבל אינו מהותי: השאר מקום למילוי בפורמט [___] ואל תמציא נתונים.
   - בסוף המסמך: בלוק חתימות לכל הצדדים.
   - body הוא טקסט רגיל בלי Markdown (בלי #, ** וכו').
3. אם העובד מבקש תיקון למסמך שכבר נוסח בשיחה, החזר את המסמך המלא המעודכן.
4. השדות שלא בשימוש: מחרוזת ריקה או רשימה ריקה.
5. לפני התשובה, קרא תמיד ל-search_agreements פעם אחת עם תיאור תמציתי של המסמך המבוקש (סוג, צדדים, תחום).
   המסמכים שחוזרים משמשים כדוגמאות לנוסח בלבד."""


class Draft(BaseModel):
    status: Literal["questions", "agreement"]
    questions: list[str]
    title: str
    body: str


def build_agent(sources):
    """Agent חדש לכל בקשה. ה-tool ממלא את sources במסמכים ששלף, כדי להחזיר אותם לממשק."""

    def search_agreements(query: str) -> str:
        """חיפוש סמנטי במאגר ההסכמים של החברה. מחזיר עד 3 מסמכים דומים, לשימוש כדוגמאות לנוסח.

        Args:
            query: תיאור המסמך המבוקש: סוג, צדדים, תחום ונושאים עיקריים.
        """
        with store.connect() as conn:
            examples = store.search(conn, query)
        known = {s["name"] for s in sources}
        sources.extend({"name": e["name"], "url": e["url"], "summary": e["summary"]}
                       for e in examples if e["name"] not in known)
        return "\n\n".join(
            f"<מסמך מהמאגר name=\"{e['name']}\">\n{e['text'][:EXAMPLE_CHARS]}\n</מסמך מהמאגר>" for e in examples
        ) or "לא נמצאו מסמכים דומים במאגר."

    return Agent(
        model=OpenAIResponses(id=MODEL, max_output_tokens=MAX_OUTPUT_TOKENS),
        instructions=INSTRUCTIONS,
        tools=[search_agreements],
        tool_call_limit=3,  # חיפוש אחד מספיק; התקרה מונעת לולאת חיפושים יקרה
        output_schema=Draft,
    )


def draft(messages):
    """messages: [{role: user|assistant, content}]. מחזיר dict עם status, questions, title, body, sources."""
    sources = []
    result = build_agent(sources).run(messages)
    if result.status != RunStatus.completed:
        raise RuntimeError(f"הניסוח נכשל: status={result.status} {result.content}")
    if not isinstance(result.content, Draft):  # סירוב או JSON לא תקין מגיעים כטקסט ולא כ-Draft
        raise RuntimeError(f"המודל לא החזיר מסמך תקין: {str(result.content)[:300]}")
    return result.content.model_dump() | {"sources": sources}


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    r = draft([{"role": "user", "content": sys.argv[1]}])
    print(json.dumps(r, ensure_ascii=False, indent=2))
