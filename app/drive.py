"""גישה ל-Google Drive: OAuth 2.0 עם refresh token (offline), הרשאת קריאה וכתיבה (scope: drive)."""
import hashlib
import io
import os
import re
import zipfile

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = "id, name, mimeType, modifiedTime, webViewLink, size"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC = "application/msword"
GDOC = "application/vnd.google-apps.document"
MAX_UPLOAD = 20 * 1024 * 1024


def _is_heif(d):
    """תמונת HEIC/HEIF (אייפון): בתים 4–8 הם "ftyp", ואחריהם סוג התמונה."""
    return d[4:8] == b"ftyp" and d[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def _is_docx(d):
    try:
        return d.startswith(b"PK\x03\x04") and "word/document.xml" in zipfile.ZipFile(io.BytesIO(d)).namelist()
    except zipfile.BadZipFile:
        return False


# סיומת מותרת להעלאה: (mime, בדיקת התוכן לפי magic bytes)
UPLOAD_TYPES = {
    "pdf": ("application/pdf", lambda d: d.startswith(b"%PDF-")),
    "png": ("image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    "jpg": ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    "tif": ("image/tiff", lambda d: d[:4] in (b"II*\x00", b"MM\x00*")),
    "tiff": ("image/tiff", lambda d: d[:4] in (b"II*\x00", b"MM\x00*")),
    "webp": ("image/webp", lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP"),
    "docx": (DOCX, _is_docx),
    "doc": (DOC, lambda d: d.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")),  # קובץ OLE של Office ישן
    "heic": ("image/heic", _is_heif),
    "heif": ("image/heif", _is_heif),
}


def service():
    creds = Credentials(
        None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_files(svc, folder_id=None):
    """הקבצים שישירות בתיקייה, בלי תתי-תיקיות (שם נשמרים תוצרים של האתר, שאסור לאנדקס). מחזיר dicts עם id, name,
    mimeType, modifiedTime, webViewLink.

    קיצור דרך לקובץ מוחלף בקובץ עצמו; קיצור דרך לתיקייה מדולג, כמו תיקייה. כל קובץ מופיע פעם אחת,
    גם כשיש אליו קיצור דרך.
    """
    folder_id = folder_id or os.environ["DRIVE_FOLDER_ID"]
    files, seen, token = [], set(), None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType != '{FOLDER_MIME}'",
            fields=f"nextPageToken, files({FILE_FIELDS}, shortcutDetails(targetId))",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in resp["files"]:
            if f["mimeType"] == SHORTCUT_MIME:
                f = _shortcut_target(svc, f)
            if f["mimeType"] != FOLDER_MIME and f["id"] not in seen:
                seen.add(f["id"])
                files.append(f)
        token = resp.get("nextPageToken")
        if not token:
            break
    return files


def _shortcut_target(svc, f):
    """היעד של קיצור דרך. אם אין אליו גישה, הקיצור נשאר עם שגיאה, והסנכרון ידווח עליו כנכשל."""
    try:
        return svc.files().get(fileId=f["shortcutDetails"]["targetId"], fields=FILE_FIELDS,
                               supportsAllDrives=True).execute()
    except HttpError as e:
        return {**f, "error": f"קיצור דרך לקובץ שאין אליו גישה ({e.status_code})"}


def download(svc, f):
    """תוכן הקובץ כ-bytes. מסמכי Google Docs מיוצאים כטקסט רגיל.

    Word 97-2003 (.doc): Drive יוצר עותק זמני שמומר ל-Google Docs, מייצא ממנו טקסט ומוחק אותו.
    כך אין צורך ב-LibreOffice, ו-Google מטפל היטב בעברית. העותק נוצר ב"האחסון שלי" של החשבון,
    מחוץ לתיקיית ההסכמים, ולכן הסנכרון לא רואה אותו."""
    if f["mimeType"] == DOC:
        tmp = svc.files().copy(fileId=f["id"], supportsAllDrives=True,
                               body={"name": f"tmp-convert {f['name']}", "mimeType": GDOC, "parents": ["root"]}).execute()
        try:
            return _fetch(svc.files().export_media(fileId=tmp["id"], mimeType="text/plain"))
        finally:
            svc.files().delete(fileId=tmp["id"], supportsAllDrives=True).execute()
    if f["mimeType"] == GDOC:
        return _fetch(svc.files().export_media(fileId=f["id"], mimeType="text/plain"))
    return _fetch(svc.files().get_media(fileId=f["id"], supportsAllDrives=True))


def _fetch(req):
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def check_upload(name, data):
    """ולידציה בשרת לפני העלאה: סיומת, גודל, ותוכן שתואם לסיומת. מחזיר (שם נקי, mime) או זורק ValueError."""
    name = "".join(ch for ch in re.split(r"[\\/]", name)[-1] if ch.isprintable()).strip()[:200]  # בלי נתיב
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in UPLOAD_TYPES:
        raise ValueError(f"סוג קובץ לא מותר: {name or 'בלי שם'}. מותר: " + ", ".join(UPLOAD_TYPES))
    if not data:
        raise ValueError(f"הקובץ ריק: {name}")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"הקובץ גדול מ-20MB: {name}")
    mime, content_ok = UPLOAD_TYPES[ext]
    if not content_ok(data):
        raise ValueError(f"תוכן הקובץ לא תואם לסיומת .{ext}: {name}")
    return name, mime


def unique_name(name, existing):
    """שם פנוי בתיקייה: "הסכם.pdf" ← "הסכם (1).pdf" ← "הסכם (2).pdf". לא דורסים קבצים קיימים."""
    stem, _, ext = name.rpartition(".")
    candidate, i = name, 0
    while candidate in existing:
        i += 1
        candidate = f"{stem} ({i}).{ext}"
    return candidate


def upload(svc, name, data, mime, folder_id=None):
    """מעלה לתיקיית ההסכמים (לא לתתי-תיקיות). מחזיר את הקובץ שנוצר, עם השם הסופי."""
    # ponytail: שתי העלאות במקביל עם אותו שם עלולות לקבל אותו שם; הממשק מעלה קובץ אחרי קובץ
    folder_id = folder_id or os.environ["DRIVE_FOLDER_ID"]
    names, token = set(), None
    while True:
        resp = svc.files().list(q=f"'{folder_id}' in parents and trashed=false", fields="nextPageToken, files(name)",
                                pageSize=1000, pageToken=token, supportsAllDrives=True,
                                includeItemsFromAllDrives=True).execute()
        names.update(f["name"] for f in resp["files"])
        token = resp.get("nextPageToken")
        if not token:
            break
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=True)
    return svc.files().create(body={"name": unique_name(name, names), "parents": [folder_id]}, media_body=media,
                              fields=FILE_FIELDS, supportsAllDrives=True).execute()


class UploadMismatch(Exception):
    pass


def verify_upload(svc, f, data, folder_id=None):
    """קורא מ-Drive את הקובץ שנוצר ומוודא שהוא בתיקיית ההסכמים, ושהגודל וה-md5 זהים למה שהתקבל.
    מחזיר את הקובץ עם webViewLink, או זורק UploadMismatch."""
    folder_id = folder_id or os.environ["DRIVE_FOLDER_ID"]
    got = svc.files().get(fileId=f["id"], fields="parents, size, md5Checksum, webViewLink",
                          supportsAllDrives=True).execute()
    if (folder_id not in got.get("parents", []) or got.get("size") != str(len(data))
            or got.get("md5Checksum") != hashlib.md5(data).hexdigest()):
        raise UploadMismatch("הקובץ לא נשמר בתיקייה כמו שצריך")
    return f | got
