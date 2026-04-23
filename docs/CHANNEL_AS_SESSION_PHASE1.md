# 페이즈 1 설계 문서 — Channel-as-Session 모드

## 1. 전체 아키텍처

```
                    ┌─────────────────────────┐
                    │  Discord Gateway        │
                    └────────┬────────────────┘
                             │ on_message
                             ▼
                   ┌──────────────────────┐
                   │ ClaudeChatCog        │       ┌──────────────────────┐
                   │ (기존, 미수정 목표)  │       │ ChannelSessionCog    │
                   │                      │       │ (신규)               │
                   │ ✓ excluded_channel_  │       │ on_message:          │
                   │   _ids 파라미터 추가 │       │ ─ channel_id ∈ PJ?   │
                   │ ─ PJ 채널이면 skip   │       │   └ handle / return  │
                   └──────────┬───────────┘       └──────────┬───────────┘
                              │                              │
                              │                              ▼
                              │                   ┌──────────────────────┐
                              │                   │ ChannelSessionService│
                              │                   │ (라우터+오케스트)    │
                              │                   └──────────┬───────────┘
                              │                              │
                              │                 ┌────────────┼─────────────┐
                              ▼                 ▼            ▼             ▼
                        ClaudeRunner      projects.json  ChannelWT    ChannelSession
                         (기본)           (PROJECTS_     Manager       Repository
                                           CONFIG)       (신규)        (신규, 별도 DB)
                              │                              │             │
                              └──────────────┬───────────────┘             │
                                             ▼                             │
                                    run_claude_with_config(RunConfig)      │
                                             │                             │
                                             ▼                             │
                                      EventProcessor                       │
                                       (기존, 미수정)                      │
                                             │                             │
                        config.repo ──────── ▶  ┬── save(...)               │
                                                └── update_context_stats ◀─┘
                                             (덕 타이핑 호환)

슬래시 커맨드 (기존 Cog 분산) ──▶ SessionLookupService (신규 헬퍼)
                                      │
                                      ├─ channel in PJ? → ChannelSessionRepository
                                      └─ else           → SessionRepository (스레드)
```

PJ = projects.json

---

## 2. 신규 파일 목록

| 파일 | 책임 |
|------|------|
| `claude_discord/config/projects_config.py` | `projects.json` 로더. dataclass `ProjectConfig`. fail-fast 검증. |
| `claude_discord/database/channel_session_repo.py` | `ChannelSessionRepository` — 별도 DB 파일(`data/channel_sessions.db`). EventProcessor 덕 타이핑 호환 메서드 + Channel-as-Session 고유 메서드. |
| `claude_discord/database/channel_session_models.py` | 위 DB의 스키마 + `init_db()`. |
| `claude_discord/services/channel_worktree.py` | `ChannelWorktreeManager` — `{repo_root}/.worktrees/ch-{channel_id}` 전용. 기존 `worktree.py`와 완전 분리. |
| `claude_discord/services/channel_session_service.py` | `ChannelSessionService` — Runner 캐시, 세션 실행, 토픽 갱신, 상태 관리. Cog에서 순수 로직을 분리. |
| `claude_discord/services/session_lookup.py` | `SessionLookupService` — 주어진 channel/thread id에 대해 어느 리포에서 session을 조회해야 하는지 라우팅. |
| `claude_discord/cogs/channel_session.py` | `ChannelSessionCog` — on_message 리스너 + `on_guild_channel_delete` + `/channel-reset` 슬래시 커맨드. |
| `docs/channel_as_session.md` | 페이즈 3에서 작성할 사용 가이드 (페이즈 1에서는 자리만). |

### 2-a. Runner 캐시 위치
`ChannelSessionService.__init__`에서 projects.json을 순회하며 프로젝트별 `ClaudeRunner` 인스턴스를 생성, `self._runners: dict[int, ClaudeRunner]`(key=channel_id)에 보관. `bot.runner`는 그대로 `ClaudeChatCog` 전용.

---

## 3. 기존 파일 수정 범위

