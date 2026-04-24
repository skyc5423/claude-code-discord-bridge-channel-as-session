# Discord 권한 승인 UI — Phase A 조사 보고서

**작성일**: 2026-04-24
**목적**: Channel-as-Session 모드에서 Claude Code 의 "approve 해주세요" 요청이 Discord 에 버튼 UI 로 노출되도록 하는 MCP 기반 permission prompt tool 설계의 **구현 착수 전 사전 조사 결과**를 문서화한다.
**결론 미리보기**: MCP 기반 `--permission-prompt-tool` 경로 유효. stdio 폐기, SSE 직행. Phase A 범위 재조정 및 캐싱 레이어 필수 포함.

---

## 1. 문제 재현 및 진단 확정 — **부분 확정** ⚠️

### 1.1 현재 증상 (사용자 보고)
- Claude 가 "approve 해주시면 진행하겠습니다" 라는 **텍스트**로 승인을 요청하고 턴 종료.
- Discord 측에서 Approve/Deny 버튼이 전혀 렌더되지 않아 사용자가 응답할 방법 없음.
- 해당 채널은 이후 영구 정지 상태.

### 1.2 로그 조회 불가
- 봇 프로세스 `pid=2975676` 의 `stdin/stdout/stderr` 가 전부 `/dev/pts/1` (사용자 tmux 터미널) 로 연결됨.
  ```
  lrwx------  0 -> /dev/pts/1
  lrwx------  1 -> /dev/pts/1
  lrwx------  2 -> /dev/pts/1
  ```
- 별도 파일 로깅이 설정되어 있지 않음. 에이전트가 과거 `permission_request` 이벤트 유무를 직접 확인할 수 없음.

### 1.3 간접 증거
- ccdb `event_processor.py:574` 의 `_handle_permission_request()` 는 정상 구현되어 있음 (`PermissionView`).
- CLI 가 stream-json `permission_request` 이벤트를 실제로 내보냈다면 UI 가 표시됐어야 함.
- 사용자 증언 ("텍스트로 말하고 끝난다") 은 CLI 가 **이벤트 없이 텍스트 응답으로 끝낸 케이스**가 존재함을 의미.

### 1.4 결론
- MCP 기반 설계는 **유효**.
- Phase A 선행 작업으로 **파일 로깅 활성화** 추가 (문제 재현 검증용):
  - 환경변수 `CCDB_LOG_FILE=/tmp/ccdb-bot.log` 도입
  - `claude_discord/utils/logger.py` 에 `RotatingFileHandler` 추가
  - 기동 후 문제 상황 재현하여 `Permission request posted` 로그 유무 확인

---

## 2. `--permission-prompt-tool` 공식 스펙 확정 — **확정** ✅

### 2.1 CLI 버전 및 플래그 지원
- **CLI 버전**: `2.1.119`
- **설치 경로**: `/home/sangmin/.local/share/claude/versions/2.1.119` (Bun 단일 바이너리)
- **`--help` 노출 여부**: **미노출 (hidden flag)**
- **실제 지원**: Bun 바이너리 strings 스캔 결과 `permissionPromptTool` / `permissionPromptToolName` 식별자 **96회** 발견. `claude -p "hi" --permission-prompt-tool mcp__foo__bar` 테스트 호출 시 에러 없이 통과.
- **관련 바이너리 증거**:
  ```
  "permissionPromptTool":return `Tool '${$.permissionPromptToolName}' requires approval for this ${H} command`
  "canUseTool callback cannot be used with permissionPromptToolName. Please use one or the other."
  "Permission prompt was aborted."
  permissionPromptTool",permissionPromptToolName:$.name,toolResult:H};if(H.behavior==="allow"
  ```

### 2.2 플래그 형식
```
--permission-prompt-tool mcp__<server-name>__<tool-name>
```
예: `--permission-prompt-tool mcp__ccdb__approval_request`

CLI 는 기동 시 `--mcp-config` 로 로드된 MCP 서버 목록에서 해당 도구가 존재하는지 검증. 없으면 에러 후 종료.

### 2.3 입력 스키마 (CLI → MCP 도구)
항상 3개 필드 고정:

| 필드 | 타입 | 설명 |
|---|---|---|
| `tool_use_id` | `string` | Anthropic 원본 `toolu_...` ID |
| `tool_name` | `string` | 승인 필요한 빌트인 도구명 (`Bash`, `Edit`, `Write`, `Read` 등) |
| `input` | `object` | 해당 도구의 원본 입력 파라미터 |

