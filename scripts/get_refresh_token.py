"""הרצה חד-פעמית במחשב מקומי: פותח דפדפן להתחברות ל-Google ומדפיס refresh token.

שימוש: python scripts/get_refresh_token.py client_secret.json
"""
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
# access_type=offline + prompt=consent מבטיחים שיוחזר refresh token
creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")
print("\nGOOGLE_CLIENT_ID=" + creds.client_id)
print("GOOGLE_CLIENT_SECRET=" + creds.client_secret)
print("GOOGLE_REFRESH_TOKEN=" + creds.refresh_token)
print("\nהעתק את השורות האלה לקובץ .env (ולמשתני הסביבה ב-Render/GitHub). אל תעלה אותן ל-git.")
