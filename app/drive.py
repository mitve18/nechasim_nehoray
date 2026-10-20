"""גישה ל-Google Drive: OAuth 2.0 עם refresh token (offline), קריאה בלבד."""
import io
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"


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
    """כל הקבצים בתיקייה, כולל תתי-תיקיות. מחזיר dicts עם id, name, mimeType, modifiedTime, webViewLink."""
    folder_id = folder_id or os.environ["DRIVE_FOLDER_ID"]
    files, pending = [], [folder_id]
    while pending:
        parent = pending.pop()
        token = None
        while True:
            resp = svc.files().list(
                q=f"'{parent}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for f in resp["files"]:
                (pending.append(f["id"]) if f["mimeType"] == FOLDER_MIME else files.append(f))
            token = resp.get("nextPageToken")
            if not token:
                break
    return files


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