| 파일 | 수정 위치 | 변경 내용 |
|------|-----------|-----------|
| `claude_discord/cogs/claude_chat.py` | `__init__` 시그니처 (L87~148) | `excluded_channel_ids: set[int] \| None = None` 파라미터 추가. `self._excluded_channel_ids` 보관. |
| `claude_discord/cogs/claude_chat.py` | `on_message` 헤드 (L203~240) | 첫 허용 체크 직후 `if message.channel.id in self._excluded_channel_ids or (thread and parent in excluded): return` 삽입. |
| `claude_discord/setup.py` | `setup_bridge` 시그니처 (L65~86) | `projects_config_path: str \| None = None` 파라미터 추가. env fallback: `PROJECTS_CONFIG`. |
| `claude_discord/setup.py` | `setup_bridge` 본문 (L138 이후) | 1) `ProjectsConfig` 로드 (fail-fast) → 2) `_all_channel_ids`에서 PJ 채널 자동 제거 → 3) `ClaudeChatCog(..., excluded_channel_ids=pj_channels)` → 4) `ChannelSessionCog` 조건부 등록 + `BridgeComponents`에 `channel_session_repo` 추가. |
| `claude_discord/__init__.py` | `__all__` 및 import | `ChannelSessionCog`, `ChannelSessionRepository`, `ProjectsConfig` export (공개 API). |
| `claude_discord/cogs/session_manage.py` | `/compact` 제외한 명령(예: `/sessions`, `/resume-info`, `/context`, `/worktree-list` 등) | **최소 수정 원칙**: 내부에서 `SessionLookupService`를 사용하도록 교체. 상세는 §9 참조. 단 `isinstance(..., discord.Thread)` 체크가 있는 커맨드는 Channel-as-Session 채널 대응을 위해 분기 추가. |
| `claude_discord/cogs/claude_chat.py` | `/compact`, `/stop`, `/clear`, `/rewind`, `/fork` (L290~499) | 이들은 `isinstance(interaction.channel, discord.Thread)`로 막혀 있어 **현 구조상 Channel-as-Session 채널에서는 자동으로 거부됨**. 대응: `ChannelSessionCog`에 `channel` 버전을 **별도 구현** (§9). 기존 코드는 건드리지 않음. |

> **기본 방침**: 기존 파일은 훅 포인트만 수정, 새 기능은 신규 파일로. upstream merge 부담 최소화.

---

## 4. ChannelSessionRepository SQLite 스키마

**파일**: `data/channel_sessions.db` (기존 `sessions.db`와 분리).

```sql
CREATE TABLE IF NOT EXISTS channel_sessions (
    channel_id        INTEGER PRIMARY KEY,
    session_id        TEXT,
    project_name      TEXT NOT NULL,
    repo_root         TEXT NOT NULL,
    worktree_path     TEXT NOT NULL,
    branch_name       TEXT NOT NULL,
    model             TEXT,
    permission_mode   TEXT,
    context_window    INTEGER,
    context_used      INTEGER,
    turn_count        INTEGER NOT NULL DEFAULT 0,
    error_count       INTEGER NOT NULL DEFAULT 0,
    warned_80pct_at   TEXT,
    topic_last_set_at TEXT,
    topic_last_pct    INTEGER,
    summary           TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_used_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_channel_sessions_session_id
    ON channel_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_channel_sessions_last_used
    ON channel_sessions(last_used_at);
```

`BUSY_TIMEOUT`: `PRAGMA busy_timeout = 5000` (5초) — open 직후 실행. DB 락 완화.

마이그레이션: `_MIGRATIONS` 리스트를 별도로 두되 idempotent 원칙(`CREATE TABLE IF NOT EXISTS`, `ALTER ... IF NOT EXISTS`는 SQLite 미지원이므로 `contextlib.suppress(Exception)`) 적용. 기존 `database/models.py` 방식과 동일.

---

## 5. ChannelSessionRepository 메서드

### 5-a. EventProcessor가 호출하는 메서드 (덕 타이핑 호환 필수)

`event_processor.py`에서 `self._config.repo`를 호출하는 라인을 실제로 추적한 결과:

