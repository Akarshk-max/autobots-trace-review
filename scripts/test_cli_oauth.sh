#!/usr/bin/env bash
#
# test_cli_oauth.sh — manually exercise the CLI paste-back OAuth flow (flow 1).
#
# You play the CLI's role: this script holds the PKCE verifier + state, opens the
# authorize URL for you, then decodes the token you paste back from the browser and
# exchanges it for a session. See docs/cli_oauth_setup.md.
#
# Usage:
#   scripts/test_cli_oauth.sh                       # google, http://localhost:8000
#   scripts/test_cli_oauth.sh azure                 # azure/Outlook
#   scripts/test_cli_oauth.sh google https://dev.api.myautobots.com
#
# Prereqs (see docs/cli_oauth_setup.md):
#   - backend running (make dev)
#   - <BASE>/v1/auth/oauth/cli-callback whitelisted in the matching Supabase project
#   - the provider enabled in that Supabase project
#
set -euo pipefail

PROVIDER="${1:-google}"
BASE="${2:-http://localhost:8000}"
REDIRECT="${BASE}/v1/auth/oauth/cli-callback"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

# --- Step 1: get authorize URL (CLI's /oauth/url call) -----------------------------
say "1/5  Requesting authorize URL (provider=${PROVIDER})"
URL_RES="$(curl -sS "${BASE}/v1/auth/oauth/url?provider=${PROVIDER}&redirect_to=${REDIRECT}")"

read -r URL VERIFIER < <(
  printf '%s' "$URL_RES" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(d["url"], d["verifier"])'
) || { echo "Failed to parse /oauth/url response:"; echo "$URL_RES"; exit 1; }

echo "verifier = ${VERIFIER:0:12}… (held locally, never sent to the browser)"

# --- Step 2: open the URL in the browser -------------------------------------------
say "2/5  Opening the authorize URL in your browser — complete the sign in"
echo "$URL"
if command -v open >/dev/null; then open "$URL"          # macOS
elif command -v xdg-open >/dev/null; then xdg-open "$URL" # Linux
fi

# --- Step 3: paste the token from the landing page, decode to the code -------------
say "3/5  Paste the token shown on the landing page, then press Enter"
read -r TOKEN
CODE="$(printf '%s' "$TOKEN" | python3 -c \
  'import sys,base64,json; t=sys.stdin.read().strip(); print(json.loads(base64.urlsafe_b64decode(t+"="*(-len(t)%4)))["code"])')" \
  || { echo "Could not decode token"; exit 1; }
echo "code = ${CODE:0:16}…"

# --- Step 4: exchange code + verifier for a session (CLI's /oauth/exchange call) ---
say "4/5  Exchanging code + verifier for a session"
EXCHANGE_RES="$(curl -sS -X POST "${BASE}/v1/auth/oauth/exchange" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import sys,json; print(json.dumps({"code":sys.argv[1],"verifier":sys.argv[2]}))' "$CODE" "$VERIFIER")")"

read -r ACCESS REFRESH EXPIRES < <(printf '%s' "$EXCHANGE_RES" | python3 -c \
  'import sys,json; s=(json.load(sys.stdin).get("session") or {}); print(s.get("access_token",""), s.get("refresh_token",""), s.get("expires_in",""))' 2>/dev/null || true)

if [[ -z "$ACCESS" ]]; then
  echo "❌  Exchange failed:" >&2
  printf '%s\n' "$EXCHANGE_RES" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$EXCHANGE_RES"
  exit 1
fi
echo "✅  Got session — the CLI persists BOTH of these:"
echo "    access_token  = ${ACCESS:0:24}…   (send as 'Authorization: Bearer …', expires in ${EXPIRES}s)"
echo "    refresh_token = ${REFRESH:0:24}…   (POST /v1/auth/session/refresh?refresh_token=… to renew)"

# --- Step 5: confirm the session works ---------------------------------------------
say "5/5  Verifying the session via GET /v1/auth/user"
curl -sS "${BASE}/v1/auth/user" -H "Authorization: Bearer ${ACCESS}" \
  | python3 -m json.tool

say "Done — CLI paste-back flow works end to end."
