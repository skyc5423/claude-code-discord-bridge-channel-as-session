# Ralph 자동 실행 플레이북 — Channel-as-Session 페이즈 2

> **핵심 원칙**: **무조건 끝까지 진행**. 어떤 문제가 발생해도 스스로 디버깅하고 해결하며 완주한다.
> 사용자에게 질문하지 않는다. 중단하지 않는다. 포기하지 않는다.
>
> **대상**: 페이즈 2 구현 전체 (배치 D → E → F → H) + 가능한 범위의 자동 검증(G-auto).

---

## 0. 절대 원칙

### 0-1. 자율 실행 (Non-negotiable)

- **어떤 상황에서도 작업을 중단하지 않는다.**
- **사용자에게 질문하지 않는다.**
- 모든 문제는 스스로 진단하고 해결한다.
- 해결책이 당장 안 보이면 **더 조사하고 더 시도한다**.
- 여러 접근을 시도하고, 실패하면 다음 접근. 계속.
- 의심되면 grep. 막히면 git log. 안 보이면 read.

### 0-2. 설계 문서 모호성은 ralph 가 합리적 결정

설계 문서에 없거나 모호한 부분:
- 페이즈 1의 기존 패턴을 참고
- 페이즈 2 설계의 정신(자동화, dirty 보존)에 맞게 판단
- 판단을 내리고 **계속 진행**
- 결정 내역은 `/tmp/ralph-decisions.md` 에 로그만 남김 (중단 사유 아님)

### 0-3. 파괴적 작업에 대한 안전장치 (중단이 아닌 자동 복구)

다음은 데이터 손실 위험이 있으므로 **자동 감지 + 자동 복구** 로직을 구현하되 **진행은 계속**:

1. **Dirty worktree 자동 삭제 위험**: `remove_if_clean` 이 dirty 에서 호출되려 하면 no-op + 로그. `remove_force` 는 테스트에서만 호출.
2. **페이즈 1 DB 레코드 손상**: 마이그레이션 전 자동 백업 (`.pre-phase2.bak.db`). 손상 감지 시 백업 복원 후 재시도.
3. **기존 스레드 모드 regression**: smoke test 로 커버. 실패 시 코드 원복 후 재설계 후 재시도.

모든 안전장치는 **자동**. 사용자 개입 없음.

### 0-4. 문제 해결 전략 (막히면 단계적으로 적용)

**레벨 1 — 기본 디버깅**:
- 에러 메시지 정확히 읽기
- Traceback 따라가기
- 관련 파일 view
- grep 으로 관련 패턴 찾기

**레벨 2 — 광역 조사**:
- `git log -p <file>` 로 변경 이력
- `git blame` 으로 특정 줄의 맥락
- 테스트 파일에서 기대 동작 확인
- 페이즈 1 구현을 reference 로 사용

**레벨 3 — 접근 변경**:
- 현재 방법이 안 되면 다른 방법 시도
- 설계 문서를 느슨하게 해석 (의도 우선, 문구 2순위)
- 필요 시 코드 구조 일부 재설계 (설계 의도 범위 내에서)

**레벨 4 — 최후의 수단**:
- 해당 기능을 최소 동작 가능 상태(MVP)로 단순화
- 복잡한 edge case 는 TODO 주석 + 로그로 남기고 넘어감
- 실행 가능한 상태를 우선, 완벽함은 나중

**레벨 5 — 그래도 안 되면**:
- 해당 파일/기능을 임시로 stub (NotImplementedError 대신 경고 로그 + no-op)
- commit 메시지에 `[PARTIAL]` 태그 + 해결 못 한 부분 명시
- **계속 다음 단계로 진행**

**절대 하지 않는 것**: 멈춤, 질문, 보류, 대기.

---

## 1. 작업 전 로드

첫 루프에서 다음 파일 전부 읽기:

1. `docs/CHANNEL_AS_SESSION_PHASE2.md` — master spec
2. `docs/CHANNEL_AS_SESSION_PHASE1_V3.md` — 페이즈 1 설계 (참조)
3. `docs/channel_as_session.md` — 페이즈 1 사용자 가이드 (배치 H에서 확장)
4. `claude_discord/__init__.py`
5. `claude_discord/setup.py`
6. 이번에 수정될 주요 파일들:
   - `claude_discord/config/projects_config.py`
   - `claude_discord/services/channel_worktree.py`
   - `claude_discord/services/channel_session_service.py`
   - `claude_discord/cogs/channel_session.py`
   - `claude_discord/cogs/claude_chat.py`
   - `claude_discord/cogs/skill_command.py`
   - `claude_discord/database/channel_session_models.py`
   - `claude_discord/database/channel_session_repo.py`

