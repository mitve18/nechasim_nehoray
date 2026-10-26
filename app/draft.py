"""מנוע הניסוח: שלושה Agents של Agno, כל אחד במודל שמתאים לתפקיד שלו.

Intake (INTAKE_MODEL, זול): מחפש במאגר ורואה רק תקצירים. מחליט אם לשאול שאלות השלמה, ובוחר מסמכים לדוגמה.
  רוב סבבי השאלות נעצרים כאן, בלי לשלם על הטקסט המלא של הדוגמאות.
Writer (MODEL): מנסח, עם הטקסט המלא רק של מסמכי הבסיס והדוגמאות שנבחרו. תיקון בצ'אט מגיע ישר אליו, בלי חיפוש.
  כותב טקסט רגיל ולא JSON, ומוזרם לממשק (stream), כך שההסכם מופיע על המסך תוך כדי כתיבה.
Reviewer (REVIEW_MODEL, רק ב"בדיקה משפטית מעמיקה"): מחזיר תיקונים נקודתיים והערות, לא את כל ההסכם מחדש.
החיבור ביניהם בקוד רגיל ולא ב-Team של Agno: הסדר קבוע, ומודל "מנהל" היה מוסיף קריאה ועלות בכל סבב.

python -m app.draft "תיאור ההסכם"   בדיקה מהטרמינל
"""
import difflib
import json
import logging
import re
from typing import Literal

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run.agent import RunContentEvent, RunOutput
from agno.run.base import RunStatus
from pydantic import BaseModel, Field

from app import store
from app.extract import strip_pages

log = logging.getLogger(__name__)
MODEL = "gpt-4o"
INTAKE_MODEL = "gpt-4.1-mini"  # שאלות השלמה ובחירת דוגמאות. המחירים של כל המודלים ב-store.PRICES
REVIEW_MODEL = "gpt-5.5"  # "בדיקה משפטית מעמיקה": מודל חזק יותר שעובר על הטיוטה
MAX_OUTPUT_TOKENS = 16384  # התקרה של gpt-4o; הסכם עברי ארוך במיוחד ייחתך ויחזיר שגיאה ברורה
EXAMPLE_CHARS = 40000  # לכל מסמך מהמאגר, כדי שהדוגמאות + השיחה ייכנסו בחלון של 128K טוקנים
BASE_CHARS = 3 * EXAMPLE_CHARS  # לכל מסמכי הבסיס יחד, כמה שלא יהיו
MAX_EXAMPLES = 2  # דוגמאות מהחיפוש שה-Writer מקבל במלואן, מעבר למסמכי הבסיס

# השדות של Agno Agent. name לא נכנס לפרומפט (add_name_to_context כבוי); description פותח את הודעת המערכת
NAME = "מנסח ההסכמים"

DESCRIPTION = """אתה עוזר משפטי שמנסח הסכמים, כתבי התחייבות ומסמכים משפטיים בעברית עבור עובדי החברה.
אתה עובד על בסיס מאגר המסמכים של החברה: אתה מאמץ את הנוסח, המבנה והמינוח שלהם,
ומתאים אותם לבקשה של העובד. התוצר שלך הוא טיוטה לבדיקה, לא ייעוץ משפטי."""

# סדר הרשימה הוא סדר ההוראות. Agno מוסיף תבליט רק לפריט בשורה אחת, ולכן פריט רב-שורות פותח ב-"- "
INTAKE_INSTRUCTIONS = [
    "אתה השלב הראשון: אתה לא מנסח. אתה מחליט אם אפשר לנסח, ובוחר מסמכים לדוגמה.",
    "קודם חפש במאגר מסמכים דומים בעזרת כלי החיפוש (search_agreements). חפש לפי סוג המסמך, הצדדים והתחום.",
    """- בדוק אם יש מספיק פרטים מהותיים: זהות הצדדים, נושא ההתקשרות או העבודה, מיקום, תקופה ומועדים,
   סכומים, ביטוח ואחריות (כשרלוונטי).
   - אם חסרים פרטים מהותיים, החזר status="questions" עם עד 6 שאלות קצרות וממוקדות, לפי סדר חשיבות.
   - אל תשאל על פרטים שוליים, ואל תשאל שוב על משהו שהעובד כבר ענה עליו.
   - אם העובד ביקש לנסח בלי שאלות השלמה, החזר status="ready".""",
    f'כשיש מספיק פרטים, החזר status="ready", וב-examples עד {MAX_EXAMPLES} file_id מתוצאות החיפוש, '
    "של המסמכים שהנוסח והמבנה שלהם הכי מתאימים לבקשה. אם אף מסמך לא מתאים, רשימה ריקה.",
    'אם הבקשה לא קשורה לניסוח מסמך משפטי, החזר status="questions" עם שאלה אחת שמבהירה איזה מסמך העובד צריך.',
]