| 라인 | 호출 형태 |
|------|-----------|
| L263 | `await self._config.repo.save(self._config.thread.id, self._state.session_id)` |
| L266-268 | `await self._config.repo.save(self._config.thread.id, self._state.session_id, summary=summary)` |
| L486 | `await self._config.repo.save(self._config.thread.id, event.session_id)` |
| L496-500 | `await self._config.repo.update_context_stats(thread_id=..., context_window=..., context_used=...)` |

**그 외 `_run_helper.py`에서 `config.repo` 직접 호출 없음.** 즉 호환 대상은 `save(...)`와 `update_context_stats(...)` **딱 둘**.

→ 호환 비용 낮음. "훅 추가 대안"은 포기하고 덕 타이핑으로 간다.

```python
# EventProcessor 호환 메서드 (동일 시그니처)
async def save(
    self,
    thread_id: int,           # 실제로는 channel_id가 들어감
    session_id: str,
    *,
    working_dir: str | None = None,   # 무시 (worktree_path는 기동 시점에 세팅)
    model: str | None = None,
    origin: str = "channel",
    summary: str | None = None,
) -> None: ...

async def update_context_stats(
    self,
    thread_id: int,           # channel_id
    context_window: int,
    context_used: int,
) -> None: ...
```

호출부가 `thread_id` 키워드를 쓰므로 파라미터명은 `thread_id`로 **유지**. 내부에서 `channel_id`로 매핑.

### 5-b. Channel-as-Session 고유 메서드

```python
async def init_db(self) -> None: ...                          # PRAGMA busy_timeout 포함
async def get(self, channel_id: int) -> ChannelSessionRecord | None: ...
async def ensure(
    self,
    channel_id: int,
    project_name: str,
    repo_root: str,
    worktree_path: str,
    branch_name: str,
    model: str | None,
    permission_mode: str | None,
) -> ChannelSessionRecord: ...                                # 없으면 생성, 있으면 반환
async def clear_session(self, channel_id: int) -> bool: ...   # session_id/context_*/turn_count 리셋, 메타는 유지
async def delete(self, channel_id: int) -> bool: ...          # 레코드 통째 삭제 (/channel-reset 마지막 단계)
async def increment_turn(self, channel_id: int) -> None: ...
async def increment_error(self, channel_id: int) -> int: ...  # 반환: 누적 error_count
async def reset_error(self, channel_id: int) -> None: ...
async def mark_80pct_warned(self, channel_id: int) -> None: ...
async def clear_80pct_warned(self, channel_id: int) -> None: ...
async def update_topic_snapshot(
    self,
    channel_id: int,
    pct: int,
    at_iso: str,
) -> None: ...
async def should_update_topic(
    self,
    channel_id: int,
    new_pct: int,
    min_interval_seconds: int,
    min_delta_pct: int,
) -> bool: ...                                                 # 읽기 전용 계산 헬퍼
```

`ChannelSessionRecord` dataclass: 스키마 필드와 1:1. `EventProcessor` 호환을 위해 `save()`는 channel 레코드가 **이미 존재한다는 전제**로 `UPDATE`만 수행. 존재 보장은 `ensure()` 가 먼저 호출된다는 invariant로 충족 (메시지 수신 시점에 서비스 레이어가 `ensure` → `runner.run` 순서).

---

## 6. ChannelSessionCog.on_message 라우팅 의사코드

```python
@commands.Cog.listener()
async def on_message(self, message: discord.Message) -> None:
    # 1. 기본 게이트
    if message.author.bot:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if not isinstance(message.channel, discord.TextChannel):
        return  # 스레드/DM/그외 → ClaudeChatCog 몫
    if self._allowed_user_ids and message.author.id not in self._allowed_user_ids:
        return

    # 2. 이 채널이 projects.json 소속인지
    project = self._projects.get(message.channel.id)
    if project is None:
        return  # 내 담당 아님. ClaudeChatCog가 알아서 처리.

    # 3. 내 담당. 여기부터는 ClaudeChatCog가 보지 못하도록
    #    setup.py에서 excluded_channel_ids로 미리 차단되어 있음 (§10)

    prompt, images = await build_prompt_and_images(message, save_dir=...)
    if not prompt and not images:
        return

    # 4. 진행 중 세션이 있으면 SIGINT-교체 (기존 _handle_thread_reply 패턴)
    active_runner = self._service.active_runner_for(message.channel.id)
    if active_runner is not None:
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("🔁")        # 중단-재시작 표시
        await active_runner.interrupt()
        await self._service.await_active_task(message.channel.id)

    # 5. 실제 실행 위임 (서비스가 ensure → worktree → runner.clone → run_claude_with_config)
    await self._service.run(
        channel=message.channel,
        user_message=message,
        project=project,
        prompt=prompt,
        images=images,
    )
```