---

## 2. 추가 결정 사항 (설계 문서에 없음, ralph 가 내재화)

### R1. 마이그레이션에서 Discord API 의존 제거

설계 §3-2 step 3 을 하드코딩 룩업으로 대체. `migration/phase2.py` 상단:

```python
_PHASE1_CHANNEL_TO_CATEGORY: dict[int, int] = {
    1496803518508699798: 1496787663263498332,  # Dalpha-main → Dalpha
    1496803536762175489: 1496787750202900581,  # oi-agent-main → oi-agent-fnco-chatbot
    1496803553484734504: 1496803244478042193,  # fnco-databricks-main → fnco-databricks
    1496803565858066533: 1496803399575015454,  # dalpha-dynamic-edge-main → dalpha-dynamic-edge
    1496803585906708562: 1496803457317994607,  # oi-oliveyoung-main → oi-oliveyoung-crawling
}
```

`run_if_needed()` 시그니처에서 `bot` 파라미터 제거. 완전 오프라인. 맵에 없는 channel_id 는 `category_id=NULL` + warning log.

### R2. unregister_channel 범위

`ProjectsConfig.unregister_channel()` = **메모리 인덱스만 제거**. DB 는 건드리지 않음. DB 처리는 `cleanup_channel(reason=...)` 담당.

### R3. RewindSelectView 하위호환

`RewindSelectView.__init__` 에 `interrupt_callable: Callable[[], Awaitable[None]] | None = None` 추가. Thread 경로는 명시적으로 `interrupt_callable=None` 전달.

### R4. 배치 G 대응

Discord 실제 메시지 전송 검증은 ralph 불가. 대신:
- **봇 부팅 smoke (실제 인스턴스 생성까지, Discord 연결 없이)**
- **마이그레이션 실행 검증 (실제 DB + JSON 변환)**
- **페이즈 1 regression smoke**
- **Mock Discord 이벤트 실증**

이걸 묶어 "G-auto" 로 실행. 사용자 실제 가동 검증은 복귀 후 수동.

---

## 3. 실행 순서

### 3-0. 준비

```bash
git status
git log --oneline -5
tmux ls 2>/dev/null
```

봇이 tmux 에서 돌고 있으면 **건드리지 않는다**. 코드 수정은 reimport 안 되므로 tmux 봇에 영향 없음.

Git dirty 면 무관 변경은 `git stash push -u -m "ralph: pre-phase2 stash"`. 완료 후 안내만 남김.

### 3-1. 배치 D — 순수 로직 + DB

**D.1** `services/channel_naming.py` 신규
- `MAIN_CHANNEL_PATTERN = re.compile(r"^main$")`
- `WORKTREE_CHANNEL_PATTERN = re.compile(r"^wt-([a-z0-9][a-z0-9_-]*)$")`
- `ResolvedChannelName` dataclass
- `resolve_channel_name(name) -> ResolvedChannelName | None`
- `branch_name(branch_prefix, slug) -> str`

**D.2** `config/projects_config.py` 재구조화
- `CategoryProjectConfig` dataclass
- `RegisteredChannel` dataclass (shared_cwd_warning property 포함)
- `ProjectsConfig` 재구현 (`_categories`, `_channel_index`)
- 페이즈 1 호환: `has`, `get`, `channel_ids` 유지 (`get` 반환 타입 변경)
- 신규: `has_category`, `get_category`, `register_channel`, `unregister_channel`, `replace_categories`
- `ProjectsConfigDiff` dataclass

**호출부 전수 수정** (grep `projects.get` / `projects.has`):
- `cogs/claude_chat.py`
- `cogs/channel_session.py`
- `services/channel_session_service.py`
- `services/topic_updater.py`
- `cogs/session_manage.py`
- `RegisteredChannel.shared_cwd_warning` property 로 기존 `project.shared_cwd_warning` 대체

**D.3** `services/channel_worktree.py::plan_paths` 시그니처 변경
- 파라미터: `repo_root, worktree_base, branch_prefix, slug: str`
- `.worktrees/ch-{slug}` 경로
- `remove_force(paths) -> RemovalResult` 신규
- 호출자 (전부 grep) 업데이트

