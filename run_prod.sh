#!/usr/bin/env bash
set -euo pipefail

# Run from script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

# Required inputs
: "${AWS_REGION:=us-east-1}"
: "${FROM_EMAIL:=${AUOTAM_FROM_EMAIL:-}}"
: "${AUOTAM_FROM_EMAIL:=${FROM_EMAIL:-}}"
: "${AUOTAM_FROM_EMAIL:?FROM_EMAIL or AUOTAM_FROM_EMAIL is required (set in .env)}"
: "${REPLY_TO:=${AUOTAM_REPLY_TO:-}}"
: "${AUOTAM_REPLY_TO:=${REPLY_TO:-}}"
: "${AUOTAM_REPLY_TO:?REPLY_TO or AUOTAM_REPLY_TO is required (set in .env)}"
: "${AUOTAM_FROM_NAME:=Govind Chauhan}"
: "${AUOTAM_DAILY_TARGET:=6000}"
: "${AUOTAM_START_HOUR_EST:=9}"
: "${AUOTAM_END_HOUR_EST:=17}"
: "${AUOTAM_SENDS_PER_SECOND:=1}"
: "${INPUT_CSV:=output/sba/all_businesses.csv}"
: "${LOG_CSV:=data/logs/send_log.csv}"
: "${STATUS_JSONL:=data/events/status.jsonl}"
: "${SEQUENCE_STATUS_JSONL:=data/sequence/status.jsonl}"

echo "Starting AUOTAM scheduler..."
echo "CSV: ${INPUT_CSV}"
echo "Daily target: ${AUOTAM_DAILY_TARGET}"
echo "Window EST: ${AUOTAM_START_HOUR_EST}-${AUOTAM_END_HOUR_EST}"

CMD=(
  python3 run_scheduler.py
  --input-csv "${INPUT_CSV}"
  --log-csv "${LOG_CSV}"
  --sequence-status-jsonl "${SEQUENCE_STATUS_JSONL}"
  --ses-message-status-jsonl "${STATUS_JSONL}"
  --daily-target "${AUOTAM_DAILY_TARGET}"
  --start-hour-est "${AUOTAM_START_HOUR_EST}"
  --end-hour-est "${AUOTAM_END_HOUR_EST}"
  --sends-per-second "${AUOTAM_SENDS_PER_SECOND}"
  --aws-region "${AWS_REGION}"
  --from-email "${AUOTAM_FROM_EMAIL}"
  --from-name "${AUOTAM_FROM_NAME}"
  --reply-to "${AUOTAM_REPLY_TO}"
)

if [[ -n "${SES_CONFIGURATION_SET:-}" ]]; then
  CMD+=(--configuration-set "${SES_CONFIGURATION_SET}")
fi

exec "${CMD[@]}"