실제 세션 실행(`self._service.run`)이 `run_claude_with_config`를 호출하고, 그 안의 `EventProcessor`가 `config.repo=ChannelSessionRepository`에 채널별 세션을 업서트한다. `config.registry=None`으로 넘겨 **concurrency notice 주입을 건너뛰어** ccdb-내장 worktree 규약(`../wt-{thread_id}`)이 섞여 들어가지 않게 한다.

---

## 7. ChannelWorktreeManager (신규)

기존 `worktree.py::WorktreeManager`와 **완전 분리**. 위치: `claude_discord/services/channel_worktree.py`. 파일 경로 규약이 다르고(`.worktrees/ch-{cid}` vs `../wt-{tid}`), 브랜치 규약도 다르다(`channel-session/{cid}` vs `session/{tid}`).

### 메서드

| 메서드 | 시그니처 | 내부 git 명령 |
|-------|----------|---------------|
| `plan_paths` | `(repo_root, worktree_base, branch_prefix, channel_id) -> WorktreePaths` | 순수 계산. IO 없음. |
| `exists` | `(paths) -> bool` | `paths.worktree_path.is_dir() and (paths.worktree_path/'.git').exists()` |
| `ensure` | `(paths) -> EnsureResult` | 존재하면 그대로 반환. 없으면 `_create`. 폴백 로직 포함. |
| `is_clean` | `(worktree_path) -> bool` | `git status --porcelain` — 출력이 비어있는지. |
| `remove_if_clean` | `(paths) -> RemovalResult` | `is_clean` 확인 → `git worktree remove <path>` → 실패 시 reason 반환. dirty면 `removed=False, reason="dirty"`. |
| `list_all` | `(repo_root) -> list[WorktreeInfo]` | `git worktree list --porcelain` 파싱. 진단용. |

### `_create` (핵심) — git 명령 매핑

```
입력: repo_root=/code/workspace/Dalpha, worktree_base=.worktrees,
      branch_prefix=channel-session, channel_id=1234567890

계산: worktree_path = /code/workspace/Dalpha/.worktrees/ch-1234567890
      branch_name  = channel-session/1234567890
```

| 단계 | 명령 | 성공 | 실패 분기 |
|------|------|------|-----------|
| 0. 레포 검증 | `git -C {repo_root} rev-parse --is-inside-work-tree` | `true` 반환 | 실패 → `EnsureResult(ok=False, reason="not_a_git_repo")` |
| 1. 디렉터리 선점 체크 | `Path(worktree_path).exists()` | 미존재 | 존재 & `.git`도 있으면 기존 워크트리로 재사용(`ok=True, reason="reused"`). 존재 & `.git` 없음(고아 폴더)이면 `ok=False, reason="path_occupied_not_worktree"`. |
| 2. 브랜치 선점 체크 | `git -C {repo_root} rev-parse --verify refs/heads/{branch}` | 미존재 → 3-a 수행 | 존재 → 3-b 수행 (폴백) |
| 3-a. 신규 브랜치 생성 | `git -C {repo_root} worktree add {worktree_path} -b {branch}` | ok | 실패 & stderr에 `already checked out` 있으면 `ok=False, reason="branch_checked_out_elsewhere"`. 기타는 `reason="git_add_failed: {stderr}"`. |
| 3-b. 기존 브랜치 재사용 | `git -C {repo_root} worktree add {worktree_path} {branch}` (브랜치만, `-b` 없음) | ok | 실패 처리 동일. |
| 4. dry-run 모드 (`/channel-reset --dry-run` 등) | 위 중 어느 것도 실행하지 않고 명령 문자열만 `planned_commands`에 채워서 반환 | — | — |

