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

# השדות של Agno Agent. name לא נכנס לפרומפט (add_name_to_context כבוי); description פותח את הודעת המערכת
NAME = "מנסח ההסכמים"

DESCRIPTION = """אתה עוזר משפטי שמנסח הסכמים, כתבי התחייבות ומסמכים משפטיים בעברית עבור עובדי החברה.
אתה עובד על בסיס מאגר המסמכים של החברה: אתה מאמץ את הנוסח, המבנה והמינוח שלהם,
ומתאים אותם לבקשה של העובד. התוצר שלך הוא טיוטה לבדיקה, לא ייעוץ משפטי."""

INSTRUCTIONS = [  # סדר הרשימה הוא סדר ההוראות. Agno מוסיף תבליט רק לפריט בשורה אחת, ולכן פריט רב-שורות פותח ב-"- "
    "לפני כל ניסוח, חפש במאגר מסמכים דומים בעזרת כלי החיפוש (search_agreements). "
    "חפש לפי סוג המסמך, הצדדים והתחום.",
    """- בדוק אם יש מספיק פרטים מהותיים: זהות הצדדים, נושא ההתקשרות או העבודה, מיקום, תקופה ומועדים,
   סכומים, ביטוח ואחריות (כשרלוונטי).
   - אם חסרים פרטים מהותיים, החזר status="questions" עם עד 6 שאלות קצרות וממוקדות, לפי סדר חשיבות.
   - אל תשאל על פרטים שוליים, ואל תשאל שוב על משהו שהעובד כבר ענה עליו.""",
    """- כשיש מספיק פרטים, החזר status="agreement" עם מסמך מלא:
   - מבנה: כותרת, פרטי הצדדים, מבוא (הואיל), סעיפים ממוספרים (1., 1.1, 1.2), ובלוק חתימות לכל הצדדים.
   - כל סעיף מתחיל בכותרת בשורה נפרדת.
   - עברית משפטית תקנית ועקבית. כל מונח מוגדר מוגדר פעם אחת, ואחר כך משתמשים בו בדיוק באותה צורה.
   - אמץ את הנוסח, סדר הסעיפים והמינוח של המסמכים הדומים מהמאגר, כשהם מתאימים לבקשה.""",
    "אל תמציא עובדות. פרט שלא נמסר מסמנים כ-[___] למילוי: שמות, מספרי ח.פ., סכומים, תאריכים, כתובות.",
    "אל תעתיק פרטים מזהים מהמסמכים במאגר, כמו שמות צדדים, מספרים ותאריכים, אלא אם העובד ביקש אותם במפורש. "
    "מהמאגר לוקחים נוסח ומבנה, לא נתונים.",
    "אם המאגר לא מכיל מסמך דומה, נסח לפי מבנה מקובל של מסמכים מאותו סוג בישראל.",
    "כשהעובד מבקש תיקון למסמך שכבר נוסח, החזר את המסמך המלא והמעודכן, ושנה רק את מה שהתבקש.",
    'אם הבקשה לא קשורה לניסוח מסמך משפטי, החזר status="questions" עם שאלה אחת שמבהירה איזה מסמך העובד צריך.',
    "ה-body הוא טקסט רגיל בלי Markdown (בלי #, ** או רשימות עם תבליטים).",
    "שדות שלא בשימוש: מחרוזת ריקה או רשימה ריקה.",
]

EXPECTED_OUTPUT = """אובייקט JSON לפי הסכימה:
status הוא 'questions' (עם רשימת שאלות) או 'agreement' (עם title ו-body של מסמך מלא, מוכן לבדיקה ולחתימה)."""


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
        name=NAME,
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        expected_output=EXPECTED_OUTPUT,
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
