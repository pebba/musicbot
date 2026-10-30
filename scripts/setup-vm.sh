#!/usr/bin/env bash
# Run once on the VM as the admin user (needs sudo) to set up the musicbot user,
# playlist backups and crash alerting. The bot and the GitHub runner run as
# musicbot, which has no sudo except restarting the bot, so CI jobs never get root.
# The deploy workflow handles .env and webhook config automatically after this.
set -e

MUSICBOT_USER=musicbot
MUSICBOT_HOME=/home/$MUSICBOT_USER
MUSICBOT_DIR=$MUSICBOT_HOME/musicbot

# --- Bot user ---
id "$MUSICBOT_USER" >/dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash "$MUSICBOT_USER"
sudo chmod 750 "$MUSICBOT_HOME"
# journalctl in the deploy health check
sudo usermod -aG systemd-journal "$MUSICBOT_USER"
# The only root command CI may run.
echo "$MUSICBOT_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart musicbot" > /tmp/sudoers-musicbot
sudo visudo -cf /tmp/sudoers-musicbot && sudo install -m 0440 -o root -g root /tmp/sudoers-musicbot /etc/sudoers.d/musicbot
rm -f /tmp/sudoers-musicbot

# --- Playlist backup cron ---
BACKUP_DIR=$MUSICBOT_HOME/musicbot-backups
sudo -u "$MUSICBOT_USER" mkdir -p "$BACKUP_DIR"
CRON_LINE="0 2 * * * tar -czf $BACKUP_DIR/playlists-\$(date +\%Y\%m\%d).tar.gz -C $MUSICBOT_DIR playlists && find $BACKUP_DIR -name 'playlists-*.tar.gz' -mtime +30 -delete"
if sudo crontab -u "$MUSICBOT_USER" -l 2>/dev/null | grep -qF "musicbot-backups"; then
  echo "Backup cron already installed."
else
  (sudo crontab -u "$MUSICBOT_USER" -l 2>/dev/null; echo "$CRON_LINE") | sudo crontab -u "$MUSICBOT_USER" -
  echo "Backup cron installed — runs daily at 02:00, keeps 30 days in $BACKUP_DIR"
fi

# --- Disk cleanup ---
# The root LV is small; the runner and VS Code Remote server fill it and take
# the bot down with them. Cap the journal and prune the rest weekly.
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

CLEAN_LINE="0 4 * * 0 find $MUSICBOT_HOME/actions-runner/_diag -type f -mtime +7 -delete 2>/dev/null; rm -rf $MUSICBOT_HOME/.cache/yt-dlp"
if sudo crontab -u "$MUSICBOT_USER" -l 2>/dev/null | grep -qF "actions-runner/_diag"; then
  echo "Cleanup cron already installed."
else
  (sudo crontab -u "$MUSICBOT_USER" -l 2>/dev/null; echo "$CLEAN_LINE") | sudo crontab -u "$MUSICBOT_USER" -
  echo "Cleanup cron installed — runs Sundays at 04:00."
fi
# VS Code Remote belongs to the admin user, so it is pruned from the admin crontab.
VSCODE_LINE="0 4 * * 0 find $HOME/.vscode-server/cli/servers -maxdepth 1 -type d -atime +30 -exec rm -rf {} + 2>/dev/null"
crontab -l 2>/dev/null | grep -qF ".vscode-server" || (crontab -l 2>/dev/null; echo "$VSCODE_LINE") | crontab -

# --- JS runtime for yt-dlp ---
# yt-dlp needs one for YouTube, or formats go missing. Installed to
# /usr/local/bin so the systemd service finds it (~/.deno/bin is not on its PATH).
if command -v deno >/dev/null; then
  echo "deno already installed — update it with: sudo deno upgrade"
else
  sudo apt-get install -y unzip
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/deno.zip" \
    "https://github.com/denoland/deno/releases/latest/download/deno-$(uname -m)-unknown-linux-gnu.zip"
  sudo unzip -o "$tmp/deno.zip" -d /usr/local/bin
  rm -rf "$tmp"
  echo "deno installed — update it with: sudo deno upgrade"
fi

# --- systemd units ---
# The webhook URL / DM user ID are written by the deploy workflow on each push.
sudo mkdir -p /etc/systemd/system/musicbot.service.d
sudo cp "$MUSICBOT_DIR/scripts/musicbot.service" /etc/systemd/system/
sudo cp "$MUSICBOT_DIR/scripts/crash-notify.conf" /etc/systemd/system/musicbot.service.d/
sudo cp "$MUSICBOT_DIR/scripts/musicbot-notify@.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable musicbot
echo "systemd units installed."

echo ""
echo "Done. Push to main to trigger a deploy — the webhook/DM config will be written automatically."
