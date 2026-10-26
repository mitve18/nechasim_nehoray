"""משתמש מחובר לבדיקות שלא בודקות את הכניסה עצמה: בלי מסד ובלי עוגייה אמיתית. הכניסה נבדקת ב-test_users.py."""
import contextlib
from unittest import mock

from app import auth, main

ADMIN = {"id": 1, "email": "admin@test.co", "name": "מנהל", "role": "admin"}


@contextlib.contextmanager
def logged_in(user=ADMIN):
    with mock.patch.object(auth, "SECRET", "test-secret"), \
            mock.patch.object(main, "_session_user", lambda request: user):
        yield