**D.4** DB 마이그레이션 구문 추가
`channel_session_models.py::_MIGRATIONS`:
```python
_MIGRATIONS = [
    "ALTER TABLE channel_sessions ADD COLUMN channel_name TEXT",
    "ALTER TABLE channel_sessions ADD COLUMN category_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_channel_sessions_category_id ON channel_sessions(category_id)",
]
```

**D.5** `channel_session_repo.py::ensure()` 확장
- `channel_name: str | None = None`
- `category_id: int | None = None`
- UPSERT INSERT/UPDATE SET 에 추가

**D.6** Smoke tests (tests/unit/)
- `test_channel_naming.py` — 8 케이스
- `test_projects_config_phase2.py` — 신 스키마
- `test_channel_worktree_slug.py` — slug 기반
- `test_channel_session_repo_phase2.py` — 새 컬럼

**D.7** 품질 게이트 + 커밋
```bash
uv run ruff check claude_discord/ tests/
uv run ruff format claude_discord/ tests/
uv run pytest tests/unit/ -v -k "channel_naming or projects_config_phase2 or channel_worktree_slug or channel_session_repo_phase2"
git add -A
git commit -m "feat: phase-2 schema (category-keyed projects, naming resolver, slug-based worktree)"
```

### 3-2. 배치 E — Watcher + 서비스 통합

**E.1** `services/channel_session_service.py` 개편
- `_prepare_cwd(channel, registered)` 리팩터
- `handle_message` 가 `RegisteredChannel` 사용
- `run_skill_in_channel` 실제 구현 (§10-2)
- `ensure()` 에 `channel_name`, `category_id` 전달

**E.2** `services/projects_watcher.py` 신규
- §7-2 `ProjectsWatcher`
- mtime polling 15초
- 첫 루프 기록만
- ConfigError → warning + 기존 cfg 유지

**E.3** `cogs/channel_session.py` 확장
- `on_guild_channel_create` 리스너
- `on_guild_channel_update` 리스너 (tear down + re-evaluate)
- `on_guild_channel_delete` 확장 (unregister + DM 시도)
- `_startup_scan` (on_ready 에서 호출)
- `/ch-worktree-cleanup --force` 인자

**E.4** Smoke tests
- `test_projects_watcher.py`
- `test_channel_events.py` (mock guild)
- `test_startup_scan.py`
- `test_force_cleanup.py`

**E.5** 품질 게이트 + 커밋
```bash
uv run ruff check ... && uv run ruff format ...
uv run pytest tests/unit/ -v
git add -A
git commit -m "feat: phase-2 Discord event handlers + hot reload + force cleanup"
```

### 3-3. 배치 F — 마이그레이션 + 기존 커맨드 + 조립

**F.1** `migration/__init__.py` + `migration/phase2.py` 신규
- R1 룩업 테이블 포함
- `MigrationResult` dataclass
- `run_if_needed(*, projects_config_path, channel_session_db_path) -> MigrationResult`
- 단계:
  1. `_is_already_migrated` 체크 (`_meta.schema_version == 2`)
  2. 백업: `projects.json.pre-phase2.bak` + DB 파일 복사
  3. 구 JSON → 신 JSON 변환
  4. DB ALTER 적용 (idempotent)
  5. 5개 main 채널 backfill
- 실패 시 복구 안내 로그
- 실패해도 ralph 는 **복구 후 계속** (stub 으로 넘어가는 한이 있어도)

**F.2** `cogs/claude_chat.py::/rewind` Channel 분기
- R3 `RewindSelectView` 시그니처 변경
- `SessionLookupService` 경로
- Thread 호출부에 `interrupt_callable=None` 명시

**F.3** `cogs/skill_command.py` Channel 훅
- `projects`, `channel_session_service` optional 파라미터
- Channel-as-Session 채널이면 `run_skill_in_channel` 위임
- Thread 경로는 기존 유지

**F.4** `setup.py` 통합
- `migration.phase2.run_if_needed()` 초반에 호출
- `ProjectsWatcher.start()`
- `SkillCommandCog` post-inject
- 기존 `ClaudeChatCog` post-inject 유지

**F.5** `__init__.py` export 추가
- `CategoryProjectConfig`, `RegisteredChannel`, `ResolvedChannelName`
- `resolve_channel_name`, `branch_name`
- `ProjectsWatcher`

**F.6** Smoke tests
- `test_migration_phase2.py` — 페이즈 1 JSON + DB 준비 → 마이그레이션 → 검증 (백업 존재, 신 스키마, 5 레코드 backfill, idempotent)
- `test_rewind_channel.py` — SessionLookup 경로
- `test_skill_channel.py` — run_skill_in_channel 호출

