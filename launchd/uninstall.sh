#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
label=$(/usr/bin/plutil -extract Label raw "$script_dir/local.live-gantt.plist")
user_id=$(/usr/bin/id -u)
plist="$HOME/Library/LaunchAgents/$label.plist"
/bin/launchctl bootout "gui/$user_id/$label" 2>/dev/null || :
if [ -f "$plist" ]; then
    rm "$plist"
fi
printf '%s\n' 'Live Gantt uninstalled. Its log has been retained.'
