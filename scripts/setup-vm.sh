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

# --- Disk cleanup ---
# The root LV is small; the runner and VS Code Remote server fill it and take
# the bot down with them. Cap the journal and prune the rest weekly.
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

CLEAN_LINE="0 4 * * 0 find /home/serverboi/actions-runner/_diag -type f -mtime +7 -delete 2>/dev/null; find /home/serverboi/.vscode-server/cli/servers -maxdepth 1 -type d -atime +30 -exec rm -rf {} + 2>/dev/null; rm -rf /home/serverboi/.cache/yt-dlp"
if crontab -l 2>/dev/null | grep -qF "actions-runner/_diag"; then
  echo "Cleanup cron already installed."
else
  (crontab -l 2>/dev/null; echo "$CLEAN_LINE") | crontab -
  echo "Cleanup cron installed — runs Sundays at 04:00."
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
