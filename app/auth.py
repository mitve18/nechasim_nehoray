"""משתמשים: סיסמאות (bcrypt) ועוגיית כניסה חתומה.

העוגייה: "user_id.session_version.זמן הכניסה.חתימה", חתומה ב-SECRET_KEY. השרת בודק בכל בקשה במסד שהמשתמש
פעיל ושה-session_version שלו לא השתנה, כך שהשבתה או איפוס סיסמה מנתקים מיד גם עוגיות שכבר בדפדפן.
"""
import hashlib
import hmac
import os
import re
import time

import bcrypt

SECRET = os.environ.get("SECRET_KEY", "")
SESSION_DAYS = 400  # התקרה של הדפדפנים לעוגייה. העוגייה מתחדשת בכל כניסה לדף הראשי, כך שבפועל אין תפוגה
MIN_PASSWORD = 10
MAX_PASSWORD_BYTES = 72  # התקרה של bcrypt; bcrypt 5 זורק שגיאה על סיסמה ארוכה יותר
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DUMMY = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt()).decode()  # לבדיקה כשאין משתמש כזה


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password, hashed):
    """hashed=None (אין משתמש כזה) עדיין מריץ bcrypt, כדי שזמן התגובה לא יגלה אילו כתובות רשומות."""
    pw = password.encode()
    if len(pw) > MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(pw, (hashed or _DUMMY).encode()) and hashed is not None


def password_problem(password):
    """הודעת שגיאה לסיסמה חדשה, או None אם היא תקינה."""
    if len(password) < MIN_PASSWORD:
        return f"הסיסמה צריכה להיות באורך {MIN_PASSWORD} תווים לפחות"
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return "הסיסמה ארוכה מדי"
    return None


def clean_email(email):
    """כתובת באותיות קטנות, או None אם היא לא נראית כמו אימייל."""
    email = email.strip().lower()
    return email if len(email) <= 254 and EMAIL.fullmatch(email) else None


def _sign(payload):
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def cookie(user_id, version, now=None):
    payload = f"{user_id}.{version}.{int(now or time.time())}"
    return f"{payload}.{_sign(payload)}"


def parse(value):
    """(user_id, session_version) מעוגייה חתומה, אחרת None. אין תפוגה: מתנתקים רק ביציאה, השבתה או איפוס סיסמה."""
    parts = (value or "").split(".")
    if not SECRET or len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
        return None
    uid, ver, ts, sig = parts
    if not hmac.compare_digest(sig, _sign(f"{uid}.{ver}.{ts}")):
        return None
    return int(uid), int(ver)