`input` 예시:
- `Bash`: `{"command": "uv run pytest", "description": "run tests", "timeout": 30000}`
- `Edit`: `{"file_path": "...", "old_string": "...", "new_string": "..."}`
- `Write`: `{"file_path": "...", "content": "..."}`
- `Read`: `{"file_path": "...", "offset": 0, "limit": 100}`

**⚠️ 세션/cwd/session_id 컨텍스트 미전달** — MCP 도구는 어떤 Discord 채널이 호출했는지 알 수 없음. 반드시 out-of-band 라우팅 필요.

### 2.4 출력 스키마 (MCP 도구 → CLI)
MCP content 타입 `text` 안에 JSON 문자열:

**Allow (승인)**
```json
{"behavior": "allow", "updatedInput": { ... }}
```
- `updatedInput` 은 선택 — 생략 시 원본 `input` 그대로 실행
- 제공 시 도구 실행 직전 입력 치환 (경로 sanitize, 타임아웃 조정 등에 사용)

**Deny (거부)**
```json
{"behavior": "deny", "message": "거부 사유"}
```
- `message` 필수 — Claude 가 이 메시지를 받아서 다음 행동 결정

**주의**: `"ask"` 는 CLI 내부 타입 (hook/SDK 간 통신용) 이지 MCP 도구 응답으로는 **유효하지 않음**.

MCP 서버 응답 packaging:
```python
return {
    "content": [
        {"type": "text", "text": json.dumps({"behavior": "allow"})}
    ]
}
```

### 2.5 `--permission-mode` 와의 상호작용
CLI 내부 평가 순서 (first match wins):

| 레이어 | 조건 | MCP 호출? |
|---|---|---|
| 1 | `--allowedTools` 매칭 | ❌ 즉시 allow |
| 2 | `--disallowedTools` 매칭 | ❌ 즉시 deny |
| 3 | mode = `bypassPermissions` 또는 `--dangerously-skip-permissions` | ❌ 즉시 allow |
| 4 | mode = `dontAsk` | ❌ 즉시 deny |
| 5 | mode = `default` / `acceptEdits` / `auto` + 매칭 룰 없음 | ✅ **MCP 호출** |

→ ccdb 기본값을 `permission_mode="default"` 로 변경하되 `acceptEdits` 도 호환 유지.

### 2.6 SDK `canUseTool` 과의 관계
- `canUseTool` 콜백 (Python/TypeScript SDK) 과 `permissionPromptToolName` (CLI 플래그) 은 **same-layer 대안**.
- 동시 지정 시 에러: `"canUseTool callback cannot be used with permissionPromptToolName. Please use one or the other."`
- ccdb 는 CLI subprocess 경로이므로 `permissionPromptToolName` 만 사용.

### 2.7 Anthropic 공식 권고
Anthropic 은 신규 구현에 **PreToolUse 훅** 사용을 권장 (`BaseHookInput` 으로 `session_id`, `cwd`, `git_branch` 등 풍부한 컨텍스트 제공). 그러나 훅은 빠른 응답 (< 60s) 이 전제이고, ccdb 의 Discord 버튼 응답은 인간이 개입하므로 **permission prompt tool 이 더 적합**.

---

## 3. stdio vs SSE — **SSE 확정** ✅

### 3.1 stdio 경로의 구조적 문제
- stdio MCP 서버는 CLI 가 **자식 프로세스로 spawn** → 봇 프로세스와 분리.
- 봇 내부 `ApprovalBroker` (asyncio), Discord gateway 객체 접근 불가.
- 환경변수로 `CCDB_CHANNEL_ID` 전달해도 Discord 로 메시지 보내려면 결국 **봇으로의 HTTP 콜백** 필요 → IPC 계층이 하나 더 생김.
- 같은 봇 프로세스에서 여러 채널이 동시에 CLI 를 띄우면 stdio 서버 프로세스가 N개 생겨 리소스 낭비.

### 3.2 SSE 경로의 이점
- MCP 서버를 **봇 프로세스 내부**에 aiohttp 라우트로 얹음 (`api_server.py` 확장).
- Broker, Discord gateway, DB 전부 **in-process 직접 참조**.
- CLI 는 `--mcp-config` 의 `url` 로 SSE 엔드포인트 접속. 다중 채널도 HTTP 커넥션 하나씩으로 처리.
- `mcp` Python SDK 의 `SseServerTransport` 로 boilerplate 최소화.

### 3.3 CLI 지원 확인
- CLI v2.1.119 바이너리 strings 에 `"sse"`, `"streamable-http"`, `"transport"` 문자열 다수 확인.
- `claude mcp add --transport http` 하위명령 존재 (doc example).
- `--mcp-config` JSON 에서 `{"mcpServers": {"ccdb": {"transport": "sse", "url": "http://..."}}}` 형태 지원.

