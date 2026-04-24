# Discord 권한 승인 UI — Phase A 설계 V2 (최종)

**작성일**: 2026-04-24
**전임 문서**: `APPROVAL_MCP_PHASE_A_RESEARCH.md` (V1)
**상태**: 승인됨. Pre-A 착수.
**V2 추가 반영 사항**: R1~R5

> V1 의 모든 섹션은 유효. V2 는 사용자 피드백 R1~R5 를 반영하여 Pre-A 실행 절차, 결과 분기, 안전 정책, 폴백 방침, SSE 보안 흔적을 보강한다.

---

## 승인 요약

| 항목 | 결정 |
|---|---|
| MCP permission prompt tool 경로 | 유효 → Phase A 진행 |
| Transport | stdio 폐기, **SSE 직행** |
| 채널 라우팅 | (A) 쿼리 파라미터 + per-spawn mcp-config |
| 캐싱 | 3-tier (세션 캐시 + prefix allowlist + auto-deny) |
| API server 포트 | 기존 `CCDB_API_URL` 포트 공유 |
| 캐시 TTL | **세션 메모리만**. 영구 캐시/DB 없음 |
| 기본값 | 신규 설치 `approval_enabled=True`, 기존 설치는 수동 opt-in |
| 마이그레이션 | 기존 사용자 수동 (문서화만) |

---

## R1. Pre-A 재현 시나리오 체크리스트

### 전제 조건
- 파일 로깅 활성화 (`CCDB_LOG_FILE=/tmp/ccdb-bot.log`)
- 봇 재기동 후 각 시나리오 실행
- 각 시나리오는 **별개 채널** 또는 `/clear` 로 분리하여 실행

### 시나리오 정의

| ID | 모드 | 요청 텍스트 | 예상 도구 | 목적 |
|---|---|---|---|---|
| **S1** | `acceptEdits` | "pwd 만 출력해줘" | Bash (pwd) | 단순 읽기 Bash — 기본적으로 권한 요구되는지 확인 |
| **S2** | `acceptEdits` | "curl -s https://example.com 해서 body 200자만 보여줘" | Bash (curl), WebFetch 가능 | 외부 네트워크 Bash — 승인 요구 예상되는 대표 케이스 |
| **S3** | `acceptEdits` | "현재 채널 작업 디렉토리에 test_hello.py 파일 만들고 hello world print 코드 넣어줘" | Write | Write 는 `acceptEdits` 가 자동 승인하는지 확인 |
| **S4** | `default` | "pwd 만 출력해줘" | Bash (pwd) | mode 변경만으로 승인 요구 강도가 달라지는지 비교 |

### 관찰 대상 (각 시나리오별 기록)

| 관찰 항목 | 관찰 방법 |
|---|---|
| **E1. stream-json 에 `permission_request` 이벤트 존재 여부** | `/tmp/ccdb-bot.log` 에서 `[PRE-A] permission_request_seen` 또는 raw event dump 검색 |
| **E2. ccdb `_handle_permission_request` 호출 여부** | 로그에서 `Permission request posted:` 문자열 검색 |
| **E3. Discord PermissionView 렌더링 여부** | Discord 채널에서 Allow/Deny 버튼이 포함된 embed 가 실제로 떴는지 육안 확인 |
| **E4. Claude 최종 응답 성질** | Discord 채널에 나온 최종 assistant 메시지가 ① 도구 실행 결과(pwd/curl 출력 등) ② "approve 해주세요" 텍스트 ③ 그 외 중 무엇인지 |

### 결과 기록 템플릿

```
| 시나리오 | E1 (permission_request) | E2 (_handle_permission_request) | E3 (Discord PermissionView) | E4 (최종 응답 성질) |
|---|---|---|---|---|
| S1 |                         |                                 |                             |                     |
| S2 |                         |                                 |                             |                     |
| S3 |                         |                                 |                             |                     |
| S4 |                         |                                 |                             |                     |
```

(O/X 또는 간단한 메모로 채움)

---

## R2. Pre-A 결과 분기