`EnsureResult`는 `(ok: bool, worktree_path: str, branch: str, created: bool, reason: str, planned_commands: list[str])` 로 감싸 반환. 페이즈 3 검증 가이드에서 각 reason을 표로 나열 가능.

---

## 8. /channel-reset 흐름

```
/channel-reset (current channel only)
    │
    ▼
① 스코프 체크 ─ interaction.channel.id ∈ projects.json ?
    └─ no  → ephemeral("❌ This channel is not a Channel-as-Session channel."); return
    │
    ▼ yes
② 세션/워크트리 현황 조회
    - record = channel_session_repo.get(channel_id)
    - paths  = wt_manager.plan_paths(...)
    - is_dirty = wt_manager.exists(paths) and not wt_manager.is_clean(paths.worktree_path)
    │
    ▼
③ Confirmation 프롬프트 (bot.send with reactions ✅ ❌)
    문구:
      "⚠️ Reset the session for this channel?
       - worktree: `{worktree_path}` ({dirty? 'DIRTY — will be KEPT' : 'clean — will be removed'})
       - session:  {session_id or '(none)'}
       - turns:    {turn_count}
       React ✅ within 60s to confirm, ❌ to cancel."
    │
    ▼
④ 대기: bot.wait_for('reaction_add', timeout=60, check=...)
    - ❌ or timeout → "Cancelled."; return
    - ✅            → proceed
    │
    ▼
⑤ 진행 중 세션 있으면 먼저 SIGINT (service.active_runner_for / await_active_task)
    │
    ▼
⑥ worktree 처리
    - is_dirty → wt_manager는 건드리지 않고 로그: "⚠️ Dirty worktree preserved at {path}. Commit/stash then `git worktree remove`."
    - clean    → wt_manager.remove_if_clean(paths)
    │
    ▼
⑦ DB 처리
    - channel_session_repo.delete(channel_id)   # 레코드 전체 삭제 (다음 메시지에 ensure로 재생성)
    │
    ▼
⑧ 채널 토픽 클리어 (or 기본 문구 복원)
    │
    ▼
⑨ 완료 임베드 — 무엇을 했는지 (제거된 worktree / 유지된 worktree / 삭제된 session_id) 체크리스트로 출력.
```

**불변식**: dirty worktree는 어떤 경로로도 자동 삭제되지 않음. ✅ 리액션조차 우회 수단 아님.

---

## 9. 슬래시 커맨드 분기 전략

### 9-a. 현재 각 커맨드가 session_id를 어떻게 얻는가

| 커맨드 | 정의 위치 | 얻는 방식 | Channel-as-Session 채널에서 현재 동작 |
|--------|-----------|-----------|----------------------------------------|
| `/help` | claude_chat | 동적 커맨드 트리 | 그대로 동작 (무관) |
| `/stop` | claude_chat L290 | `self._active_runners[thread.id]` + `isinstance(..., Thread)` 거부 | Channel에선 거부됨 |
| `/compact` | claude_chat L315 | `self.repo.get(thread_id)` + Thread 전용 | 거부됨 |
| `/clear` | claude_chat L355 | `self._active_runners + self.repo.delete(thread.id)` + Thread 전용 | 거부됨 |
| `/rewind` | claude_chat L380 | `self.repo.get` + Thread 전용 | 거부됨 |
| `/fork` | claude_chat L446 | Thread 전용 (새 thread 생성 개념) | 거부됨 (의미 불명확 → 지원 안 함) |
| `/sessions`, `/resume`, `/sync-*`, `/model-*`, `/effort-*`, `/tools-*` | session_manage | SettingsRepo / SessionRepository 직접 | Channel-as-Session 채널에서도 **그대로 유효** (thread 체크 없는 것들은 전역). 수정 불필요. |
| `/resume-info`, `/context` | session_manage L551, L849 | `isinstance(..., Thread)` + `self.repo.get(thread.id)` | 거부됨 |
| `/worktree-list`, `/worktree-cleanup` | session_manage | 기존 `WorktreeManager` 전용 | **의미 자체가 기존 `wt-{tid}` 워크트리용**. Channel-as-Session 워크트리는 여기 안 잡힘. → 별도 신규 커맨드로 분리. |
| `/skill` | skill_command | 스레드 생성해서 실행 | Channel-as-Session 채널에서는 **채널 안에서 실행**되도록 분기 필요. |

