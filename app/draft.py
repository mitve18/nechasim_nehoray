"""מנוע הניסוח: שולף הסכמים דומים מהמאגר, ומנסח הסכם או שואל שאלות השלמה.

python -m app.draft "תיאור ההסכם"   בדיקה מהטרמינל
"""
import json

from openai import OpenAI

from app import store

MODEL = "gpt-5.5"

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
4. השדות שלא בשימוש: מחרוזת ריקה או רשימה ריקה."""

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["questions", "agreement"]},
        "questions": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["status", "questions", "title", "body"],
    "additionalProperties": False,
}


def draft(messages):
    """messages: [{role: user|assistant, content}]. מחזיר dict עם status, questions, title, body, sources."""
    query = "\n".join(m["content"] for m in messages if m["role"] == "user")
    with store.connect() as conn:
        examples = store.search(conn, query)
    return respond(messages, examples)


def respond(messages, examples):
    context = "\n\n".join(
        f"<מסמך מהמאגר name=\"{e['name']}\">\n{e['text']}\n</מסמך מהמאגר>" for e in examples
    ) or "לא נמצאו מסמכים דומים במאגר."
    resp = OpenAI().responses.create(
        model=MODEL,
        reasoning={"effort": "medium"},
        instructions=INSTRUCTIONS + "\n\nמסמכים מהמאגר (לשימוש כדוגמאות לנוסח בלבד):\n\n" + context,
        input=messages,
        text={"format": {"type": "json_schema", "name": "draft", "schema": SCHEMA, "strict": True}},
    )
    if resp.status != "completed":
        raise RuntimeError(f"הניסוח נכשל: status={resp.status} {resp.incomplete_details}")
    out = json.loads(resp.output_text)
    out["sources"] = [{"name": e["name"], "url": e["url"], "summary": e["summary"]} for e in examples]
    return out


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    r = draft([{"role": "user", "content": sys.argv[1]}])
    print(json.dumps(r, ensure_ascii=False, indent=2))
