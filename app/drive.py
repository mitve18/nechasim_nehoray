"""גישה ל-Google Drive: OAuth 2.0 עם refresh token (offline), הרשאת קריאה וכתיבה (scope: drive)."""
import io
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = "id, name, mimeType, modifiedTime, webViewLink"


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
    """כל הקבצים בתיקייה, כולל תתי-תיקיות. מחזיר dicts עם id, name, mimeType, modifiedTime, webViewLink.

    קיצור דרך מוחלף ביעד שלו: לקובץ הוא מאונדקס כקובץ, ולתיקייה היא נסרקת. כל קובץ ותיקייה מופיעים פעם אחת,
    גם כשיש אליהם קיצור דרך, וקיצור דרך לתיקיית אב לא יוצר לולאה.
    """
    folder_id = folder_id or os.environ["DRIVE_FOLDER_ID"]
    files, pending, seen = [], [folder_id], set()
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        token = None
        while True:
            resp = svc.files().list(
                q=f"'{parent}' in parents and trashed=false",
                fields=f"nextPageToken, files({FILE_FIELDS}, shortcutDetails(targetId))",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in resp["files"]:
                if f["mimeType"] == SHORTCUT_MIME:
                    f = _shortcut_target(svc, f)
                if f["mimeType"] == FOLDER_MIME:
                    pending.append(f["id"])
                elif f["id"] not in seen:
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
    """תוכן הקובץ כ-bytes. מסמכי Google Docs מיוצאים כטקסט רגיל."""
    if f["mimeType"] == "application/vnd.google-apps.document":
        req = svc.files().export_media(fileId=f["id"], mimeType="text/plain")
    else:
        req = svc.files().get_media(fileId=f["id"], supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()