### 9-b. 해결 전략

두 가지 옵션 중 **"신규 Cog에 채널 버전 복제"** 채택.

| 옵션 | 평가 |
|------|------|
| ❌ 기존 커맨드에 if 분기 추가 | upstream merge 부담. `isinstance(..., Thread)` 거부 로직이 스레드 모드 invariant. |
| ✅ `ChannelSessionCog`에 별도 채널 버전 커맨드 등록 | 기존 코드 0줄 수정. Discord는 **동일 이름 커맨드 여러 Cog 등록 불가**이므로 이름 네임스페이스 필요 (예: `/ch-stop`, `/ch-clear`, `/ch-compact`, `/ch-context`, `/ch-resume-info`). |

**커맨드 이름 최종안** (페이즈 2에서 확정 가능):

| 스레드 커맨드 | Channel-as-Session 커맨드 | 동작 |
|---------------|---------------------------|------|
| `/stop` | `/ch-stop` | 이 채널의 active runner에 SIGINT |
| `/compact` | `/ch-compact` | 이 채널 세션에 `/compact` prompt 실행 |
| `/clear` | `/channel-reset` (이미 있음) | dirty check 포함한 풀 리셋 |
| `/rewind` | (페이즈 1에서 미지원) | — |
| `/fork` | (지원 안 함) | 채널 개념상 의미 없음 |
| `/resume-info` | `/ch-resume-info` | `claude --resume {session_id}` 출력 + worktree 경로 |
| `/context` | `/ch-context` | context 바 + warned_80pct 여부 |
| `/worktree-list` | `/ch-worktree-list` | `projects.json` 순회하여 각 채널 워크트리 상태 |

**구현 헬퍼**: `SessionLookupService` (신규, `claude_discord/services/session_lookup.py`).

```python
# 시그니처만 — 의사코드
@dataclass
class LookupResult:
    kind: Literal["channel", "thread", "none"]
    session_id: str | None
    working_dir: str | None
    repo: SessionRepository | ChannelSessionRepository | None

class SessionLookupService:
    def __init__(self, session_repo, channel_session_repo, projects): ...
    async def resolve(self, interaction_channel_id: int) -> LookupResult: ...
```

추후 `/sessions` 같은 전역 뷰가 두 리포를 모두 훑어야 하면 이 헬퍼를 확장. 페이즈 1에서는 **명시적 읽기 전용**으로만 쓴다.

### 9-c. 스킬 커맨드 (`/skill`)

기존 `SkillCommandCog`는 `claude_channel_id`에서 스레드 생성. Channel-as-Session 채널에서 `/skill`이 트리거되면 **스레드 생성 대신 채널 본문에 실행**해야 한다.
- 최소 침투 방식: `SkillCommandCog` 내부의 "`spawn thread`" 분기 앞에서 `interaction.channel.id in projects`이면 `ChannelSessionService.run_skill(channel, skill_name, ...)`에 위임. 기존 동작은 유지.
- 페이즈 2에서 `SkillCommandCog.__init__`에 optional `channel_session_service` 주입 포인트 추가.

---

## 10. 공존 규칙 구현 상세 (하이브리드 A+B)

### 10-a. setup.py 수정 부분 (의사코드)