**F.7** 품질 게이트 + 커밋
```bash
uv run ruff check ... && uv run ruff format ...
uv run pytest tests/ -v
git add -A
git commit -m "feat: phase-2 migration + /rewind channel + /skill channel + setup wiring"
```

### 3-4. 배치 G-auto — 자동 가능한 가동 검증

**실제 봇 가동 없이** 자동 실행.

**G.1** Bot boot smoke (Discord 연결 없이 인스턴스 생성까지)
- `tests/integration/test_bot_boot.py`
- AsyncMock bot → `setup_bridge` 실행 → 모든 Cog 등록 확인

**G.2** 페이즈 1 regression
- PROJECTS_CONFIG 없는 상태로 부팅 → `ClaudeChatCog` 만 등록

**G.3** 페이즈 2 활성
- 신 스키마 projects.json 로 부팅 → 두 Cog 다 등록 + ProjectsWatcher 시작

**G.4** 마이그레이션 실행
- 페이즈 1 JSON + DB 를 temp 에 복사 → `run_if_needed` 실행 → 결과 검증

**G.5** Mock Discord 이벤트
- `on_guild_channel_create/update/delete` mock → 각 핸들러 동작 확인

**결과**: `/tmp/ralph-g-auto-results.md` 에 기록. 커밋 없음.

### 3-5. 배치 H — 문서

**H.1** `docs/channel_as_session.md` 업데이트
- "페이즈 2" 섹션 신규 추가
- 카테고리 단위 projects.json 스키마
- 채널 이름 규칙 + 8개 예시
- 자동 등록 흐름 (create/update/delete)
- hot reload 동작
- `/ch-worktree-cleanup --force`
- 마이그레이션 로그 포맷
- 예시 서버 구조 (5개 카테고리)
- 페이즈 1 섹션은 "레거시 (자동 마이그레이션됨)" 표시

**H.2** 커밋
```bash
git add -A
git commit -m "docs: channel-as-session phase-2 usage guide"
```

---

## 4. 공통 품질 게이트

각 배치 commit 전 반드시:

```bash
uv run ruff check claude_discord/ tests/
uv run ruff format --check claude_discord/ tests/
uv run pytest tests/ -v --tb=short
uv run python -c "from claude_discord import setup_bridge, ProjectsConfig, ChannelSessionCog; print('OK')"
```

**하나라도 실패하면** (0-4 적용):
1. 에러 로그 분석 → 수정 → 재실행
2. 반복
3. 3회 반복해도 안 되면: 문제 테스트를 `@pytest.mark.xfail(reason="TODO: ...")` + 다음 진행
4. 그래도 안 되면: 문제 코드 단순화 (stub/no-op) + `# TODO-PHASE2` 주석 + 진행

**중단 금지**. 어떤 경우에도 완주.

---

## 5. 진행 중 보고 (stdout)

각 배치 완료 후:

```
=== BATCH <X> COMPLETE ===
Files changed: <count>
Smoke tests: <passed>/<total>
Commit: <sha>
Decisions logged: <count>
Next: BATCH <Y>
```

최종 완료 시:

```
=== PHASE 2 IMPLEMENTATION COMPLETE ===
Batches executed: D, E, F, G-auto, H
Total commits: <N>
Total smoke tests: <N>
Decisions logged: /tmp/ralph-decisions.md
G-auto results: /tmp/ralph-g-auto-results.md

Known partials (if any):
  - <파일>:<라인> — <이유>
  - ...

User next steps:
1. Review commits: git log --oneline -15
2. Review decisions: cat /tmp/ralph-decisions.md
3. Restart bot in tmux to apply
4. Run manual Discord validation (PHASE2 §13 batch G scenarios 1-8)
```

---

## 6. 절대 금지

- ❌ 사용자에게 질문
- ❌ 중단
- ❌ "사용자 결정 필요" 류 표현
- ❌ 애매한 상황에서 멈춤
- ❌ 완벽하지 않으면 진행 안 함
- ❌ "다음 지시 기다림"
- ❌ "확인 필요"

## 7. 절대 필수

- ✅ 끝까지 진행
- ✅ 막히면 다른 방법 시도
- ✅ 판단 내려지면 실행
- ✅ 실패해도 복구하고 계속
- ✅ 각 배치 commit 까지 완료
- ✅ 최종 보고까지 도달

---

## 8. 시작

지금 즉시 배치 D 부터 착수. 설계 문서 완독 후 진행. 이 문서의 원칙을 모든 루프에서 준수.
