#!/bin/sh
set -eu

DOMAIN="${LETSENCRYPT_DOMAIN:-_}"
CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
TARGET_CONF="/etc/nginx/conf.d/default.conf"

if [ "${DOMAIN}" != "_" ] && [ -f "${CERT_DIR}/fullchain.pem" ] && [ -f "${CERT_DIR}/privkey.pem" ]; then
  TEMPLATE="/etc/nginx/templates/https.conf.template"
  echo "Starting nginx with HTTPS config for ${DOMAIN}"
else
  TEMPLATE="/etc/nginx/templates/http.conf.template"
  echo "Certificate not found for ${DOMAIN}; starting nginx in HTTP mode"
fi

envsubst '${LETSENCRYPT_DOMAIN}' < "${TEMPLATE}" > "${TARGET_CONF}"

exec nginx -g "daemon off;"
