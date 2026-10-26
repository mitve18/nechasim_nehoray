"""משתמשים: הרשמה, כניסה, עוגייה, CSRF, בידוד טיוטות ושיחות בין משתמשים (IDOR) וניהול משתמשים.

רץ מול Postgres אמיתי (PGlite, ראה tests/pg.py), עם הכניסה האמיתית: bcrypt, עוגייה חתומה ובדיקה במסד בכל בקשה.

python tests/test_users.py   (או pytest)
"""
import contextlib
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402
from pg import postgres  # noqa: E402

from app import ask, auth, main, store  # noqa: E402

PASSWORD = "סיסמה-טובה-1"
DRAFT = {"title": "הסכם", "html": "<p>גוף</p>", "text": "גוף", "messages": [], "sources": []}


def skip(reason):
    try:
        import pytest
        pytest.skip(reason)
    except ImportError:
        print("SKIP:", reason)


@contextlib.contextmanager
def site(conn):
    """האתר מול מסד הבדיקה, עם מפתח חתימה, דומיין להרשמה מיידית ומוני ניסיונות נקיים."""
    with mock.patch.object(store, "connect", lambda: contextlib.nullcontext(conn)), \
            mock.patch.object(auth, "SECRET", "test-secret"), \
            mock.patch.multiple(main, SIGNUP_DOMAINS={"corp.co"}, _login_fails=main.defaultdict(main.deque),
                                _drafts=main.defaultdict(main.deque), _daily={"date": None, "count": 0}), \
            mock.patch.object(main.asyncio, "sleep", mock.AsyncMock()):
        yield


def client():
    return TestClient(main.app, follow_redirects=False)


def register(email, name="עובד", password=PASSWORD):
    c = client()
    r = c.post("/register", data={"email": email, "name": name, "password": password})
    return c, r


def user(conn, email, role="user", active=True):
    """משתמש ישירות במסד, ולקוח שמחובר אליו דרך /login."""
    store.create_user(conn, email, email.split("@")[0], auth.hash_password(PASSWORD), role, active)
    c = client()
    assert c.post("/login", data={"email": email, "password": PASSWORD}).headers["location"] == "/"
    return c


def test_register_login_cookie_and_csrf():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        with site(conn):
            c = client()
            r = c.get("/")
            assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
            assert c.get("/api/files").status_code == 401 and c.get("/admin/sync").status_code == 303
            assert c.get("/healthz").status_code == 200 and c.get("/static/logo.webp").status_code == 200
            assert "אימייל" in c.get("/login").text and c.get("/register").status_code == 200

            # הרשמה מדומיין החברה: נכנס מיד. האימייל נשמר באותיות קטנות, והסיסמה כ-bcrypt
            c, r = register(" Dana@Corp.co ")
            assert r.status_code == 303 and r.headers["location"] == "/"
            assert c.get("/api/me").json()["user"] == {"id": 1, "email": "dana@corp.co", "name": "עובד", "role": "user"}
            hashed = conn.execute("select password_hash from users where email = 'dana@corp.co'").fetchone()[0]
            assert hashed.startswith("$2b$") and PASSWORD not in hashed
            cookie = r.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=2592000" in cookie

            # כתובת מבחוץ: ממתינה לאישור admin, גם אחרי סיסמה נכונה
            _, r = register("guest@gmail.com")
            assert r.headers["location"] == "/login?registered=1" and "set-cookie" not in r.headers
            r = client().post("/login", data={"email": "guest@gmail.com", "password": PASSWORD})
            assert r.headers["location"] == "/login?error=pending" and "set-cookie" not in r.headers

            assert register("dana@corp.co")[1].headers["location"] == "/register?error=exists"
            assert register("x@corp.co", password="קצרה")[1].headers["location"] == "/register?error=password"
            assert register("x@corp.co", password="א" * 40)[1].headers["location"] == "/register?error=password"
            assert register("not-an-email")[1].headers["location"] == "/register?error=email"
            assert register("x@corp.co", name=" ")[1].headers["location"] == "/register?error=name"

            # כניסה: סיסמה שגויה, הפניה רק בתוך האתר, ויציאה
            c = client()
            wrong = c.post("/login", data={"email": "dana@corp.co", "password": "nope", "next": "/admin/sync"})
            assert wrong.headers["location"] == "/login?error=1&next=/admin/sync" and "session" not in c.cookies
            for evil in ("//evil.com", "https://evil.com", "/\\evil.com"):
                r = c.post("/login", data={"email": "dana@corp.co", "password": PASSWORD, "next": evil})
                assert r.headers["location"] == "/"
            assert c.post("/login", data={"email": "DANA@corp.co", "password": PASSWORD,
                                          "next": "/#drafts"}).headers["location"] == "/#drafts"
            assert c.get("/").status_code == 200 and c.get("/").headers["x-frame-options"] == "DENY"
            assert conn.execute("select last_login_at is not null from users where id = 1").fetchone()[0]

            # CSRF: שינוי שנשלח מאתר אחר נחסם; מאותו אתר (או בלי Origin) עובר
            assert c.post("/api/history", json=DRAFT, headers={"origin": "https://evil.com"}).status_code == 403
            assert c.post("/api/history", json=DRAFT, headers={"origin": "http://testserver"}).status_code == 200
            assert c.post("/login", data={}, headers={"origin": "https://evil.com"}).status_code == 403

            r = c.post("/logout")
            assert r.headers["location"] == "/login" and c.get("/api/me").status_code == 401

            # עוגייה מזויפת, ישנה (30 יום) או של משתמש אחר בלי חתימה תקפה: 401
            old = auth.cookie(1, 1, time.time() - auth.SESSION_DAYS * 24 * 3600 - 1)
            for bad in (old, "1.1.9999999999.forged", auth.cookie(1, 1)[:-1] + "x", auth.cookie(1, 2), "garbage"):
                assert TestClient(main.app, cookies={"session": bad}).get("/api/me").status_code == 401, bad
            assert TestClient(main.app, cookies={"session": auth.cookie(1, 1)}).get("/api/me").status_code == 200

            # ניחוש סיסמה: אחרי LOGIN_FAILS כישלונות לאותה כתובת, גם מ-IP אחר ועם הסיסמה הנכונה, חסום
            for i in range(main.LOGIN_FAILS):
                client().post("/login", data={"email": "dana@corp.co", "password": "nope"},
                              headers={"x-forwarded-for": f"9.9.9.{i}"})
            r = client().post("/login", data={"email": "dana@corp.co", "password": PASSWORD})
            assert "error=locked" in r.headers["location"] and "set-cookie" not in r.headers

            # הרשמות: עד REGISTRATIONS לכל IP ב-15 דקות
            for i in range(main.REGISTRATIONS):
                register(f"spam{i}@gmail.com")
            assert register("one-more@gmail.com")[1].headers["location"] == "/register?error=locked"

    with mock.patch.object(auth, "SECRET", ""):  # בלי SECRET_KEY: האתר חסום, לא פתוח לכולם
        c = client()
        assert c.get("/").status_code == 503 and c.get("/api/me").status_code == 503
        assert c.get("/healthz").status_code == 200