| 결과 | 조건 | 조치 |
|---|---|---|
| **결과 A** | S2 또는 S4 에서 E1=X (이벤트 자체 부재) | MCP 설계 유효. **Phase A-1 착수 승인 요청** |
| **결과 B** | E1=O 이지만 E3=X (이벤트는 오는데 UI 가 안 뜸) | **MCP 설계 취소**. PermissionView 디버그가 우선. 즉시 중단 후 보고 |
| **결과 C** | 도구별 혼합 (예: Bash 는 E1=X, Edit 는 E1=O) | **도구별 혼합 경로**. 이벤트 없는 도구만 MCP 로 커버. Phase A 범위 재설계 지시 대기 |

### 공통 규칙
- Pre-A 결과는 어떤 분기든 **Claude 에게 테이블 형태로 제출**
- Claude 는 결과 표를 받은 즉시 분기 판정 결과 보고
- 사용자의 명시적 승인 전까지 Phase A-1 구현 코드 작성 금지

---

## R3. Bash 커맨드 파싱 안전화

### 원칙
`shlex.split` 만으로는 shell metachar 를 통한 **복합 커맨드 우회**를 막을 수 없다. prefix 매칭은 **"단일 심플 커맨드"** 에만 적용.

### Metachar 차단 목록
다음 문자 중 **하나라도 포함**되면 prefix 화이트리스트 적용 안 함 → 무조건 Discord prompt:

