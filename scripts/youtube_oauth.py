#!/usr/bin/env python3
"""One-time OAuth flow to obtain a YouTube refresh token for .env.

Prerequisites:
  1. Google Cloud Console → enable "YouTube Data API v3"
  2. Create OAuth 2.0 Client ID (application type: Desktop)
  3. Put Client ID / Secret in .env (or pass via flags)

Usage:
  python scripts/youtube_oauth.py
  python scripts/youtube_oauth.py --client-id ... --client-secret ...
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Obter YOUTUBE_REFRESH_TOKEN")
    parser.add_argument("--client-id", default=os.getenv("YOUTUBE_CLIENT_ID", "").strip())
    parser.add_argument(
        "--client-secret", default=os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    )
    args = parser.parse_args()

    client_id = (args.client_id or "").strip().strip("'\"")
    client_secret = (args.client_secret or "").strip().strip("'\"")
    if not client_id or not client_secret:
        print(
            "Defina YOUTUBE_CLIENT_ID e YOUTUBE_CLIENT_SECRET no .env "
            "(ou passe --client-id / --client-secret).",
            file=sys.stderr,
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Instale as deps: pip install google-auth-oauthlib google-api-python-client",
            file=sys.stderr,
        )
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print(
            "Nenhum refresh token retornado. Revogue o acesso do app em "
            "https://myaccount.google.com/permissions e tente de novo com prompt=consent.",
            file=sys.stderr,
        )
        return 1

    print("\nCole no seu .env:\n")
    print(f"YOUTUBE_CLIENT_ID={client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("YOUTUBE_PRIVACY_STATUS=private")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