WRITER_INSTRUCTIONS = [
    """- נסח מסמך מלא, מוכן לבדיקה ולחתימה:
   - מבנה ה-body: פרטי הצדדים, מבוא (הואיל), סעיפים ממוספרים (1., 1.1, 1.2), ובלוק חתימות לכל הצדדים.
   - אל תחזור על הכותרת בתוך ה-body.
   - פסקאות המבוא כתובות בלי מספור, ומתחילות ב'הואיל ו...' ו'והואיל ו...'.
   - הסעיפים הממוספרים מתחילים מ-1. אחרי 'לפיכך'.
   - כל סעיף מתחיל בכותרת בשורה נפרדת.
   - עברית משפטית תקנית ועקבית. כל מונח מוגדר מוגדר פעם אחת, ואחר כך משתמשים בו בדיוק באותה צורה.
   - אמץ את הנוסח, סדר הסעיפים והמינוח של המסמכים מהמאגר שבהקשר, כשהם מתאימים לבקשה.""",
    "אל תמציא עובדות. פרט שלא נמסר מסמנים כ-[___] למילוי: שמות, מספרי ח.פ., סכומים, תאריכים, כתובות.",
    "אל תעתיק פרטים מזהים מהמסמכים במאגר, כמו שמות צדדים, מספרים ותאריכים, אלא אם העובד ביקש אותם במפורש. "
    "מהמאגר לוקחים נוסח ומבנה, לא נתונים.",
    "אם אין בהקשר מסמכים מהמאגר, נסח לפי מבנה מקובל של מסמכים מאותו סוג בישראל.",
    "כשהעובד מבקש תיקון למסמך שכבר נוסח, החזר את המסמך המלא והמעודכן, ושנה רק את מה שהתבקש.",
    "השורה הראשונה היא כותרת המסמך בלבד. אחריה שורה ריקה, ואז גוף המסמך.",
    "כתוב טקסט רגיל בלי Markdown (בלי #, ** או רשימות עם תבליטים).",
]


class Intake(BaseModel):
    status: Literal["questions", "ready"]
    questions: list[str]
    examples: list[str] = Field(description="file_id של המסמכים לדוגמה, מתוצאות החיפוש")


def build_intake(focus, found, usage=None):
    """Intake חדש לכל בקשה. ה-tool ממלא את found ({file_id: מסמך}) במסמכים ששלף, כדי שהבחירה תהיה רק מהם."""
    context = None
    if focus:  # ה-Intake רק צריך לדעת שיש בסיס; הטקסט המלא נשלח ל-Writer בלבד
        context = "העובד בחר מסמכי בסיס, והם ישמשו כדוגמאות העיקריות. בחר דוגמאות נוספות רק אם הן מוסיפות:\n" + \
                  "\n".join(f"- {f['name']}: {f['summary']}" for f in focus)

    def search_agreements(query: str) -> str:
        """חיפוש סמנטי במאגר ההסכמים של החברה. מחזיר עד 3 מסמכים דומים: file_id, שם ותקציר.

        Args:
            query: תיאור המסמך המבוקש: סוג, צדדים, תחום ונושאים עיקריים.
        """
        with store.connect() as conn:
            docs = store.search(conn, query, usage=usage, learn=True)
        found.update((d["file_id"], d) for d in docs)
        return "\n".join(f'<מסמך file_id="{d["file_id"]}" name="{d["name"]}">{d["summary"]}</מסמך>'
                         for d in docs) or "לא נמצאו מסמכים דומים במאגר."

    return Agent(
        model=OpenAIResponses(id=INTAKE_MODEL, timeout=60, max_retries=1),
        description=DESCRIPTION,
        instructions=INTAKE_INSTRUCTIONS,
        additional_context=context,
        tools=[search_agreements],
        tool_call_limit=3,  # חיפוש אחד מספיק; התקרה מונעת לולאת חיפושים
        output_schema=Intake,
    )


