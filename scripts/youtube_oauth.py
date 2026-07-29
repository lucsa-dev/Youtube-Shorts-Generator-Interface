"""One-time OAuth flow to obtain a YouTube refresh token for a project.

Prerequisites:
  1. Google Cloud Console → enable "YouTube Data API v3"
  2. Create OAuth 2.0 Client ID (application type: Desktop)
  3. Pass Client ID / Secret via flags (or temporarily in .env)

Usage:
  python scripts/youtube_oauth.py --client-id ... --client-secret ...

Paste the printed refresh token into the Web UI:
  Projetos → seu canal → Config → Refresh Token
(or use the "Conectar YouTube" button there).
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Obter refresh token do YouTube para um projeto"
    )
    parser.add_argument("--client-id", default=os.getenv("YOUTUBE_CLIENT_ID", "").strip())
    parser.add_argument(
        "--client-secret", default=os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    )
    args = parser.parse_args()

    client_id = (args.client_id or "").strip().strip("'\"")
    client_secret = (args.client_secret or "").strip().strip("'\"")
    if not client_id or not client_secret:
        print(
            "Passe --client-id e --client-secret "
            "(ou defina YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET temporariamente).",
            file=sys.stderr,
        )
        return 1

    try:
        from shorts_generator.youtube_uploader import run_oauth_flow
    except ImportError:
        print(
            "Instale as deps: pip install google-auth-oauthlib google-api-python-client",
            file=sys.stderr,
        )
        return 1

    try:
        tokens = run_oauth_flow(client_id, client_secret)
    except Exception as exc:
        print(f"OAuth falhou: {exc}", file=sys.stderr)
        return 1

    print("\nCole na Config do projeto (Web UI):\n")
    print(f"Client ID:      {tokens['client_id']}")
    print(f"Client Secret:  {tokens['client_secret']}")
    print(f"Refresh Token:  {tokens['refresh_token']}")
    if tokens.get("channel_title"):
        print(f"Canal:          {tokens['channel_title']}")
    print("\nPrivacidade sugerida: private")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