| 구분 | 문자 | 설명 |
|---|---|---|
| 커맨드 체이닝 | `&&`, `\|\|`, `;` | 순차/조건 실행 |
| 파이프 | `\|` | 출력 파이프 |
| 리다이렉션 | `>`, `<`, `>>` | 파일 입출력 |
| 명령 치환 | `` ` ``, `$(`, `$((` | 하위 명령 실행 |
| 백그라운드 | `&` | 비동기 실행 |

### 검증 순서
```
Bash 커맨드 도착
    ↓
auto_deny_patterns 검사 (rm -rf /, sudo 등)  ─── 매칭 → deny (즉시)
    ↓ 미매칭
metachar 검사                                    ─── 포함 → prompt (화이트리스트 생략)
    ↓ 단일 심플 커맨드
prefix_allowlist 검사                            ─── 매칭 → allow (즉시)
    ↓ 미매칭
session_cache 검사                               ─── 매칭 → allow (즉시)
    ↓ 미매칭
Discord 버튼 prompt
```

### 테스트 케이스 (tests/unit/test_prefix_allowlist.py)

| 입력 | metachar? | 화이트리스트 적용 | 최종 결과 |
|---|---|---|---|
| `ls /tmp` | 없음 | 적용 (prefix: ls) | allow |
| `pwd` | 없음 | 적용 (prefix: pwd) | allow |
| `ls /tmp && rm -rf /` | `&&` 포함 | 미적용 | auto-deny 먼저 매칭 → deny |
| `ls; cat /etc/passwd` | `;` 포함 | 미적용 | prompt |
| `cat $(echo foo)` | `$(` 포함 | 미적용 | prompt |
| `echo hello \| grep he` | `\|` 포함 | 미적용 | prompt |
| `echo hi > /tmp/x` | `>` 포함 | 미적용 | prompt |
| `sleep 1 &` | `&` 포함 | 미적용 | prompt |
| `` `whoami` `` | `` ` `` 포함 | 미적용 | prompt |
| `git status` | 없음 | 적용 (prefix: git status) | allow |
| `sudo rm file` | 없음 | `sudo` auto-deny 매칭 | deny |

### 구현 스펙 (prefix_allowlist.py 외부 인터페이스)

```python
def evaluate_bash(command: str, policy: ApprovalPolicy) -> Decision:
    """
    Returns:
        Decision.DENY      (auto-deny 패턴 매칭)
        Decision.PROMPT    (metachar 포함 또는 prefix 미매칭)
        Decision.ALLOW     (단일 심플 커맨드 + prefix 매칭)
    """
```

---

## R4. MCP 서버 장애 시 폴백 정책 (V1 §7.5 대체)

### 폐기: 조용한 `acceptEdits` 폴백 금지
V1 §7.5 의 "MCP 기동 실패 → acceptEdits 폴백" 은 **보안 완화 방향**이므로 폐기.

### 신규 정책

| 상황 | 조치 | 사용자 고지 |
|---|---|---|
| **MCP 서버 기동 실패** (API server 부팅 시 SSE 라우트 마운트 실패 등) | 해당 채널에서 **새 세션 생성 거부**. runner 가 CLI 를 spawn 하지 않음 | Discord 메시지: "⚠️ 권한 서버 장애로 세션을 시작할 수 없습니다. 관리자에게 문의하세요." |
| **MCP 연결은 됐는데 도구 호출 시 timeout** (25s 내 응답 없음) | 해당 **세션만 중단** (`runner.terminate()`). 다른 세션/채널은 영향 없음 | Discord 메시지: "⚠️ 권한 응답 timeout 으로 세션이 중단되었습니다." |
| **SSE 연결 끊김** (네트워크 오류) | 해당 세션 중단 + 재연결 시도 없음 (중간 state 가 정합 안 맞을 수 있음) | Discord 메시지: "⚠️ 권한 서버와 연결이 끊어졌습니다. 다시 시도하세요." |

### runner.py 구현 요구사항
- `approval_enabled=True` + MCP 기동 실패 시 `RuntimeError` raise. Caller 는 사용자에게 에러 메시지 노출.
- **fallback flag 자동 전환 없음**. 관리자가 `projects.json` 에서 수동으로 `approval_enabled=False` 로 바꿀 때만 비활성.

### approval_broker.py 구현 요구사항
- Pending request 별 asyncio timeout (25s)
- Timeout 발생 시 해당 요청의 future 를 `TimeoutError` 로 완료 → MCP 서버는 deny 응답 `{"behavior": "deny", "message": "User did not respond in time (25s)"}` 반환
- Timeout 누적 (예: 3회) 감지 시 runner 중단 신호

---

## R5. MCP SSE 라우트 보안 (알려진 제약)

### 현재 Phase A 범위
- SSE 라우트는 `127.0.0.1:<api_port>` 바인딩
- **인증 없음**
- 같은 머신의 다른 프로세스가 `channel_id` 만 추측하면 임의의 approval request 를 가로챌 수 있음

### 리스크 평가
- 현재 환경: 단일 사용자 EC2. 위험도 **낮음** (로컬 공격자가 이미 봇 프로세스를 장악 가능한 수준)
- 그러나 다중 사용자 환경 / 컨테이너 공유 / CI 환경에서는 **주의 필요**

### Phase A 방침
- 인증 **미구현**
- `docs/SECURITY.md` 와 본 문서의 "알려진 제약" 섹션에 **반드시 명시**
- Phase A 완료 후 Issue 생성: "SSE 라우트 토큰 인증 추가" (후속 작업)

### Phase B / 후속 작업 설계 초안
1. `runner_cache.build_mcp_config()` 가 per-spawn 랜덤 토큰 생성 (32바이트 URL-safe)
2. 토큰을 mcp-config JSON 의 `headers` 필드에 주입: `"Authorization": "Bearer <token>"`
3. 봇 측 SSE 핸들러가 handshake 시 토큰 검증 + 채널 ID 매칭 확인
4. 세션 종료 시 토큰 invalidate

### 문서 명시 위치
- 본 문서 §8 "알려진 제약 및 후속 과제"
- `docs/SECURITY.md` 의 "Network Security" 섹션에 한 줄 추가 예정

---

## 6. Phase A 구현 파일 목록 (V1 §7.2 + R3/R4/R5 반영)

| 파일 | 상태 | 역할 | V2 변경 |
|---|---|---|---|
| `claude_discord/utils/logger.py` | **수정** | `CCDB_LOG_FILE` env 지원 | Pre-A 선행 |
| `claude_discord/cogs/event_processor.py` | **수정** | Pre-A 로그 라인 추가 (`[PRE-A] permission_request_seen`) | Pre-A 선행 |
| `claude_discord/claude/parser.py` | **수정** | stream-json raw event debug 로그 (PRE-A 만) | Pre-A 선행 |
| `claude_discord/ext/api_server.py` | 수정 | `/mcp/sse` + `/mcp/messages` 라우트 | Phase A-1 |
| `claude_discord/mcp/__init__.py` | 신규 | 모듈 엔트리 | Phase A-1 |
| `claude_discord/mcp/permission_server.py` | 신규 | in-process MCP 서버 | Phase A-1 |
| `claude_discord/mcp/approval_broker.py` | 신규 | broker + 타임아웃 + 누적 감지 | Phase A-2 (R4 반영) |
| `claude_discord/mcp/prefix_allowlist.py` | 신규 | metachar 차단 + prefix 매칭 + auto-deny | Phase A-2 (R3 반영) |
| `claude_discord/mcp/errors.py` | 신규 | `ApprovalServerUnavailable`, `ApprovalTimeout` | Phase A-1 (R4) |
| `claude_discord/discord_ui/approval_view.py` | 신규 | 3버튼 (Allow / Allow+prefix / Deny) | Phase A-2 |
| `claude_code_core/runner.py` | 수정 | `--permission-prompt-tool` + 기동 실패 시 `RuntimeError` (R4) | Phase A-3 |
| `claude_discord/services/runner_cache.py` | 수정 | per-spawn mcp-config 생성/정리 | Phase A-3 |
| `claude_discord/config/projects_config.py` | 수정 | `ApprovalPolicy` 필드 | Phase A-3 |
| `claude_discord/setup.py` | 수정 | broker 인스턴스, MCP 라우트 마운트 | Phase A-3 |
| `tests/unit/test_approval_broker.py` | 신규 | 타임아웃/누적/중단 정책 (R4) | Phase A-4 |
| `tests/unit/test_prefix_allowlist.py` | 신규 | metachar 차단 11개 케이스 (R3) | Phase A-4 |

---

## 7. Pre-A 실행 단계 (자율 진행 범위)

| 단계 | 작업 | 담당 |
|---|---|---|
| **P0** | 이 문서 (V2) 작성 | ✅ Claude |
| **P1** | `logger.py` 파일 로깅 추가 (`CCDB_LOG_FILE`) | Claude |
| **P2** | `event_processor.py` 에 Pre-A 전용 로그 라인 추가 | Claude |
| **P3** | `parser.py` 에 stream-json raw dump 로그 추가 (기본 꺼짐, env `CCDB_DEBUG_STREAM=1` 로 활성) | Claude |
| **P4** | ruff check + pytest 로 regression 없음 확인 | Claude |
| **P5** | 변경 사항 commit (메시지: `chore(pre-a): add file logging and permission event instrumentation`) | Claude |
| **P6** | 사용자에게 봇 재기동 요청 + 4개 시나리오 안내 | Claude → 사용자 |
| **P7** | 사용자가 S1~S4 실행 | 사용자 |
| **P8** | 로그 수집: `/tmp/ccdb-bot.log` 읽어서 이벤트 표 채움 | Claude |
| **P9** | 결과 A/B/C 분기 판정 + 보고 | Claude → 사용자 |
| **P10** | 사용자 승인 후 Phase A-1 착수 (또는 B 이면 중단, C 이면 재설계) | 사용자 결정 |

---

## 8. 알려진 제약 및 후속 과제

### Phase A 범위 내 알려진 제약
1. **SSE 라우트 인증 없음** (R5 상세). localhost 바인딩만으로 신뢰. 다중 사용자 환경 부적합.
2. **캐시는 세션 메모리만**. 봇 재기동 시 초기화 → 재기동 직후 사용자 경험 저하 가능.
3. **MCP 서버 단일 장애점**. 장애 시 새 세션 거부 + 기존 세션 중단 정책 (R4). 자동 복구 없음.
4. **도구 입력 sanitize 는 approve 단계에서만 가능**. MCP 응답 `updatedInput` 으로 경로 치환은 하지만, auto-deny 이후 Claude 가 우회 시도 가능성 있음 (Claude 가 `rm -rf /` → `rm -rf /tmp/foo` 로 바꾸면 allow 될 수 있음). 이는 의도된 동작 (사용자가 `/tmp/foo` 는 허용하므로).

### 후속 과제 (Phase B 이상)
1. **SSE 토큰 인증** (R5 상세)
2. **영구 승인 이력 DB** (audit trail)
3. **사용자별 프로필/권한 분리** (현재는 봇 전역)
4. **Slash command 로 화이트리스트 동적 편집** (`/ch-approval-allow <prefix>`)
5. **MCP 서버 헬스체크 + 자동 재기동**

---

## 9. 착수 의사결정 상태

- [x] V2 설계 승인 완료
- [ ] **Pre-A 실행** ← **현재 단계**
- [ ] Pre-A 결과 보고 및 분기 판정
- [ ] 사용자 최종 승인
- [ ] Phase A-1 착수

**다음 행동**: Claude 가 P1~P5 실행 후 사용자에게 봇 재기동 안내.
