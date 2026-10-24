"""מנוע הניסוח: Agent של Agno שמחפש הסכמים דומים במאגר (tool), ומנסח הסכם או שואל שאלות השלמה.

python -m app.draft "תיאור ההסכם"   בדיקה מהטרמינל
"""
import difflib
import json
import re
from typing import Literal

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.base import RunStatus
from pydantic import BaseModel

from app import store
from app.extract import strip_pages

MODEL = "gpt-4o"
REVIEW_MODEL = "gpt-5.5"  # "בדיקה משפטית מעמיקה": מודל חזק יותר שעובר על הטיוטה. המחיר ב-store.PRICES
MAX_OUTPUT_TOKENS = 16384  # התקרה של gpt-4o; הסכם עברי ארוך במיוחד ייחתך ויחזיר שגיאה ברורה
EXAMPLE_CHARS = 40000  # לכל מסמך מהמאגר, כדי ששלוש דוגמאות + השיחה ייכנסו בחלון של 128K טוקנים
BASE_CHARS = 3 * EXAMPLE_CHARS  # לכל מסמכי הבסיס יחד, כמה שלא יהיו

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
   - מבנה ה-body: פרטי הצדדים, מבוא (הואיל), סעיפים ממוספרים (1., 1.1, 1.2), ובלוק חתימות לכל הצדדים.
   - אל תחזור על הכותרת בתוך ה-body.
   - פסקאות המבוא כתובות בלי מספור, ומתחילות ב'הואיל ו...' ו'והואיל ו...'.
   - הסעיפים הממוספרים מתחילים מ-1. אחרי 'לפיכך'.
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


