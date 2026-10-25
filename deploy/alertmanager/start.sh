#!/bin/sh
# Alertmanager entrypoint, shared by docker compose and Kubernetes.
#
# No Telegram credentials: runs alertmanager.yml (every receiver -> local alert sink).
# TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set: renders alertmanager-telegram.yml into a
# private runtime dir, with the token in a 0600 file (bot_token_file), and runs that.
# The variables come from a git-ignored .env (compose) or the optional Kubernetes Secret
# "alertmanager-telegram" (make alerts-telegram). Nothing secret is in the repo.
#
# ALERTMANAGER_CHECK_ONLY=1 validates the chosen config with amtool and exits.
set -eu

CONF_DIR=${ALERTMANAGER_CONF_DIR:-/etc/alertmanager}
RUNTIME_DIR=${ALERTMANAGER_RUNTIME_DIR:-/tmp/alertmanager}
BIN=${ALERTMANAGER_BIN:-/bin/alertmanager}
AMTOOL=${AMTOOL_BIN:-/bin/amtool}

token=${TELEGRAM_BOT_TOKEN:-}
chat_id=$(printf '%s' "${TELEGRAM_CHAT_ID:-}" | tr -d ' \r\n')
# Keep the token out of the Alertmanager process environment.
unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID

config=$CONF_DIR/alertmanager.yml
if [ -n "$token" ] || [ -n "$chat_id" ]; then
  if [ -z "$token" ] || [ -z "$chat_id" ]; then
    echo "start.sh: set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither" >&2
    exit 1
  fi
  case ${chat_id#-} in
    '' | *[!0-9]*)
      echo "start.sh: TELEGRAM_CHAT_ID must be an integer (e.g. 123456789 or -100123456789)" >&2
      exit 1 ;;
  esac
  umask 077
  mkdir -p "$RUNTIME_DIR"
  printf '%s' "$token" > "$RUNTIME_DIR/bot_token"
  sed -e "s|__TELEGRAM_BOT_TOKEN_FILE__|$RUNTIME_DIR/bot_token|" \
      -e "s|__TELEGRAM_CHAT_ID__|$chat_id|" \
      -e "s|__CONF_DIR__|$CONF_DIR|" \
      "$CONF_DIR/alertmanager-telegram.yml" > "$RUNTIME_DIR/alertmanager.yml"
  config=$RUNTIME_DIR/alertmanager.yml
  echo "start.sh: Telegram receiver enabled for page and warn alerts (chat $chat_id)"
else
  echo "start.sh: no Telegram credentials; alerts go to the local alert sink only"
fi

if [ "${ALERTMANAGER_CHECK_ONLY:-}" = 1 ]; then
  exec "$AMTOOL" check-config "$config"
fi
exec "$BIN" --config.file="$config" "$@"
