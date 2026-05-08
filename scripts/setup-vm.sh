#!/usr/bin/env bash
# Run once on the VM as serverboi to set up playlist backups and crash alerting.
# The deploy workflow handles .env and webhook config automatically after this.
set -e

MUSICBOT_DIR=/home/serverboi/musicbot

# --- Playlist backup cron ---
BACKUP_DIR=/home/serverboi/musicbot-backups
mkdir -p "$BACKUP_DIR"
CRON_LINE="0 2 * * * tar -czf $BACKUP_DIR/playlists-\$(date +\%Y\%m\%d).tar.gz -C $MUSICBOT_DIR playlists && find $BACKUP_DIR -name 'playlists-*.tar.gz' -mtime +30 -delete"
if crontab -l 2>/dev/null | grep -qF "musicbot-backups"; then
  echo "Backup cron already installed."
else
  (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
  echo "Backup cron installed — runs daily at 02:00, keeps 30 days in $BACKUP_DIR"
fi

# --- Crash alerting systemd setup ---
# The webhook URL / DM user ID are written by the deploy workflow on each push.
sudo mkdir -p /etc/systemd/system/musicbot.service.d
sudo cp "$MUSICBOT_DIR/scripts/crash-notify.conf" /etc/systemd/system/musicbot.service.d/
sudo cp "$MUSICBOT_DIR/scripts/musicbot-notify@.service" /etc/systemd/system/
sudo systemctl daemon-reload
echo "Crash alerting systemd files installed."

echo ""
echo "Done. Push to main to trigger a deploy — the webhook/DM config will be written automatically."
