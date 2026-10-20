<div dir="rtl">

# מנסח ההסכמים

אפליקציית ווב פנימית שמנסחת הסכמים בעברית בעזרת OpenAI (gpt-4o), על בסיס מאגר הסכמים קיים ב-Google Drive.

> הפרויקט נבנה בשלבים. האתר רץ ב-Render, והסנכרון היומי רץ ב-GitHub Actions.

## דרישות מקדימות
- Python 3.11 ומעלה
- חשבון Google עם גישה לתיקיית ההסכמים

## הגדרת פרויקט Google Cloud

### 1. יצירת פרויקט והפעלת ה-API
1. היכנס ל-[Google Cloud Console](https://console.cloud.google.com/) וצור פרויקט חדש (למשל `agreements-drafter`).
2. בתפריט: **APIs & Services → Library**, חפש **Google Drive API** ולחץ **Enable**.

### 2. מסך ההסכמה (OAuth consent screen)
1. **APIs & Services → OAuth consent screen** (נקרא גם **Google Auth Platform**).
2. סוג משתמש: **External** (או **Internal** אם יש לכם Google Workspace).
3. מלא שם אפליקציה ומייל תמיכה.
4. ב-**Data Access / Scopes** הוסף את ההרשאה `https://www.googleapis.com/auth/drive.readonly` (קריאה בלבד).
5. **חשוב:** ב-**Audience** לחץ **Publish app** כדי להעביר את האפליקציה לסטטוס **In production**.
   באפליקציה בסטטוס *Testing*, ה-refresh token **פג אחרי 7 ימים** והסנכרון יפסיק לעבוד.
   אין צורך לעבור אימות (verification) של Google לשימוש פנימי. בזמן ההתחברות תופיע אזהרת "Google hasn't verified this app". לחץ **Advanced → Go to ... (unsafe)**.

### 3. יצירת פרטי OAuth
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. סוג אפליקציה: **Desktop app**.
3. הורד את קובץ ה-JSON ושמור אותו בתיקיית הפרויקט בשם `client_secret.json`. הקובץ כבר מוחרג ב-`.gitignore`, אל תעלה אותו ל-git.

### 4. צמצום הרשאות (מומלץ)
ל-Google אין הרשאה שמגבילה גישה לתיקייה אחת בלבד, ולכן `drive.readonly` מאפשר קריאה של **כל** ה-Drive של החשבון המחובר.
מומלץ ליצור חשבון Google ייעודי (למשל `agreements-bot@...`), לשתף איתו **רק** את תיקיית ההסכמים (הרשאת צפייה), ולהתחבר בשלב הבא עם החשבון הזה.

### 5. הפקת refresh token (פעם אחת)
</div>

```bash
python -m venv .venv
.venv\Scripts\activate          # ב-Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scripts/get_refresh_token.py client_secret.json
```

<div dir="rtl">

ייפתח דפדפן. התחבר עם החשבון שיש לו גישה לתיקייה ואשר. הסקריפט ידפיס שלוש שורות.
העתק את `.env.example` לקובץ `.env` והדבק אותן לתוכו.

## בדיקת שלב 1
</div>

```bash
python sync.py --list
```

<div dir="rtl">

אמורה להופיע רשימת כל הקבצים בתיקייה (כולל תתי-תיקיות): תאריך שינוי, סוג קובץ ושם.

## איפה שמים סודות
הסודות בפרויקט הם `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL` ו-`APP_PASSWORD`. הרשימה המלאה של המשתנים, עם הסבר מאיפה משיגים כל אחד, נמצאת ב-[`.env.example`](.env.example).

### פיתוח מקומי
- הסודות נשמרים **רק** בקובץ `dev\.env`. הקובץ מוחרג ב-`.gitignore`.
- תיקיית הפרויקט מסונכרנת ב-**Qsync**, ולכן `.env` מועתק גם לשרת הסנכרון ולכל מחשב מסונכרן.
  מומלץ להחריג אותו מהסנכרון: ב-Qsync ← **Preferences ← Filter Settings**, הוסף את שם הקובץ `.env` לרשימת הקבצים המסוננים.

### פרודקשן (Render)
- נכנסים ל-Render ← השירות `agreements-drafter` ← **Environment**.
- מזינים שם ידנית כל משתנה שמוגדר ב-[`render.yaml`](render.yaml) עם `sync: false`.
- Render לא קורא את קובץ `.env`, והערכים לעולם לא נכנסים לקוד.

### אסור
- להעלות סודות ל-git, כולל בקבצים שנמחקו אחר כך, כי הם נשארים בהיסטוריה.
- להדביק סודות בצ'אט, במייל או בהודעות.
- לשמור סודות בקבצי טקסט פזורים, כמו `client.txt` או `token.txt`.

### אם סוד דלף
מבטלים אותו מיד ומנפיקים חדש:

| סוד | איך מבטלים ומנפיקים מחדש |
|---|---|
| `GOOGLE_CLIENT_SECRET` | Google Cloud Console ← Credentials ← ה-OAuth client ← **Reset secret**. אחר כך מפיקים refresh token חדש |
| `GOOGLE_REFRESH_TOKEN` | [myaccount.google.com/permissions](https://myaccount.google.com/permissions) ← האפליקציה ← **Remove access**. אחר כך מריצים שוב `scripts/get_refresh_token.py` |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) ← Revoke למפתח ויצירת מפתח חדש |
| `DATABASE_URL` | Neon ← הפרויקט ← Roles ← Reset password, ואז מעתיקים את ה-connection string החדש |
| `APP_PASSWORD` | בוחרים סיסמה חדשה |

אחרי ההנפקה מחדש מעדכנים את הערך החדש ב-`.env` וב-Render ← Environment.

## הרצה מקומית
</div>

```bash
python sync.py                          # סנכרון: מאנדקס רק קבצים חדשים או ששונו
python -m app.draft "תיאור ההסכם"       # בדיקת מנוע הניסוח מהטרמינל
uvicorn app.main:app --reload           # האתר בכתובת http://127.0.0.1:8000
```

<div dir="rtl">

- לוג ריצות הסנכרון: `/admin/sync` (אחרי כניסה לאתר).
- הסנכרון היומי רץ ב-GitHub Actions ([`.github/workflows/sync.yml`](.github/workflows/sync.yml)), והרצה ידנית: Actions ← sync ← **Run workflow**.
- סריקות בלי שכבת טקסט עוברות OCR מקומי עם Tesseract (עברית + אנגלית), ולא דרך מודל. ב-Actions הוא מותקן אוטומטית.
  להרצה ב-Windows: מתקינים את ה-installer של [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki), ובמסך הרכיבים מסמנים
  **Additional language data → Hebrew**. הקוד מחפש את `tesseract` ב-PATH, ואחר כך ב-`C:\Program Files\Tesseract-OCR\tesseract.exe`.
  נתיב אחר: מגדירים `TESSERACT_CMD`. בדיקה: `python -m app.extract --ocr scan.pdf`.
- קובץ שנכשל בחילוץ או ב-OCR לא נכנס למאגר, ולכן ינוסה שוב בריצת הסנכרון הבאה.

### תקלות נפוצות
| שגיאה | פתרון |
|---|---|
| `invalid_grant` | ה-refresh token פג או בוטל. ודא שהאפליקציה ב-*In production* והרץ שוב את `get_refresh_token.py` |
| `File not found` / רשימה ריקה | לחשבון המחובר אין גישה לתיקייה, או ש-`DRIVE_FOLDER_ID` שגוי |
| `KeyError: 'GOOGLE_...'` | קובץ `.env` חסר או לא מלא |

</div>