```python
async def setup_bridge(..., projects_config_path: str | None = None, ...):
    # (기존 채널 ID 계산 블록 바로 뒤)
    if projects_config_path is None:
        projects_config_path = os.getenv("PROJECTS_CONFIG")

    projects: ProjectsConfig | None = None
    if projects_config_path:
        projects = ProjectsConfig.load(projects_config_path)   # fail-fast
        logger.info("Channel-as-Session enabled: %d project(s)", len(projects))

    pj_channel_ids: set[int] = set(projects.channel_ids()) if projects else set()

    # (A) _all_channel_ids에서 PJ 채널 자동 제거
    _all_channel_ids -= pj_channel_ids

    # ChatCog에 (B) excluded 전달
    chat_cog = ClaudeChatCog(
        bot, repo=session_repo, runner=runner, ...,
        channel_ids=_all_channel_ids or None,
        excluded_channel_ids=pj_channel_ids,   # ← 추가 파라미터
    )

    # ChannelSessionCog 등록 — projects 있을 때만
    channel_session_repo: ChannelSessionRepository | None = None
    if projects is not None:
        channel_session_db_path = os.getenv(
            "CHANNEL_SESSION_DB", "data/channel_sessions.db"
        )
        channel_session_repo = ChannelSessionRepository(channel_session_db_path)
        await channel_session_repo.init_db()

        channel_ws_manager = ChannelWorktreeManager()
        channel_service = ChannelSessionService(
            bot=bot,
            runner_template=runner,       # 프로젝트별 runner 생성을 위한 베이스
            projects=projects,
            repo=channel_session_repo,
            wt_manager=channel_ws_manager,
            session_repo=session_repo,
        )
        ch_cog = ChannelSessionCog(
            bot, service=channel_service, projects=projects,
            allowed_user_ids=allowed_user_ids,
        )
        await bot.add_cog(ch_cog)
        logger.info("Registered ChannelSessionCog")

    components = BridgeComponents(
        session_repo=session_repo,
        ...,
        channel_session_repo=channel_session_repo,   # 신규 필드
    )
    return components
```

### 10-b. ClaudeChatCog 최소 수정 (claude_chat.py)

```python
# __init__: 파라미터 1개 추가 + 저장
excluded_channel_ids: set[int] | None = None,
...
self._excluded_channel_ids: set[int] = excluded_channel_ids or set()

# on_message 최상단 (기본 게이트 바로 다음)
if isinstance(message.channel, discord.TextChannel):
    if message.channel.id in self._excluded_channel_ids:
        return
elif isinstance(message.channel, discord.Thread):
    if (message.channel.parent_id or 0) in self._excluded_channel_ids:
        return
```

`monitor_all_channels=True` 모드에서도 위 체크가 있으면 **무조건 PJ 채널을 비킨다**. 두 Cog 책임 영역이 완전히 분리.

---

## 11. 에러 처리 매트릭스