def test_users_cannot_reach_each_others_drafts_and_chats():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        with site(conn):
            a, b = user(conn, "a@corp.co"), user(conn, "b@corp.co")
            draft_id = a.post("/api/history", json=DRAFT).json()["id"]
            assert [d["id"] for d in a.get("/api/history").json()] == [draft_id]

            # ב' לא רואה, לא פותח, לא מדרג ולא מוחק את הטיוטה של א'
            assert b.get("/api/history").json() == []
            assert b.get(f"/api/history/{draft_id}").status_code == 404
            assert b.put(f"/api/history/{draft_id}/rating", json={"rating": 1}).status_code == 404
            assert b.delete(f"/api/history/{draft_id}").status_code == 404
            # שמירה עם ה-id של א' לא משנה את הטיוטה שלו: נוצרת טיוטה חדשה של ב'
            other = b.post("/api/history", json=DRAFT | {"id": draft_id, "title": "של ב'"}).json()["id"]
            assert other != draft_id
            mine = a.get(f"/api/history/{draft_id}").json()
            assert mine["title"] == "הסכם" and mine["rating"] is None
            assert [d["title"] for d in b.get("/api/history").json()] == ["של ב'"]

            # שיחות "שאלו": נשמרות אחרי התשובה, עם הציטוטים, וממשיכות מההיסטוריה שבמסד
            asked = []

            def fake_ask(messages, conversation=None, user_id=None):
                asked.append((messages, user_id))
                return {"answer": f"תשובה {len(asked)}", "citations": [{"n": 1, "name": "הסכם.pdf"}]}

            with mock.patch.object(ask, "ask", fake_ask):
                r = a.post("/api/ask", json={"messages": [{"role": "user", "content": "שאלה ראשונה"}],
                                             "conversation": "c1"}).json()
                chat_id = r["chat_id"]
                r = a.post("/api/ask", json={"chat_id": chat_id, "messages": [
                    {"role": "user", "content": "מזויף"}, {"role": "assistant", "content": "מזויף"},
                    {"role": "user", "content": "שאלת המשך"}]}).json()
                assert r["chat_id"] == chat_id
                # ההיסטוריה מהמסד ולא מהדפדפן; למודל נשלחים רק role ו-content
                assert asked[1] == ([{"role": "user", "content": "שאלה ראשונה"},
                                     {"role": "assistant", "content": "תשובה 1"},
                                     {"role": "user", "content": "שאלת המשך"}], a.get("/api/me").json()["user"]["id"])

                # ב' לא ממשיך את השיחה של א', ולא נקראת שום קריאה למודל
                r = b.post("/api/ask", json={"chat_id": chat_id, "messages": [{"role": "user", "content": "?"}]})
                assert r.status_code == 404 and len(asked) == 2

            got = a.get(f"/api/chats/{chat_id}").json()
            assert got["title"] == "שאלה ראשונה" and len(got["messages"]) == 4
            assert got["messages"][1]["citations"] == [{"n": 1, "name": "הסכם.pdf"}]
            assert [c["id"] for c in a.get("/api/chats").json()] == [chat_id]
            assert b.get("/api/chats").json() == []
            assert b.get(f"/api/chats/{chat_id}").status_code == 404
            assert b.delete(f"/api/chats/{chat_id}").status_code == 404
            assert a.delete(f"/api/chats/{chat_id}").json() == {"ok": True}
            assert a.get(f"/api/chats/{chat_id}").status_code == 404


