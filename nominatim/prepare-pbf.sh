#!/usr/bin/env bash
set -Eeuo pipefail

# Если задан локальный PBF или URL отсутствует, оставляем штатное поведение образа.
if [[ -n "${PBF_PATH:-}" || -z "${PBF_URL:-}" ]]; then
  exec /app/start.sh
fi

cache_dir="${PBF_CACHE_DIR:-/var/cache/nominatim}"
cache_file="${cache_dir}/data.osm.pbf"
metadata_file="${cache_dir}/data.osm.pbf.source"
user_agent="${USER_AGENT:-municipal-low-code-nominatim}"

mkdir -p "${cache_dir}"

headers="$(curl -fsSIL --retry 5 --retry-delay 3 -A "${user_agent}" "${PBF_URL}")"
remote_size="$(
  printf '%s\n' "${headers}" \
    | awk 'BEGIN { IGNORECASE = 1 } /^content-length:/ { gsub("\\r", "", $2); size = $2 } END { print size }'
)"
remote_modified="$(
  printf '%s\n' "${headers}" \
    | awk 'BEGIN { IGNORECASE = 1 } /^last-modified:/ { sub(/^[^:]+:[[:space:]]*/, ""); gsub("\\r", ""); value = $0 } END { print value }'
)"

if [[ ! "${remote_size}" =~ ^[0-9]+$ ]] || (( remote_size <= 0 )); then
  echo "Не удалось определить размер PBF по адресу ${PBF_URL}" >&2
  exit 1
fi

stored_url=""
stored_size=""
stored_modified=""
if [[ -f "${metadata_file}" ]]; then
  IFS='|' read -r stored_url stored_size stored_modified < "${metadata_file}" || true
fi

local_size=0
if [[ -f "${cache_file}" ]]; then
  local_size="$(stat -c '%s' "${cache_file}")"
fi

same_source=false
if [[ "${stored_url}" == "${PBF_URL}" && "${stored_size}" == "${remote_size}" ]]; then
  if [[ -z "${remote_modified}" || "${stored_modified}" == "${remote_modified}" ]]; then
    same_source=true
  fi
fi

if [[ "${same_source}" == true && "${local_size}" == "${remote_size}" ]]; then
  echo "PBF уже полностью загружен: ${cache_file} (${local_size} байт)"
else
  if [[ "${same_source}" != true || "${local_size}" -gt "${remote_size}" ]]; then
    echo "Источник PBF изменился, начинаем загрузку заново"
    rm -f "${cache_file}"
    local_size=0
  fi

  printf '%s|%s|%s\n' "${PBF_URL}" "${remote_size}" "${remote_modified}" > "${metadata_file}"

  if (( local_size > 0 )); then
    echo "Продолжаем загрузку PBF с байта ${local_size}"
  else
    echo "Загружаем PBF: ${PBF_URL}"
  fi

  if curl -fL --retry 5 --retry-delay 3 -A "${user_agent}" \
    -C - --create-dirs -o "${cache_file}" "${PBF_URL}"; then
    curl_status=0
  else
    curl_status=$?
    downloaded_size=0
    if [[ -f "${cache_file}" ]]; then
      downloaded_size="$(stat -c '%s' "${cache_file}")"
    fi
    if [[ "${downloaded_size}" != "${remote_size}" ]]; then
      echo "Не удалось загрузить PBF: curl завершился с кодом ${curl_status}" >&2
      exit "${curl_status}"
    fi
    echo "Сервер вернул ошибку Range, но PBF уже загружен полностью"
  fi

  downloaded_size="$(stat -c '%s' "${cache_file}")"
  if [[ "${downloaded_size}" != "${remote_size}" ]]; then
    echo "Размер загруженного PBF ${downloaded_size} не совпадает с ожидаемым ${remote_size}" >&2
    exit 1
  fi
fi

chmod 0644 "${cache_file}"
export PBF_PATH="${cache_file}"
export PBF_URL=""

exec /app/start.sh