| 에러 유형 | 탐지 위치 | 대응 |
|-----------|-----------|------|
| `projects.json` 파싱 실패 (JSON 문법) | `ProjectsConfig.load` | raise `ConfigError("projects.json: JSON parse error at line N: ...")`. `setup_bridge`에서 캐치하지 않고 **봇 기동 거부** (fail-fast). |
| `projects.json` 필드 누락/타입 오류 | `ProjectsConfig.load` | raise `ConfigError("projects.json[channel_id=..., field='repo_root']: missing or not str")`. 어느 키가 문제인지 명확히. 봇 기동 거부. |
| 동일 `repo_root` 중복 | `ProjectsConfig.load` | 경고 로그만(의도적일 수 있음). 차단 X. |
| `repo_root`가 git 레포 아님 | `ChannelWorktreeManager._create` 1단계 | `EnsureResult(ok=False, reason="not_a_git_repo")`. 채널에 임베드: "❌ repo_root `/...`는 git 레포가 아닙니다. projects.json 수정 필요." 세션 실행 건너뜀. |
| worktree 경로 이미 존재(다른 것) | `_create` 2단계 | `reason="path_occupied_not_worktree"` → 채널 안내 + 수동 개입 요청. |
| 브랜치가 다른 곳에서 체크아웃됨 | `_create` 3-a 실패 stderr | `reason="branch_checked_out_elsewhere"` → 안내 + `git worktree list` 결과 표시. |
| 권한 없음 / 디스크 풀 | `_create` 임의 단계 | stderr 전체를 임베드에 표시(truncate). |
| Claude subprocess 크래시 (비정상 종료) | `run_claude_with_config` finally + `StreamEvent.error` | `ChannelSessionRepository.increment_error(channel_id)` → 반환 count가 **3 이상**이면 채널에 경고: "최근 3회 연속 세션이 실패했습니다. `/channel-reset` 을 권장합니다." + `reset_error`는 **성공 세션 완료 시**에 호출. |
| Claude subprocess 타임아웃 | runner가 timeout_embed 이벤트를 이미 emit | 기존 경로 그대로. `error_count` 증가만 추가. |
| DB 락 (`aiosqlite.OperationalError: database is locked`) | `ChannelSessionRepository`의 각 `async with connect` | `PRAGMA busy_timeout=5000` + 단발 예외 발생 시 `asyncio.sleep(0.2)` → 1회 재시도. 실패하면 로그 + 호출부 None 반환. |
| 채널 삭제 이벤트 (`on_guild_channel_delete`) | `ChannelSessionCog` | 활성 세션 있으면 `interrupt()`. worktree가 **clean이면** `remove_if_clean`. **dirty면 로그만** (절대 삭제 X). DB 레코드도 `delete`. |
| Discord 토픽 rate limit (`429`) | `channel.edit(topic=...)` 호출 | `contextlib.suppress(discord.HTTPException)` + `update_topic_snapshot` 스킵 (갱신 실패를 DB에 기록 안 함 → 다음 시도 때 재시도됨). |
| 80% 경고 중복 방지 | `EventProcessor` 후처리 | context_used/context_window 계산 → 0.80 이상이고 `warned_80pct_at IS NULL`이면 경고 메시지 + `mark_80pct_warned`. `clear_80pct_warned`는 `/channel-reset`과 `/ch-compact` 성공 후에만 호출. |

---

## 12. 구현 순서 (페이즈 2)

의존성 그래프:

```
  ProjectsConfig ──────────────────────────┐
       │                                   │
       ▼                                   │
  ChannelSessionModels ─┐                  │
                        ▼                  │
                ChannelSessionRepository ──┼──┐
                        │                  │  │
                        ▼                  │  │
                ChannelWorktreeManager ────┤  │
                        │                  │  │
                        ▼                  │  │
                ChannelSessionService ◀────┘  │
                        │                     │
                        ▼                     │
                ChannelSessionCog ◀──────── SessionLookupService
                        │
                        ▼
                setup.py 수정  +  ClaudeChatCog(excluded_*)
                        │
                        ▼
                __init__.py export  +  /ch-* 슬래시 커맨드
```

### 파일별 작업 순서

| 단계 | 파일 | 커밋 제안 |
|------|------|-----------|
| 1 | `config/projects_config.py` | `chore: add ProjectsConfig loader` |
| 2 | `database/channel_session_models.py` + `database/channel_session_repo.py` | `chore: add ChannelSession DB layer` |
| 3 | `services/channel_worktree.py` | `feat: add ChannelWorktreeManager` |
| 4 | `services/session_lookup.py` | `chore: add SessionLookupService` |
| 5 | `services/channel_session_service.py` | `feat: add ChannelSessionService (runner cache + execute flow)` |
| 6 | `cogs/channel_session.py` (on_message + on_guild_channel_delete만, 슬래시 커맨드 제외) | `feat: add ChannelSessionCog with project routing` |
| 7 | `cogs/claude_chat.py` 수정(`excluded_channel_ids`) + `setup.py` 수정 + `__init__.py` export | `feat: integrate ChannelSession into setup_bridge` |
| 8 | `cogs/channel_session.py`에 `/channel-reset` 추가 | `feat: add /channel-reset command` |
| 9 | `cogs/channel_session.py`에 `/ch-stop /ch-compact /ch-context /ch-resume-info /ch-worktree-list` 추가 + `SkillCommandCog` 최소 훅 | `feat: add channel-scoped slash commands` |
| 10 | `docs/channel_as_session.md` 작성 | `docs: add channel-as-session usage guide` |

각 단계 끝에 `python -c "import claude_discord"` 수준의 smoke test. 단계 2~5는 순수 로직이므로 필요 시 pytest unit 가능(요구상 테스트 스킵이라 생략).