def build_agent(sources, focus=(), usage=None, web=False):
    """Agent חדש לכל בקשה. ה-tool ממלא את sources במסמכים ששלף, כדי להחזיר אותם לממשק.

    focus: המסמכים שהעובד בחר ("נסח על בסיס המסמך הזה"), בלי הגבלה על הכמות. הם נכנסים להקשר כדוגמאות
    העיקריות ומופיעים ראשונים במקורות, ותוצאות החיפוש משמשות רק כדוגמאות משניות.
    """
    context = None
    if focus:
        # תקציב תווים משותף: עד 3 מסמכי בסיס כל אחד באורך מלא, ומעבר לזה כל מסמך מקוצר כדי להישאר בחלון ההקשר
        chars = min(EXAMPLE_CHARS, BASE_CHARS // len(focus))
        sources.extend({"name": f["name"], "url": f["url"], "summary": f["summary"], "base": True} for f in focus)
        context = ("העובד בחר מסמכי בסיס. הם הדוגמאות העיקריות: אמץ את המבנה, סדר הסעיפים והנוסח שלהם, "
                   "שלב ביניהם לפי הצורך, והתאם לבקשה. מסמכים שתמצא בחיפוש הם דוגמאות משניות בלבד.\n"
                   + "\n".join(f"<מסמך בסיס name=\"{f['name']}\">\n{strip_pages(f['text'])[:chars]}\n</מסמך בסיס>"
                               for f in focus))
    if web:
        context = (context + "\n\n" if context else "") + (
            "לפני הניסוח, חפש באינטרנט (web_search) דרישות חוק, תקנות ופסיקה עדכניות בישראל לסוג ההסכם הזה, "
            "ושלב בטיוטה את מה שנדרש. העדף מקורות רשמיים (nevo, gov.il, knesset).")

    def search_agreements(query: str) -> str:
        """חיפוש סמנטי במאגר ההסכמים של החברה. מחזיר עד 3 מסמכים דומים, לשימוש כדוגמאות לנוסח.

        Args:
            query: תיאור המסמך המבוקש: סוג, צדדים, תחום ונושאים עיקריים.
        """
        with store.connect() as conn:
            examples = store.search(conn, query, usage=usage, learn=True)
        known = {s["name"] for s in sources}
        sources.extend({"name": e["name"], "url": e["url"], "summary": e["summary"]}
                       for e in examples if e["name"] not in known)
        return "\n\n".join(
            f"<מסמך מהמאגר name=\"{e['name']}\">\n{strip_pages(e['text'])[:EXAMPLE_CHARS]}\n</מסמך מהמאגר>"
            for e in examples
        ) or "לא נמצאו מסמכים דומים במאגר."

    return Agent(
        # ה-SDK של OpenAI מנסה שוב פעם אחת, עם backoff, על 429, 5xx וניתוק
        model=OpenAIResponses(id=MODEL, max_output_tokens=MAX_OUTPUT_TOKENS, timeout=120, max_retries=1),
        name=NAME,
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        expected_output=EXPECTED_OUTPUT,
        additional_context=context,
        tools=[search_agreements] + ([{"type": "web_search"}] if web else []),  # web_search רץ אצל OpenAI
        tool_call_limit=4 if web else 3,  # חיפוש אחד מספיק; התקרה מונעת לולאת חיפושים יקרה
        output_schema=Draft,
    )


REVIEW_INSTRUCTIONS = [
    "אתה עורך דין בכיר שבודק טיוטת הסכם בעברית לפני שהיא נשלחת לעובד.",
    "בדוק: סתירות בין סעיפים, מונחים שמוגדרים ולא בשימוש או בשימוש בלי הגדרה, הפניות שגויות למספרי סעיפים, "
    "סעיפים מקובלים שחסרים לסוג ההסכם (סיום, הפרה, שיפוי, סודיות, סמכות שיפוט, הודעות), ניסוח עמום שקשה לאכוף, "
    "והתאמה לדין הישראלי, לבקשה ולנטיית הניסוח שהעובד ביקש.",
    "תקן את הטיוטה עצמה והחזר את המסמך המלא והמתוקן. שמור על המבנה, המספור והסגנון; שנה רק מה שצריך.",
    "אל תמציא עובדות. פרט חסר נשאר [___].",
    "notes: עד 6 הערות קצרות בעברית על מה שתיקנת או על מה שהעובד צריך לבדוק בעצמו. בלי הערות כלליות.",
    "ה-body הוא טקסט רגיל בלי Markdown.",
]


class Review(BaseModel):
    title: str
    body: str
    notes: list[str]


def _review(messages, out, usage):
    """"בדיקה משפטית מעמיקה": REVIEW_MODEL מתקן את הטיוטה. כישלון לא מאבד את הטיוטה: היא חוזרת עם הערה."""
    request = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
    agent = Agent(model=OpenAIResponses(id=REVIEW_MODEL, timeout=300, max_retries=1),
                  instructions=REVIEW_INSTRUCTIONS, output_schema=Review)
    try:
        result = agent.run(f"<בקשת העובד>\n{request}\n</בקשת העובד>\n\n<טיוטה>\n{out['title']}\n\n{out['body']}\n</טיוטה>")
        usage.append(store.agent_usage(result, REVIEW_MODEL))
        if result.status != RunStatus.completed or not isinstance(result.content, Review):
            raise RuntimeError(f"status={result.status}")
    except Exception as e:
        print(f"הבדיקה המשפטית נכשלה: {e!r}")
        return {"review_notes": ["הבדיקה המשפטית המעמיקה נכשלה, וזו הטיוטה בלעדיה. אפשר לנסות שוב בהסכם חדש."]}
    title, body = clean_output(result.content.title, result.content.body)
    return {"title": title, "body": body, "review_notes": result.content.notes}


def draft(messages, focus_file_ids=(), conversation=None, web=False, review=False):
    """messages: [{role: user|assistant, content}]. מחזיר dict עם status, questions, title, body, sources.
    focus_file_ids: מסמכי בסיס מהמאגר (אופציונלי). LookupError אם אחד מהם לא במאגר.
    conversation: מזהה השיחה מהדפדפן. העלות נרשמת איתו, כדי שעמוד "עלויות" יחבר אותה לטיוטה.
    web: חיפוש דרישות חוק עדכניות באינטרנט. review: בדיקה משפטית מעמיקה של הטיוטה ב-REVIEW_MODEL."""
    focus = []
    if focus_file_ids:
        with store.connect() as conn:
            for fid in dict.fromkeys(focus_file_ids):  # בלי כפילויות, בסדר שבו נבחרו
                doc = store.get_doc(conn, fid)
                if not doc:
                    raise LookupError("מסמך בסיס לא נמצא במאגר. ייתכן שעוד לא אונדקס")
                focus.append(doc)
    sources, usage = [], []
    try:
        result = build_agent(sources, focus, usage, web).run(messages)
        usage.append(store.agent_usage(result, MODEL))
        if web:  # ponytail: נספר חיפוש אחד לניסוח; Agno לא מחזיר כמה חיפושים OpenAI הריץ בפועל
            usage.append(("web_search", 1, 0, 0))
        if result.status != RunStatus.completed:
            raise RuntimeError(f"הניסוח נכשל: status={result.status} {result.content}")
        if not isinstance(result.content, Draft):  # סירוב או JSON לא תקין מגיעים כטקסט ולא כ-Draft
            raise RuntimeError(f"המודל לא החזיר מסמך תקין: {str(result.content)[:300]}")
        out = result.content.model_dump()
        out["title"], out["body"] = clean_output(out["title"], out["body"])
        if review and out["status"] == "agreement":
            out |= _review(messages, out, usage)
    finally:  # גם ניסוח שנכשל אחרי הקריאה למודל עלה כסף
        store.record("draft", usage, conversation)
    return out | {"sources": sources}


def _plain(line):
    """שורה בלי Markdown: בלי * בכלל, בלי __הדגשה__, בלי # או תבליט בתחילת השורה."""
    line = line.replace("*", "")
    line = re.sub(r"(?<![_\[])__([^_\[\]\s](?:[^_\[\]]*?[^_\[\]\s])?)__(?![_\]])", r"\1", line)  # לא נוגע ב-[___]
    return re.sub(r"^\s*(#+|[-•])\s+", "", line).strip()


def _norm(s):
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def clean_output(title, body):
    """מנקה את פלט המודל לפני שהוא מגיע לממשק ול-Word, גם כשהמודל לא ציית להוראות."""
    title = _plain(title)
    lines = [_plain(l) for l in body.splitlines()]
    first = next((i for i, l in enumerate(lines) if l), None)
    if first is not None:  # כותרת שחוזרת בראש ה-body, גם כגרסה מקוצרת שלה ("כתב התחייבות")
        a, b = _norm(lines[first]), _norm(title)
        if a and (a == b or b.startswith(a) or difflib.SequenceMatcher(None, a, b).ratio() > 0.85):
            del lines[first]
    # מבוא ממוספר ("1. המשרד...") בין "הואיל" ל"לפיכך": מסירים את המספור, כדי שהסעיפים יתחילו מ-1 אחרי "לפיכך"
    start = next((i for i, l in enumerate(lines) if re.match(r"ו?הואיל", l)), None)
    end = next((i for i, l in enumerate(lines) if l.startswith("לפיכך")), None)
    if start is not None and end is not None and start < end:
        lines[start:end] = [re.sub(r"^\d+\.\s+", "", l) for l in lines[start:end]]
    return title, re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")
    r = draft([{"role": "user", "content": sys.argv[1]}])
    print(json.dumps(r, ensure_ascii=False, indent=2))