def test_admin_manages_users():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        with site(conn):
            boss, worker = user(conn, "boss@corp.co", "admin"), user(conn, "worker@corp.co")
            for path in ("/api/admin/users", "/api/costs", "/api/costs/xlsx", "/admin/sync", "/admin/users"):
                assert worker.get(path).status_code == 403, path
            assert worker.post("/api/admin/users", json={"email": "x@y.co", "name": "x", "password": PASSWORD}
                               ).status_code == 403
            assert boss.get("/api/costs").status_code == 200 and "<table" in boss.get("/admin/users").text

            # admin יוצר משתמש; כתובת קיימת, אימייל או סיסמה לא תקינים נדחים
            new = {"email": "New@Corp.co", "name": "חדש", "password": PASSWORD}
            new_id = boss.post("/api/admin/users", json=new).json()["id"]
            assert boss.post("/api/admin/users", json=new).status_code == 409
            assert boss.post("/api/admin/users", json=new | {"email": "bad"}).status_code == 400
            assert boss.post("/api/admin/users", json=new | {"email": "z@corp.co", "password": "short"}).status_code == 400
            listed = {u["email"]: u for u in boss.get("/api/admin/users").json()}
            assert listed["new@corp.co"]["role"] == "user" and listed["new@corp.co"]["active"]
            assert "password_hash" not in listed["new@corp.co"]

            # אישור משתמש שנרשם מבחוץ
            register("guest@gmail.com")
            guest = conn.execute("select id from users where email = 'guest@gmail.com'").fetchone()[0]
            assert boss.patch(f"/api/admin/users/{guest}", json={"active": True}).json() == {"ok": True}
            assert client().post("/login", data={"email": "guest@gmail.com", "password": PASSWORD}
                                 ).headers["location"] == "/"

            # סיסמה חדשה למי ששכח: הישנה כבר לא עובדת, והעוגיות הקיימות שלו מנותקות
            worker_id = worker.get("/api/me").json()["user"]["id"]
            assert boss.patch(f"/api/admin/users/{worker_id}", json={"password": "חדשה-וארוכה-2"}).status_code == 200
            assert worker.get("/api/me").status_code == 401
            assert client().post("/login", data={"email": "worker@corp.co", "password": PASSWORD}
                                 ).headers["location"].startswith("/login?error=1")
            worker = client()
            assert worker.post("/login", data={"email": "worker@corp.co", "password": "חדשה-וארוכה-2"}
                               ).headers["location"] == "/"

            # השבתה: מנותק מיד, ולא נכנס שוב
            assert boss.patch(f"/api/admin/users/{worker_id}", json={"active": False}).status_code == 200
            assert worker.get("/api/history").status_code == 401
            assert client().post("/login", data={"email": "worker@corp.co", "password": "חדשה-וארוכה-2"}
                                 ).headers["location"] == "/login?error=pending"

            # admin לא משבית את עצמו ולא מוריד לעצמו הרשאות (כדי לא להישאר בלי admin)
            boss_id = boss.get("/api/me").json()["user"]["id"]
            assert boss.patch(f"/api/admin/users/{boss_id}", json={"active": False}).status_code == 400
            assert boss.patch(f"/api/admin/users/{boss_id}", json={"role": "user"}).status_code == 400
            assert boss.patch("/api/admin/users/999999", json={"active": True}).status_code == 404
            assert boss.patch(f"/api/admin/users/{new_id}", json={"role": "admin"}).status_code == 200


def test_bootstrap_admin_claims_drafts_from_before_users():
    with postgres() as conn:
        if conn is None:
            return skip("אין py-pglite או Node")
        store.init(conn)
        conn.execute("insert into drafts (title, html, text, messages, sources) values ('ישנה', '', '', '[]', '[]')")
        env = {"ADMIN_EMAIL": "Boss@Corp.co", "ADMIN_PASSWORD": PASSWORD}
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "boss@corp.co", "ADMIN_PASSWORD": "short"}):
            main._bootstrap_admin(conn)  # סיסמה חלשה: לא נוצר admin
        assert store.needs_admin(conn)
        with mock.patch.dict(os.environ, env):
            main._bootstrap_admin(conn)
            main._bootstrap_admin(conn)  # כבר יש admin: לא קורה כלום
        assert conn.execute("select count(*), min(role), min(email) from users").fetchone() == (1, "admin", "boss@corp.co")
        assert conn.execute("select count(*) from drafts where user_id is null").fetchone()[0] == 0
        with site(conn):
            c = client()
            assert c.post("/login", data={"email": "boss@corp.co", "password": PASSWORD}).headers["location"] == "/"
            assert [d["title"] for d in c.get("/api/history").json()] == ["ישנה"]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for test in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        test()
        print("ok ", test.__name__)