def build_writer(focus=(), examples=(), web=False):
    """Writer חדש לכל בקשה. focus: מסמכי הבסיס שהעובד בחר ("נסח על בסיס המסמך הזה"), בלי הגבלה על הכמות.
    הם הדוגמאות העיקריות. examples: הדוגמאות שה-Intake בחר מהחיפוש, משניות בלבד."""
    parts = []
    if focus:
        # תקציב תווים משותף: עד 3 מסמכי בסיס כל אחד באורך מלא, ומעבר לזה כל מסמך מקוצר כדי להישאר בחלון ההקשר
        chars = min(EXAMPLE_CHARS, BASE_CHARS // len(focus))
        parts.append("העובד בחר מסמכי בסיס. הם הדוגמאות העיקריות: אמץ את המבנה, סדר הסעיפים והנוסח שלהם, "
                     "שלב ביניהם לפי הצורך, והתאם לבקשה. מסמכים מהמאגר שאינם בסיס הם דוגמאות משניות בלבד.\n"
                     + "\n".join(f"<מסמך בסיס name=\"{f['name']}\">\n{strip_pages(f['text'])[:chars]}\n</מסמך בסיס>"
                                 for f in focus))
    if examples:
        parts.append("\n\n".join(f"<מסמך מהמאגר name=\"{e['name']}\">\n{strip_pages(e['text'])[:EXAMPLE_CHARS]}\n"
                                 "</מסמך מהמאגר>" for e in examples))
    if web:
        parts.append("לפני הניסוח, חפש באינטרנט (web_search) דרישות חוק, תקנות ופסיקה עדכניות בישראל לסוג ההסכם הזה, "
                     "ושלב בטיוטה את מה שנדרש. העדף מקורות רשמיים (nevo, gov.il, knesset).")
    return Agent(
        # ה-SDK של OpenAI מנסה שוב פעם אחת, עם backoff, על 429, 5xx וניתוק
        model=OpenAIResponses(id=MODEL, max_output_tokens=MAX_OUTPUT_TOKENS, timeout=120, max_retries=1),
        name=NAME,
        description=DESCRIPTION,
        instructions=WRITER_INSTRUCTIONS,
        additional_context="\n\n".join(parts) or None,
        tools=[{"type": "web_search"}] if web else None,  # web_search רץ אצל OpenAI
        tool_call_limit=3 if web else None,
    )


REVIEW_INSTRUCTIONS = [
    "אתה עורך דין בכיר שבודק טיוטת הסכם בעברית לפני שהיא נשלחת לעובד.",
    "בדוק: סתירות בין סעיפים, מונחים שמוגדרים ולא בשימוש או בשימוש בלי הגדרה, הפניות שגויות למספרי סעיפים, "
    "סעיפים מקובלים שחסרים לסוג ההסכם (סיום, הפרה, שיפוי, סודיות, סמכות שיפוט, הודעות), ניסוח עמום שקשה לאכוף, "
    "והתאמה לדין הישראלי, לבקשה ולנטיית הניסוח שהעובד ביקש.",
    "תקן באמצעות edits בלבד, ואל תחזיר את המסמך המלא. find הוא קטע שמועתק מהטיוטה תו בתו, ארוך מספיק כדי להופיע "
    "בה פעם אחת בלבד. replace הוא הנוסח שבא במקומו. להוספת סעיף: find הוא סוף הסעיף שלפניו, ו-replace הוא אותו "
    "קטע ואחריו הסעיף החדש. שמור על המבנה, המספור והסגנון; שנה רק מה שצריך.",
    "אל תמציא עובדות. פרט חסר נשאר [___].",
    "notes: עד 6 הערות קצרות בעברית על מה שתיקנת או על מה שהעובד צריך לבדוק בעצמו. בלי הערות כלליות.",
    "replace הוא טקסט רגיל בלי Markdown.",
]


class Edit(BaseModel):
    find: str = Field(description="קטע מהטיוטה, מועתק תו בתו, שמופיע בה פעם אחת בלבד")
    replace: str = Field(description="הנוסח המתוקן שבא במקום הקטע")


class Review(BaseModel):
    edits: list[Edit]
    notes: list[str]


def apply_edits(body, edits):
    """מחיל את התיקונים של הבודק לפי הסדר. תיקון שהקטע שלו לא מופיע בדיוק פעם אחת מדולג, כדי לא לשנות מקום
    אחר בטעות. מחזיר (body, מספר התיקונים שדולגו)."""
    skipped = 0
    for e in edits:
        if e.find and body.count(e.find) == 1:
            body = body.replace(e.find, e.replace)
        else:
            skipped += 1
    return body, skipped


def _review(messages, out, usage, tag):
    """"בדיקה משפטית מעמיקה": REVIEW_MODEL מתקן את הטיוטה. כישלון לא מאבד את הטיוטה: היא חוזרת עם הערה."""
    request = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
    agent = Agent(model=OpenAIResponses(id=REVIEW_MODEL, timeout=300, max_retries=1),
                  instructions=REVIEW_INSTRUCTIONS, output_schema=Review)
    try:
        result = agent.run(f"<בקשת העובד>\n{request}\n</בקשת העובד>\n\n<טיוטה>\n{out['title']}\n\n{out['body']}\n</טיוטה>",
                           **tag)
        usage.append(store.agent_usage(result, REVIEW_MODEL))
        if result.status != RunStatus.completed or not isinstance(result.content, Review):
            raise RuntimeError(f"status={result.status}")
    except Exception as e:
        log.warning(f"הבדיקה המשפטית נכשלה: {e!r}")
        return {"review_notes": ["הבדיקה המשפטית המעמיקה נכשלה, וזו הטיוטה בלעדיה. אפשר לנסות שוב בהסכם חדש."]}
    body, skipped = apply_edits(out["body"], result.content.edits)
    notes = list(result.content.notes)
    if skipped:
        notes.append(f"{skipped} מהתיקונים של הבדיקה לא הוחלו, כי הקטע שלהם לא נמצא בטיוטה. כדאי לעבור על ההערות.")
    title, body = clean_output(out["title"], body)
    return {"title": title, "body": body, "review_notes": notes}


def _run(agent, model, messages, usage, schema, tag):
    """ריצה של Agent אחד: העלות נרשמת גם כשהתשובה לא תקינה (הקריאה כבר חויבה)."""
    result = agent.run(messages, **tag)
    usage.append(store.agent_usage(result, model))
    if result.status != RunStatus.completed:
        raise RuntimeError(f"הניסוח נכשל: status={result.status} {result.content}")
    if not isinstance(result.content, schema):  # סירוב או JSON לא תקין מגיעים כטקסט ולא כסכימה
        raise RuntimeError(f"המודל לא החזיר תשובה תקינה: {str(result.content)[:300]}")
    return result.content


class Truncated(RuntimeError):
    """ההסכם ארוך מ-MAX_OUTPUT_TOKENS ונחתך. ההודעה מוצגת לעובד, בניגוד לשגיאות אחרות."""


def _write(agent, messages, usage, tag):
    """ה-Writer בהזרמה: אירוע delta לכל קטע טקסט, ובסוף (return) הטקסט המלא.
    ב-Agno הפריט האחרון הוא RunOutput (yield_run_output), או RunErrorEvent כשהריצה נכשלה."""
    last = None
    for last in agent.run(messages, stream=True, yield_run_output=True, **tag):
        if isinstance(last, RunContentEvent) and isinstance(last.content, str) and last.content:
            yield {"type": "delta", "text": last.content}
    if not isinstance(last, RunOutput):
        raise RuntimeError(f"הניסוח נכשל: {getattr(last, 'content', None)}")
    usage.append(store.agent_usage(last, MODEL))  # גם כשהתשובה לא תקינה: הקריאה כבר חויבה
    if last.status != RunStatus.completed or not isinstance(last.content, str) or not last.content.strip():
        raise RuntimeError(f"הניסוח נכשל: status={last.status} {str(last.content)[:300]}")
    # בלי סכימה, הסכם שהגיע לתקרה לא נכשל אלא נקטע באמצע. בלי הבדיקה הזו העובד היה מקבל הסכם חסר
    if last.metrics and (last.metrics.output_tokens or 0) >= MAX_OUTPUT_TOKENS:
        raise Truncated("ההסכם ארוך מדי ונקטע באמצע. נסה לבקש מסמך קצר יותר, או לנסח אותו בחלקים")
    return last.content


def _split(text):
    """הפלט של ה-Writer: השורה הראשונה היא הכותרת, והשאר הוא הגוף."""
    title, _, body = text.strip().partition("\n")
    return title, body


def stream(messages, focus_file_ids=(), conversation=None, web=False, review=False, revise=False, user_id=None):
    """אירועי הניסוח לממשק, לפי הסדר: delta (עוד טקסט של ההסכם), reviewing (הטיוטה מוכנה, ו-REVIEW_MODEL בודק
    אותה), ובסוף result: dict עם status, questions, title, body, sources (ו-review_notes בבדיקה מעמיקה).
    messages: [{role: user|assistant, content}].
    focus_file_ids: מסמכי בסיס מהמאגר (אופציונלי). LookupError מיד, לפני ההזרמה, אם אחד מהם לא במאגר.
    conversation: מזהה השיחה מהדפדפן. העלות נרשמת איתו, כדי שעמוד "עלויות" יחבר אותה לטיוטה.
    web: חיפוש דרישות חוק עדכניות באינטרנט. review: בדיקה משפטית מעמיקה של הטיוטה ב-REVIEW_MODEL.
    revise: תיקון בצ'אט להסכם שכבר נוסח. ישר ל-Writer, בלי Intake, חיפוש ודוגמאות: ההסכם עצמו בשיחה.
    המקורות חוזרים ריקים, והממשק משאיר את המקורות של הניסוח.
    user_id: המשתמש המחובר. עובר ל-Agno עם conversation כ-session_id, לתיוג ומעקב בלבד: Agno לא שומר כאן
    כלום (אין db), וההיסטוריה נשמרת בטבלת drafts."""
    focus = []
    if focus_file_ids and not revise:
        with store.connect() as conn:
            for fid in dict.fromkeys(focus_file_ids):  # בלי כפילויות, בסדר שבו נבחרו
                doc = store.get_doc(conn, fid)
                if not doc:
                    raise LookupError("מסמך בסיס לא נמצא במאגר. ייתכן שעוד לא אונדקס")
                focus.append(doc)
    return _events(messages, focus, conversation, web, review, revise, user_id)


def _events(messages, focus, conversation, web, review, revise, user_id):
    tag = {"user_id": user_id and str(user_id), "session_id": conversation}
    sources = [{"name": f["name"], "url": f["url"], "summary": f["summary"], "base": True} for f in focus]
    usage, examples = [], []
    try:
        if not revise:
            found = {}
            intake = _run(build_intake(focus, found, usage), INTAKE_MODEL, messages, usage, Intake, tag)
            if intake.status == "questions":
                yield {"type": "result", "status": "questions", "questions": intake.questions, "title": "",
                       "body": "", "sources": sources}
                return
            base = {f["file_id"] for f in focus}
            # רק מסמכים שהחיפוש באמת החזיר: file_id מומצא או מסמך בסיס כפול לא נכנסים
            examples = [found[i] for i in dict.fromkeys(intake.examples) if i in found and i not in base][:MAX_EXAMPLES]
            sources += [{"name": e["name"], "url": e["url"], "summary": e["summary"]} for e in examples]
        if web:  # ponytail: נספר חיפוש אחד לניסוח; Agno לא מחזיר כמה חיפושים OpenAI הריץ בפועל
            usage.append(("web_search", 1, 0, 0))
        text = yield from _write(build_writer(focus, examples, web), messages, usage, tag)
        title, body = clean_output(*_split(text))
        out = {"status": "agreement", "questions": [], "title": title, "body": body}
        if review:
            yield {"type": "reviewing", "title": title, "body": body}
            out |= _review(messages, out, usage, tag)
        yield {"type": "result", **out, "sources": sources}
    finally:  # גם ניסוח שנכשל, או שהעובד סגר את החלון באמצעו (GeneratorExit), עלה כסף
        store.record("draft", usage, conversation)


def draft(*args, **kwargs):
    """אותם פרמטרים כמו stream, בלי הזרמה: רק ה-result (לטרמינל ולבדיקות)."""
    for ev in stream(*args, **kwargs):
        pass
    return {k: v for k, v in ev.items() if k != "type"}


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
