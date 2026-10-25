"""הפעלת workflow הסנכרון ב-GitHub Actions (workflow_dispatch) מהאתר, לכל היותר פעם ב-5 דקות.

משתני סביבה: GH_DISPATCH_TOKEN (fine-grained token, Actions: Read and write, רק ל-repo הזה) ו-GH_REPO (owner/repo).
"""
import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger(__name__)
WORKFLOW = "sync.yml"
REF = "main"  # הענף שממנו ה-workflow רץ
MIN_INTERVAL = 5 * 60
RUNNING = {"queued", "in_progress", "waiting", "requested", "pending"}

_lock = threading.Lock()
_last = None  # זמן ההפעלה האחרונה (monotonic)
_timer = None  # הפעלה דחויה שכבר מתוזמנת

# ponytail: המגבלה בזיכרון של התהליך; הפעלה מחדש של השרת מאפסת אותה. ב-GitHub הריצות ממילא לא רצות במקביל


def configured():
    return bool(os.environ.get("GH_DISPATCH_TOKEN") and os.environ.get("GH_REPO"))


def _api(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GH_REPO']}/{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['GH_DISPATCH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read() or b"null")


def trigger(defer=False):
    """מפעיל סנכרון. אם הופעל לפני פחות מ-5 דקות: לא מפעיל, ומחזיר כמה שניות לחכות.
    defer=True (העלאה, נסה שוב): מתזמן הפעלה אחת לסוף ההמתנה, כדי שהקובץ החדש לא יחכה לסנכרון הלילי."""
    global _last, _timer
    with _lock:
        wait = 0 if _last is None else MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            if defer and _timer is None:
                _timer = threading.Timer(wait, _deferred)
                _timer.daemon = True
                _timer.start()
            return {"started": False, "retry_after": int(wait) + 1, "deferred": defer}
        _api("POST", f"actions/workflows/{WORKFLOW}/dispatches", {"ref": REF})
        _last = time.monotonic()
        return {"started": True, "retry_after": MIN_INTERVAL, "deferred": False}


def _deferred():
    global _timer
    with _lock:
        _timer = None
    try:
        trigger()
    except Exception as e:  # אין למי להחזיר את השגיאה; הסנכרון הלילי יאסוף את הקבצים
        log.warning(f"הפעלת סנכרון דחויה נכשלה: {e}")


def running():
    """האם יש ריצת סנכרון שעוד לא הסתיימה."""
    runs = _api("GET", f"actions/workflows/{WORKFLOW}/runs?per_page=1")["workflow_runs"]
    return bool(runs) and runs[0]["status"] in RUNNING