### 3.4 결론
Phase A 부터 **SSE 직행**. stdio 백업 경로는 두지 않음. 일정 재산정 필요 (아래 섹션 7).

---

## 4. 채널 라우팅 — **(A) 쿼리 파라미터 + per-spawn mcp-config** ✅

### 4.1 대안 비교

| 대안 | 구현 복잡도 | 신뢰성 | 유지보수 | 비고 |
|---|---|---|---|---|
| **(A) `?channel_id=X` + 임시 mcp-config 파일** | 중 | 높음 | 파일 라이프사이클 관리 | **선택** |
| (B) 도구 인자 + 시스템 프롬프트 강제 | 낮음 | 낮음 | 프롬프트 파손 즉시 장애 | Claude 가 잊을 수 있음 |
| (C) 채널마다 다른 포트 | 높음 | 높음 | 포트 풀 관리, 방화벽 | 과한 복잡도 |

### 4.2 (A) 선택 근거
- **라이프사이클 자연**: mcp-config 파일은 CLI 서브프로세스 수명과 함께 생성/소멸. 이미 ccdb 는 `/tmp/ccdb-uploads` 등 tmp 디렉토리 사용 중.
- **신뢰성**: CLI 는 반드시 config 에 지정된 URL 로 접속 → 채널 매핑 불확실성 제로.
- **봇 측 단순**: SSE handshake 시 `request.query.get("channel_id")` 한 줄로 세션 태깅.

### 4.3 구현 스케치
```python
# runner_cache.py
def build_mcp_config(channel_id: int, api_port: int) -> Path:
    path = Path(f"/tmp/ccdb-mcp/{channel_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mcpServers": {
            "ccdb": {
                "transport": "sse",
                "url": f"http://127.0.0.1:{api_port}/mcp/sse?channel_id={channel_id}"
            }
        }
    }))
    return path
```

```python
# api_server.py (MCP SSE route)
async def mcp_sse_handler(request):
    channel_id = int(request.query.get("channel_id", "0"))
    # SSE session 에 channel_id 주입 → permission_server 에서 접근
    return await sse_transport.handle_sse(request, context={"channel_id": channel_id})
```

---

## 5. `permission_mode=default` UX 시뮬레이션 및 캐싱 — **3-tier 혼합 캐싱** ✅

### 5.1 시나리오별 예상 승인 횟수

| 작업 | Bash | Edit/Write | Read | 총 승인 |
|---|---|---|---|---|
| "*.py 에서 TODO 주석 grep" | 0~2 | 0 | 0 | **0~2** |
| "pytest → 실패 원인 분석 → 수정" | 3~8 | 1~5 | 3~10 | **7~23** |
| "의존성 추가 + 빌드" | 2~4 | 0~1 | 1~2 | **3~7** |
| "7개 파일 리팩터" | 1~3 | 7~15 | 7~15 | **15~33** |

**결론**: 복잡 작업에서 20+ 회 승인 팝업이 현실. **캐싱 필수**.

### 5.2 정책별 안전성/실용성 매트릭스

| 정책 | 안전성 | UX | 구현 난이도 |
|---|---|---|---|
| 도구명 단위 캐싱 (Bash 한 번 허용 → 세션 내 모든 Bash) | 낮음 (`rm -rf /` 허용됨) | 최상 | 낮음 |
| 도구+input 해시 단위 (동일 호출만 캐싱) | 높음 | 중 (idempotent 호출만 혜택) | 낮음 |
| **도구+커맨드 prefix** (첫 토큰/경로 prefix 일치) | 중-높음 | 상 | 중 |
| 정적 화이트리스트 (`ls`, `cat`, `grep` 등) | 매우 높음 | 중 | 낮음 |

### 5.3 권장 정책: **3-tier 혼합**

1. **세션 로컬 캐시** — `set[tuple[tool_name, sha256(input_json)]]`
   - 동일 입력 재호출은 자동 allow (idempotent)
   - 세션 종료 시 소멸 (메모리만)

2. **Prefix-allow list** — per-project 화이트리스트
   - **기본 안전 prefix** (read-only or idempotent):
     ```
     ls, pwd, cat, head, tail, grep, find, wc
     git status, git diff, git log, git branch
     uv run pytest, uv run ruff
     python --version, node --version
     ```
   - Discord 버튼에 `"✅ 허용 + 이번 세션 내 동일 prefix 모두 허용"` 추가 → 사용자가 런타임에 확장.

