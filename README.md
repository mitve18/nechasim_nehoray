<div dir="rtl">

# מנסח ההסכמים

אפליקציית ווב פנימית שמנסחת הסכמים בעברית בעזרת OpenAI (gpt-4o), על בסיס מאגר הסכמים קיים ב-Google Drive.

> הפרויקט נבנה בשלבים. האתר רץ ב-Render, והסנכרון היומי רץ ב-GitHub Actions.

## איך הפרויקט משתמש ב-Agno
מנוע הניסוח ([`app/draft.py`](app/draft.py)) הוא Agent של [Agno](https://docs.agno.com) (גרסה מוצמדת ב-`requirements.txt`):
- **מודל:** `OpenAIResponses(id="gpt-4o")`.
- **Tool:** `search_agreements` מחפש הסכמים דומים ב-pgvector ב-Neon (`store.search`). ה-Agent מחליט על שאילתת החיפוש בעצמו.
  בחרנו tool ולא Knowledge של Agno, כי הטבלה הקיימת שומרת גם תקציר, טקסט מלא וקישור ל-Drive, והסנכרון משתמש בכולם. כך לא נדרשת מיגרציה.
- **פלט מובנה:** `output_schema=Draft` (status, questions, title, body). רשימת המקורות נאספת מתוך קריאות ה-tool ומוחזרת לממשק.
- גם התקצירים בסנכרון נוצרים ב-Agent של Agno. ה-OCR נשאר Tesseract.

</div>

```
משתמש ──► /api/draft ──► Agent (Agno)
                            │  קריאה ל-tool
                            ▼
                     search_agreements ──► Neon (pgvector) ──► הסכמים דומים
                            │
                            ▼
                     מודל gpt-4o ──► תשובה מובנית (questions / agreement) + מקורות ──► משתמש
```

<div dir="rtl">

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
4. ב-**Data Access / Scopes** הוסף את ההרשאה `https://www.googleapis.com/auth/drive` (קריאה וכתיבה מלאה).
5. **חשוב:** ב-**Audience** לחץ **Publish app** כדי להעביר את האפליקציה לסטטוס **In production**.
   באפליקציה בסטטוס *Testing*, ה-refresh token **פג אחרי 7 ימים** והסנכרון יפסיק לעבוד.
   אין צורך לעבור אימות (verification) של Google לשימוש פנימי. בזמן ההתחברות תופיע אזהרת "Google hasn't verified this app". לחץ **Advanced → Go to ... (unsafe)**.

### 3. יצירת פרטי OAuth
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. סוג אפליקציה: **Desktop app**.
3. הורד את קובץ ה-JSON ושמור אותו בתיקיית הפרויקט בשם `client_secret.json`. הקובץ כבר מוחרג ב-`.gitignore`, אל תעלה אותו ל-git.

### 4. צמצום הרשאות (מומלץ)
ל-Google אין הרשאה שמגבילה גישה לתיקייה אחת בלבד, ולכן `drive` מאפשר קריאה, כתיבה ומחיקה של **כל** ה-Drive של החשבון המחובר.
מומלץ ליצור חשבון Google ייעודי (למשל `agreements-bot@...`), לשתף איתו **רק** את תיקיית ההסכמים (הרשאת עריכה), ולהתחבר בשלב הבא עם החשבון הזה.

> אם ה-refresh token הופק עם scope אחר (למשל `drive.readonly`), הסנכרון ייכשל עם `invalid_scope`. צריך להפיק token חדש.

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
הסודות בפרויקט הם `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL`, `APP_PASSWORD` ו-`GH_DISPATCH_TOKEN`. הרשימה המלאה של המשתנים, עם הסבר מאיפה משיגים כל אחד, נמצאת ב-[`.env.example`](.env.example).

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
| `APP_PASSWORD` | בוחרים סיסמה חדשה ב-Render. כל מי שנכנס עם הסיסמה הישנה מתנתק אוטומטית |
| `GH_DISPATCH_TOKEN` | GitHub ← Settings ← Developer settings ← Fine-grained tokens ← Revoke, ויוצרים token חדש |

אחרי ההנפקה מחדש מעדכנים את הערך החדש ב-`.env` וב-Render ← Environment.

## הרצה מקומית
</div>

```bash
python sync.py                          # סנכרון: קבצים חדשים, ששונו או שסומנו "נסה שוב", ו-backfill
python -m app.draft "תיאור ההסכם"       # בדיקת מנוע הניסוח מהטרמינל
python -m pytest tests                  # כל הבדיקות, בלי סודות (pip install pytest py-pglite; דורש Node)
uvicorn app.main:app --reload           # האתר בכתובת http://127.0.0.1:8000
```

<div dir="rtl">

- לוג ריצות הסנכרון: `/admin/sync`.
- הסנכרון היומי רץ ב-GitHub Actions ([`.github/workflows/sync.yml`](.github/workflows/sync.yml)), והרצה ידנית: Actions ← sync ← **Run workflow**.
- סריקות בלי שכבת טקסט עוברות OCR מקומי עם Tesseract (עברית + אנגלית), ולא דרך מודל. ב-Actions הוא מותקן אוטומטית.
  להרצה ב-Windows: מתקינים את ה-installer של [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki), ובמסך הרכיבים מסמנים
  **Additional language data → Hebrew**. הקוד מחפש את `tesseract` ב-PATH, ואחר כך ב-`C:\Program Files\Tesseract-OCR\tesseract.exe`.
  נתיב אחר: מגדירים `TESSERACT_CMD`. בדיקה: `python -m app.extract --ocr scan.pdf`.
- קובץ שנכשל בחילוץ או ב-OCR לא נכנס למאגר, ולכן ינוסה שוב בריצת הסנכרון הבאה.
- תמונות (png, jpg, tif, webp) עוברות OCR כמו PDF סרוק, כולל TIFF עם כמה עמודים.

### פאנל הקבצים ונתונים מובנים
- **טבלת `files`:** הסנכרון רושם כל קובץ שהוא רואה ב-Drive עם סטטוס: `indexed` (מאונדקס), `failed` (נכשל, עם הסיבה),
  `unsupported` (סוג לא נתמך) או `queued` (בתור: הועלה מהאתר או סומן "נסה שוב"). "לא נתמך" לא מכשיל את ה-workflow.
- **נתונים מובנים:** אותה קריאה למודל שכותבת את התקציר מחזירה גם סוג מסמך, צדדים, מספר תוכנית, מיקום, גוש/חלקה,
  תאריכים, סכומים ועמודים (עמודה `metadata` מסוג jsonb, ו-`end_date` בעמודה נפרדת). שדה שלא כתוב במסמך נשאר null.
- **Backfill:** בכל סנכרון, מסמכים בלי נתונים מובנים (או עם `metadata_version` ישן) מושלמים מהטקסט ששמור במסד,
  בלי הורדה מ-Drive ובלי OCR. מוסיפים שדה? מעלים את `METADATA_VERSION` ב-[`app/store.py`](app/store.py), והסנכרון הבא משלים.
- **כל האתר מאחורי סיסמה** (`APP_PASSWORD`): מסך כניסה בפעם הראשונה, והאתר זוכר את המשתמש ל-30 יום.
  פתוחים בלי כניסה רק `/login`, `/healthz` (בדיקת התקינות של Render) והלוגו. בלי `APP_PASSWORD` ב-Render האתר חסום,
  כדי שלא ייפתח לכולם בטעות. החלפת הסיסמה מנתקת את כל המחוברים.
  הקובץ נבדק בשרת לפי סיומת, גודל (עד 20MB) ותוכן, ומועלה לתיקייה הראשית. שם קיים מקבל סיומת (1), (2).
- **כפתור הסנכרון** מפעיל את ה-workflow דרך GitHub API (`GH_DISPATCH_TOKEN`, `GH_REPO`), לכל היותר פעם ב-5 דקות.

### תקלות נפוצות
| שגיאה | פתרון |
|---|---|
| `invalid_grant` | ה-refresh token פג או בוטל. ודא שהאפליקציה ב-*In production* והרץ שוב את `get_refresh_token.py` |
| `invalid_scope` | ה-token הופק עם scope אחר. הפק token חדש עם `https://www.googleapis.com/auth/drive` ועדכן את `GOOGLE_REFRESH_TOKEN` |
| `File not found` / רשימה ריקה | לחשבון המחובר אין גישה לתיקייה, או ש-`DRIVE_FOLDER_ID` שגוי |
| `KeyError: 'GOOGLE_...'` | קובץ `.env` חסר או לא מלא |

</div>
