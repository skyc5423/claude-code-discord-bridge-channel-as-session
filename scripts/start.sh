#!/usr/bin/env bash
# ccdb 봇을 시작합니다.
#
# 사용법:
#   ./scripts/start.sh
#
# 환경변수 (모두 optional, 기본값 사용 가능):
#   CCDB_LOG_FILE          파일 로깅 경로 (기본: /tmp/ccdb-bot.log)
#   API_PORT               REST API + MCP SSE 포트 (기본: 8765, approval_enabled=true 필수)
#   CCDB_APPROVAL_TIMEOUT  Discord 승인 대기 timeout 초 단위 (기본: 300)
#   CCDB_DEBUG_STREAM=1    raw stream-json 전부 로그에 덤프 (디버그용, 기본 꺼짐)
set -euo pipefail

cd "$(dirname "$0")/.."

LOG_FILE="${CCDB_LOG_FILE:-/tmp/ccdb-bot.log}"

# 기존 로그 백업 (있으면)
if [[ -f "$LOG_FILE" ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.$(date +%s).bak"
fi

export CCDB_LOG_FILE="$LOG_FILE"
export API_PORT="${API_PORT:-8765}"

echo "[ccdb] CCDB_LOG_FILE=$CCDB_LOG_FILE"
echo "[ccdb] API_PORT=$API_PORT"
[[ -n "${CCDB_APPROVAL_TIMEOUT:-}" ]] && echo "[ccdb] CCDB_APPROVAL_TIMEOUT=$CCDB_APPROVAL_TIMEOUT"
[[ "${CCDB_DEBUG_STREAM:-}" == "1" ]] && echo "[ccdb] CCDB_DEBUG_STREAM=1 (verbose raw stream logging on)"
echo "[ccdb] starting bot..."

exec uv run python -m claude_discord.main
