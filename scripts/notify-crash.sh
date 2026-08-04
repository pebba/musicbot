#!/usr/bin/env bash
BOT_DIR=/home/serverboi/musicbot
WEBHOOK_URL=$(cat "$BOT_DIR/.discord-webhook" 2>/dev/null)
DM_USER_ID=$(cat "$BOT_DIR/.discord-dm-user" 2>/dev/null)
BOT_TOKEN=$(grep -m1 '^DISCORD_TOKEN=' "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2-)

MSG='{"content": "⚠️ **musicbot** crashed — check `journalctl -u musicbot -n 50`"}'

if [ -n "$WEBHOOK_URL" ]; then
  curl -sf -X POST -H "Content-Type: application/json" -d "$MSG" "$WEBHOOK_URL"
fi

if [ -n "$BOT_TOKEN" ] && [ -n "$DM_USER_ID" ]; then
  DM_CHANNEL=$(curl -sf -X POST \
    -H "Authorization: Bot $BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"recipient_id\": \"$DM_USER_ID\"}" \
    "https://discord.com/api/v10/users/@me/channels" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
  if [ -n "$DM_CHANNEL" ]; then
    curl -sf -X POST \
      -H "Authorization: Bot $BOT_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$MSG" \
      "https://discord.com/api/v10/channels/$DM_CHANNEL/messages"
  fi
fi