3. **Auto-deny 고정 패턴** — 화이트리스트 포함되어도 항상 prompt (또는 자동 거부):
   ```
   rm -rf /
   rm -rf ~
   sudo
   chmod 777
   curl | sh
   curl | bash
   wget | sh
   ```

### 5.4 평가 순서
```
입력 → auto_deny_patterns 검사 → prefix_allowlist 검사 → session_cache 검사 → Discord 버튼
       ↓ 매칭                      ↓ 매칭                    ↓ 매칭                ↓ 사용자 응답
       deny (즉시)                  allow (즉시)              allow (즉시)           allow/deny 기록 후 응답
```

---

## 6. 추가 확인 사항 — **확정** ✅

| 항목 | 결과 | 근거 |
|---|---|---|
| `--permission-prompt-tool` + `--dangerously-skip-permissions` 동시 | `dangerously-skip` 우선 → MCP 미호출 | permission-mode 평가 순서 레이어 3 |
| `canUseTool` + `permissionPromptToolName` 동시 | **에러 exit** | 바이너리 strings: `"Please use one or the other."` |
| MCP tool 기본 timeout | 60s | Anthropic MCP 클라이언트 기본값 |
| Timeout 초과 시 CLI 동작 | `"Permission prompt was aborted"` → 도구 호출 실패 처리 | 바이너리 strings 확인 |
| Discord 버튼 응답 권장 timeout | 25s | MCP 60s 안에 여유있게 응답 완료 필요 |
| 기본 timeout 시 fallback | **deny** + `message="timeout"` | 안전 기본값 |
| 기존 `PermissionView` 재활용 가능? | 부분 재활용 | 입력 구조는 동일, 출력 경로가 다름 → 새 `ApprovalView` 필요 |

---

## 7. Phase A 수정 설계

### 7.1 범위 변경 요약
| 변경 | 이유 |
|---|---|
| ❌ stdio 초기 구현 폐기 | 구조적 IPC 문제 (섹션 3) |
| ➕ SSE 직행 | 단일 프로세스 in-process broker |
| ➕ 파일 로깅 선행 작업 | 재현 진단 검증 (섹션 1) |
| ➕ 캐싱 레이어 MVP 포함 | 20+ 팝업 현실화 (섹션 5) |
| ➕ 새 `ApprovalView` 추가 | 기존 `PermissionView` 는 legacy 경로 유지 |

### 7.2 파일 변경 목록

| 파일 | 상태 | 역할 |
|---|---|---|
| `claude_discord/utils/logger.py` | **수정** | `CCDB_LOG_FILE` env 로 `RotatingFileHandler` 추가 |
| `claude_discord/ext/api_server.py` | **수정** | `/mcp/sse` + `/mcp/messages` 라우트 추가. `mcp` SDK `SseServerTransport` 사용 |
| `claude_discord/mcp/__init__.py` | **신규** | 모듈 엔트리 |
| `claude_discord/mcp/permission_server.py` | **신규** | in-process MCP 서버. 도구 `approval_request(tool_use_id, tool_name, input)` 노출 |
| `claude_discord/mcp/approval_broker.py` | **신규** | per-channel 캐시 + pending future map. `submit(channel_id, tool_name, tool_input) → Future[AllowOrDeny]` |
| `claude_discord/mcp/prefix_allowlist.py` | **신규** | Bash 커맨드 파서 (`shlex.split`) + 안전 prefix 테이블 + auto-deny 패턴 |
| `claude_discord/discord_ui/approval_view.py` | **신규** | `Allow` / `Allow + prefix 허용` / `Deny` 3버튼 + 명령 미리보기 embed |
| `claude_code_core/runner.py` | **수정** | `_build_args`: `approval_enabled=True` 시 `--permission-prompt-tool mcp__ccdb__approval_request` + `--mcp-config` 자동 추가. `permission_mode` 기본 `"default"` |
| `claude_discord/services/runner_cache.py` | **수정** | 채널별 `/tmp/ccdb-mcp/<channel_id>.json` 생성/정리 |
| `claude_discord/config/projects_config.py` | **수정** | `ApprovalPolicy` 필드 추가 (`enabled`, `safe_prefixes`, `auto_deny_patterns`) |
| `claude_discord/setup.py` | **수정** | API server 에 MCP 라우트 마운트, broker 인스턴스 생성 |
| `tests/unit/test_approval_broker.py` | **신규** | 캐시, prefix 매칭, timeout, concurrent submit |
| `tests/unit/test_prefix_allowlist.py` | **신규** | Bash 파싱, 화이트리스트/auto-deny 매칭 |

