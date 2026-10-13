#!/bin/bash
# Install mx4ctl for the current user: CLI symlink, config, systemd user service.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ~/.local/bin ~/.config/mx4ctl ~/.config/systemd/user
ln -sf "$PWD/mx4ctl" ~/.local/bin/mx4ctl
ln -sf "$PWD/mx4ctl" ~/.local/bin/mx4
ln -sf "$PWD/mx4-wizard" ~/.local/bin/mx4-wizard
[ -f ~/.config/mx4ctl/config.ini ] || cp config.example.ini ~/.config/mx4ctl/config.ini
# point the unit at wherever this checkout actually lives
sed "s|^ExecStart=.*|ExecStart=$PWD/mx4ctl daemon|" mx4ctl.service > ~/.config/systemd/user/mx4ctl.service
# GNOME Wayland: the ring finds the pointer through this small shell extension
EXT=mx4ctl-cursor@mx4ctl
mkdir -p ~/.local/share/gnome-shell/extensions
rm -rf ~/.local/share/gnome-shell/extensions/$EXT
cp -r "gnome-extension/$EXT" ~/.local/share/gnome-shell/extensions/
gnome-extensions enable $EXT 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable mx4ctl.service
systemctl --user restart mx4ctl.service

echo "installed. status: systemctl --user status mx4ctl"
echo "config:    ~/.config/mx4ctl/config.ini (restart with: systemctl --user restart mx4ctl)"
echo "tuning:    mx4-wizard (interactive settings tuner)"