### 7.3 의존성 추가
- `mcp` Python SDK v1.x
  ```bash
  uv add mcp
  ```

### 7.4 일정 재산정

| Phase | 작업 | 기간 |
|---|---|---|
| **Pre-A** | 파일 로깅 활성화 + 문제 재현 Discord 검증 | 0.5 일 |
| **A-1** | `mcp` 의존성 추가, `api_server.py` SSE 라우트, `permission_server.py` | 0.5 일 |
| **A-2** | `approval_broker.py`, `prefix_allowlist.py`, `approval_view.py` | 0.5 일 |
| **A-3** | `runner.py` / `runner_cache.py` / `projects_config.py` 연결 | 0.25 일 |
| **A-4** | 단위 테스트, Discord 수동 검증, 문서 업데이트 | 0.25 일 |
| **총** | | **2 일** |

### 7.5 리스크 및 완화

| 리스크 | 완화 |
|---|---|
| MCP 서버 장애 → 모든 도구 호출 실패 | runner 에서 MCP 기동 실패 감지 시 `acceptEdits` 폴백 (degradation path). 봇 프로세스 상태 주기 헬스체크 |
| 세션 캐시 과도 관대 → 보안 약화 | `auto_deny_patterns` 는 캐시 전에 평가. 화이트리스트는 읽기전용/idempotent 만 |
| Discord 30s 무응답 → MCP 60s timeout 위험 | broker 내부 타임아웃 25s. 초과 시 자동 deny + "timeout" message |
| 다중 채널 동시 MCP 세션 누적 → 메모리 누수 | 채널 마지막 메시지 후 1h idle 시 SSE 세션 자동 정리 |
| `--permission-prompt-tool` 이 향후 CLI 에서 제거될 가능성 | hidden flag 이지만 `canUseTool` 과 동등 취급됨. SDK 에서 `canUseTool` 이 safe fallback. 필요 시 Python SDK `query()` 경로로 마이그레이션 가능 |

### 7.6 Open Questions (구현 착수 전 확정 필요)

1. **API server 공유 vs 전용 포트**
   - 권장: 기존 `CCDB_API_URL` 포트 공유 (1 포트 원칙)
   - 이유: 인프라 단순, 이미 enable 된 ccdb 인스턴스에는 zero-config

2. **캐시 TTL**
   - 권장: 세션 메모리만 (봇 재시작 시 초기화)
   - 이유: 보안 우선, DB 화 시 schema/마이그레이션 부담

3. **기본값 적용 범위**
   - 권장: 새 설치는 `approval_enabled=True` 기본, 기존 설치는 `acceptEdits` 유지
   - 이유: 호환성. projects.json 스키마에 `approval_enabled: bool` 플래그 추가

4. **EbiBot 기존 설정 마이그레이션 여부**
   - 권장: 수동 옵트인 (문서화만)
   - 이유: 기존 사용자 surprise 방지

---

## 8. 착수 의사결정 요청

**현재 상태**: 조사 완료. 코드 수정 미착수.
**진행 시 해야 할 일**:
1. 위 Open Questions (8.6) 확정
2. Phase Pre-A (파일 로깅) 먼저 실행하여 문제 재현 확증
3. Phase A-1 ~ A-4 순차 구현

**사용자 결정 대기 중**:
- [ ] 이 설계로 착수 승인?
- [ ] Open Questions 답변?
- [ ] 우선순위 (이 작업 vs 다른 대기 작업)?

---

## 부록 A. 조사 명령 레퍼런스

```bash
# CLI 버전
claude --version  # → 2.1.119 (Claude Code)

# 플래그 지원 검증 (hidden flag)
strings /home/sangmin/.local/share/claude/versions/2.1.119 | grep permissionPromptTool
# → 96회 매칭

# 실 호출 테스트
claude -p "hi" --permission-prompt-tool mcp__foo__bar
# → 정상 실행 (플래그 수락)

# 봇 프로세스 stderr 경로
ls -la /proc/2975676/fd/2
# → /dev/pts/1 (파일 로깅 없음)
```

## 부록 B. 참고 리소스

- Anthropic Claude Code docs: `https://docs.claude.com/en/docs/claude-code/`
- MCP Python SDK: `https://github.com/modelcontextprotocol/python-sdk`
- Community MCP permission server (JS): `github.com/UnknownJoe796/claude-code-mcp-permission`
- CLI 바이너리 내 핵심 식별자:
  - `permissionPromptTool`, `permissionPromptToolName`
  - `canUseTool`
  - `"behavior": "allow" | "deny"`
  - `updatedInput`, `message`
  - `"Permission prompt was aborted."`
