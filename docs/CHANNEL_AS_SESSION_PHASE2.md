# Channel-as-Session 페이즈 2 설계 문서

> **목표**: Discord 에서 채널을 만들고 이름을 바꾸고 지우는 것만으로 Channel-as-Session 상태가 자동으로 동기화되게 한다. `projects.json` 편집은 **카테고리 = 레포 = 프로젝트** 추가 시에만.
>
> **범위**: on_guild_channel_create/update/delete, projects.json hot reload, 카테고리 단위 스키마, 이름 패턴 기반 자동 등록, 마이그레이션, `/rewind` Channel 지원, `/skill` Channel 지원, `/ch-worktree-cleanup --force`.
>
> **영구 제외**: `/project-register` 커맨드, 런타임 cwd 전환, 채널 아카이브.

---

## §0 배경 (페이즈 1 한계 요약)

페이즈 1 에서는 채널 한 개마다 `projects.json` 의 한 엔트리를 수동으로 추가하고 봇을 재시작해야 했다. 5개 프로젝트 main 만 있는 현재도 번거롭고, 작업 채널(`wt-*`)을 자주 만드는 실사용에서는 마찰이 더 커진다.

페이즈 2 는 이 마찰을 제거한다. 새 채널 = 자동 인식. 이름 변경 = 자동 재평가. 삭제 = 자동 정리(단, dirty 보존 불변식 유지).

---

## §1 아키텍처 (페이즈 1 → 페이즈 2 Diff)

### 1-1 전체 흐름

```
                              ┌─────────────────────────────┐
                              │  projects.json              │
                              │  (category_id keyed)        │
                              └──────────┬──────────────────┘
                                         │ mtime polling (15s)
                                         ▼
                              ┌──────────────────────────┐
                              │ ProjectsWatcher (신규)   │
                              │ async background task    │
                              └──────────┬───────────────┘
                                         │ on_change(new_cfg)
                                         ▼
                              ┌─────────────────────────────┐
                              │ ProjectsConfig (확장)       │
                              │  - _categories: cid→cfg     │
                              │  - _channel_index: ch→reg   │
                              │  - register/unregister_chan │
                              └──────────┬──────────────────┘
                                         │
 Discord Gateway events                   │
 ─────────────────────                    │
 on_guild_channel_create  ────────────────┤
 on_guild_channel_update  ────────────────┤  (ChannelNameResolver 로 slug 판정)
 on_guild_channel_delete  ────────────────┤
                                         │
                                         ▼
                             ┌──────────────────────────┐
                             │ ChannelSessionCog (확장) │
                             │  + on_guild_channel_     │
                             │    create / update       │
                             │  + /ch-worktree-cleanup  │
                             │    --force               │
                             └──────────┬───────────────┘
                                         │
                                         ▼
                             ┌──────────────────────────┐
                             │ ChannelSessionService    │
                             │ (handle_message now uses │
                             │  RegisteredChannel)      │
                             │ + run_skill_in_channel() │
                             └──────────┬───────────────┘
                                         │
                                         ▼
                             ┌──────────────────────────┐
                             │ ChannelWorktreeManager   │
                             │ plan_paths(..., slug=..) │ ← channel_id 대신 slug
                             └──────────────────────────┘

                             ChannelNameResolver   (순수, 신규)
                             BranchNamer           (순수, 신규)
                             migration/phase2      (1회 실행, 신규)
```

### 1-2 신규 컴포넌트

| 모듈 | 역할 |
|------|------|
| `services/channel_naming.py` | `resolve_channel_name(name)` 순수 함수, 정규식 상수 (코드↔문서 공유) |
| `services/projects_watcher.py` | `ProjectsWatcher` — mtime polling, on_change 콜백 디스패치 |
| `migration/phase2.py` | 1회성 마이그레이션: projects.json 변환 + DB 컬럼 + 레코드 backfill |

### 1-3 확장되는 컴포넌트

| 모듈 | 변경 요약 |
|------|-----------|
| `config/projects_config.py` | 스키마 재구조화. `ProjectConfig` → `CategoryProjectConfig` (카테고리 단위), `RegisteredChannel` 추가, `_channel_index` in-memory 인덱스 |
| `database/channel_session_{models,repo}.py` | `channel_name TEXT`, `category_id INTEGER` 컬럼 추가 + `ensure()` 시그니처 확장 |
| `services/channel_worktree.py` | `plan_paths(..., slug=...)` — channel_id 대신 slug 기반 path/branch |
| `services/channel_session_service.py` | `handle_message` 가 `RegisteredChannel` 사용, `register_channel_on_create`, `unregister_channel` 훅, `run_skill_in_channel` 실제 구현 |
| `cogs/channel_session.py` | 3개 이벤트 리스너 확장, `/ch-worktree-cleanup --force` |
| `cogs/claude_chat.py` | `/rewind` Channel 지원 (SessionLookupService 경유) |
| `cogs/skill_command.py` | Channel-as-Session 채널 분기 훅 |
| `setup.py` | ProjectsWatcher 기동 + startup migration + startup scan |

### 1-4 페이즈 1 대비 인터페이스 호환성

| API | 페이즈 1 | 페이즈 2 | 호환 |
|-----|----------|----------|------|
| `projects.has(channel_id)` | channel_id 가 키였음 | `_channel_index` 에서 조회 | ✅ 시그니처 동일 |
| `projects.get(channel_id)` | `ProjectConfig` 반환 | `RegisteredChannel` 반환 | ⚠️ 반환 타입 다름 |
| `projects.channel_ids()` | 카테고리 + 채널 혼동 없음 | 현재 등록된 채널 ID 들 | ✅ 의도 동일 |
| `SessionLookupService.resolve(cid)` | - | 불변 | ✅ |
| `ChannelSessionRepository.save/update_context_stats` | - | 불변 | ✅ (EventProcessor 덕 타이핑) |
| `ChannelWorktreeManager.plan_paths(..., channel_id)` | channel_id | `slug` | ⚠️ 시그니처 변경 |

"반환 타입 다름" 은 호출부 전수 업데이트 필요. grep 결과 `projects.get(cid)` 호출 지점:
- `claude_chat.py` — `_is_session_channel`, `/clear`, `/rewind`, `/fork` 안내 분기 (5 곳)
- `channel_session.py` — `on_message` 진입점 (1 곳)
- `channel_session_service.py` — `handle_message` (1 곳)
- `topic_updater` / `session_manage` — `RegisteredChannel.project.shared_cwd_warning` 로 간접 접근 필요

총 수정 지점 약 8 곳. 파이프 통과 가능 범위.

---

## §2 projects.json 신 스키마

### 2-1 키와 필드

```jsonc
{
  "<category_id_string>": {
    "name":               "사람용 표시 이름 (검증 안 됨, 카테고리 rename 가능)",
    "repo_root":          "/absolute/path/to/git/repo",
    "shared_cwd_warning": true,          // 기본 false
    "worktree_base":      ".worktrees",  // 기본 ".worktrees"
    "branch_prefix":      "channel-session",  // 기본 "channel-session"
    "model":              "sonnet",      // 선택
    "permission_mode":    "acceptEdits"  // 선택
  },
  ...
}
```

- **키**: Discord 카테고리 ID (영구 고정). 카테고리 이름은 변경돼도 ID 는 불변 — 따라서 ID 를 키로 씀.
- **`name`**: 표시용, 검증/매칭 불사용.
- **`repo_root`**: 이 카테고리 전체가 공유하는 git 레포.
- **`shared_cwd_warning`**: `main` 채널 (cwd_mode=repo_root) 에만 주입되는 경고.
- **`worktree_base` / `branch_prefix`**: `wt-*` 채널 (dedicated_worktree) 에만 적용.
- **cwd_mode 는 저장하지 않음**: 채널 이름 패턴으로 매 매시지마다 동적 결정 (ChannelNameResolver).

### 2-2 구 스키마 ↔ 신 스키마 대응

| 구 (페이즈 1, channel_id 키) | 신 (페이즈 2, category_id 키) |
|-------------------------------|-------------------------------|
| `"1496803518508699798": { name: "Dalpha-main", repo_root: "/code/workspace/Dalpha", cwd_mode: "repo_root", shared_cwd_warning: true, ... }` | `"1496787663263498332": { name: "Dalpha", repo_root: "/code/workspace/Dalpha", shared_cwd_warning: true, ... }` |
| 각 main 채널당 1 엔트리 | 카테고리당 1 엔트리 (채널은 자동 검출) |
| `cwd_mode` 필드 필수 | `cwd_mode` 필드 없음 (이름 패턴으로 결정) |
| `shared_cwd_warning` 은 채널 단위 설정 | `shared_cwd_warning` 은 카테고리 단위 — 해당 카테고리의 `main` 채널에만 적용 |

### 2-3 데이터 클래스

```python
@dataclass(frozen=True)
class CategoryProjectConfig:
    """One entry in projects.json — one category = one repo = one project."""
    category_id: int
    name: str
    repo_root: str
    shared_cwd_warning: bool = False
    worktree_base: str = ".worktrees"
    branch_prefix: str = "channel-session"
    model: str | None = None
    permission_mode: str | None = None

@dataclass(frozen=True)
class RegisteredChannel:
    """Resolved channel — what the old ProjectConfig used to carry."""
    channel_id: int
    channel_name: str           # "main" or "wt-<slug>"
    category_id: int
    cwd_mode: CwdMode           # "repo_root" or "dedicated_worktree"
    slug: str | None            # None for main, non-empty for worktree
    worktree_path: str | None   # computed by plan_paths; None for repo_root
    branch_name: str | None     # None for repo_root
    project: CategoryProjectConfig

    @property
    def shared_cwd_warning(self) -> bool:
        return self.cwd_mode == "repo_root" and self.project.shared_cwd_warning
```

`ProjectsConfig` 는 두 인덱스 유지:

```python
class ProjectsConfig:
    _categories: dict[int, CategoryProjectConfig]
    _channel_index: dict[int, RegisteredChannel]  # 빈 상태로 시작, startup scan + Discord events 로 채워짐

    def has_category(self, cid: int) -> bool: ...
    def get_category(self, cid: int) -> CategoryProjectConfig | None: ...
    def has(self, channel_id: int) -> bool: ...              # 페이즈 1 호환
    def get(self, channel_id: int) -> RegisteredChannel | None: ...  # 페이즈 1 호환, 반환 타입 변경
    def channel_ids(self) -> set[int]: ...                   # 페이즈 1 호환 — 현재 등록된 채널
    def register_channel(self, channel_id: int, channel_name: str, category_id: int) -> RegisteredChannel | None: ...
    def unregister_channel(self, channel_id: int) -> RegisteredChannel | None: ...
    def replace_categories(self, new_categories: dict[int, CategoryProjectConfig]) -> ProjectsConfigDiff: ...  # hot reload 용
```

---

## §3 마이그레이션 (3범위)

### 3-1 트리거

봇 기동 시 `setup_bridge()` 초반에 `migration.phase2.run_if_needed(projects_config_path, db_path)` 호출. 이미 마이그레이션된 파일 (`schema_version` 플래그) 은 no-op.

### 3-2 범위 (a): projects.json 변환

**대상**: 키가 `channel_id` 인 엔트리가 하나라도 있으면 구 스키마로 간주.

**알고리즘**:
1. 백업 생성: `cp projects.json projects.json.pre-phase2.bak`
2. 구 엔트리 전부 읽음 → `channel_id → repo_root` 맵 구성
3. **각 channel_id 에 대해 Discord API 로 category_id 조회**
   - 봇 객체 필요 → migration 은 `setup_bridge` 의 `on_ready` 이후 시점에 실행
   - API 실패 시 "unknown_category" 임시 카테고리로 임시 등록 + 경고 로그
4. 같은 `category_id` 에 여러 channel_id 가 대응하면 **repo_root 가 같은지 검증**. 다르면 fail-fast (사용자가 직접 해결).
5. 카테고리 엔트리 빌드:
   - `name` = `<discord_category_name>` (API 에서 조회)
   - `repo_root` = 공통값
   - `shared_cwd_warning` = **채널들 중 하나라도 true 면 true** (보수적)
   - `worktree_base` / `branch_prefix` / `model` / `permission_mode` = 다수결 또는 첫 번째 값
6. 새 JSON 을 메모리에서 직렬화, `projects.json.new` 에 쓴 뒤 `mv projects.json.new projects.json` (원자적 교체).
7. 첫 줄 주석으로 `// phase2 migrated from X old entries` 기록 (JSON 이라 실제로는 `"_meta": {"schema_version": 2}` 필드 추가).

**복구**: 실패 시 `projects.json.pre-phase2.bak` 이 그대로 남음 → 수동 rename 으로 원복. 로그에 절차 출력.

### 3-3 범위 (b): DB 스키마

`channel_session_models.py::_MIGRATIONS` 에 추가:

```sql
ALTER TABLE channel_sessions ADD COLUMN channel_name TEXT;
ALTER TABLE channel_sessions ADD COLUMN category_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_channel_sessions_category_id ON channel_sessions(category_id);
```

`contextlib.suppress(aiosqlite.OperationalError)` 덕분에 idempotent.

### 3-4 범위 (c): DB 레코드 backfill

기존 5개 레코드는 페이즈 1 에서 `cwd_mode=repo_root` 로 등록된 main 채널. backfill 전략:

- `channel_name = "main"` (확정 — 페이즈 1 에서 dedicated_worktree 채널이 등록된 적 없음, DB 로 검증 가능)
- `category_id` = Discord API 로 채널 조회 후 `channel.category_id`
  - API 실패 시 NULL 로 두고 다음 메시지 때 on-message 경로에서 채워짐

### 3-5 복구 매트릭스

| 실패 지점 | 영향 | 복구 |
|-----------|------|------|
| (a) JSON 파싱 실패 | projects.json 이 이미 백업됨 → 봇 기동 거부, 로그에 "run `mv projects.json.pre-phase2.bak projects.json` to revert" |
| (a) Discord API 호출 실패 (채널 조회) | 해당 항목은 "unknown_category" 로 임시 등록, 경고 로그. 봇은 계속 기동. 다음 재시작 시 재시도. |
| (b) ALTER TABLE 실패 (이미 존재) | `OperationalError` suppress — idempotent |
| (b) 다른 OperationalError (disk full 등) | raise → 봇 기동 거부 |
| (c) backfill 실패 | 해당 레코드 미터치. 다음 메시지 때 ensure() 에서 채워짐 |

### 3-6 마이그레이션 모듈 시그니처

```python
# claude_discord/migration/phase2.py

@dataclass(frozen=True)
class MigrationResult:
    projects_json_migrated: bool
    db_columns_added: bool
    records_backfilled: int
    warnings: list[str]

async def run_if_needed(
    *,
    projects_config_path: str,
    channel_session_db_path: str,
    bot: commands.Bot,  # For Discord API (fetch_channel)
) -> MigrationResult: ...
```

---

## §4 채널 이벤트 핸들러

### 4-1 `on_guild_channel_create`

```python
@commands.Cog.listener()
async def on_guild_channel_create(self, channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if not self._projects.has_category(channel.category_id or 0):
        return
    resolved = resolve_channel_name(channel.name)
    if resolved is None:
        return  # 규칙 위반 — 조용히 무시 (A2)

    registered = self._projects.register_channel(
        channel_id=channel.id,
        channel_name=channel.name,
        category_id=channel.category_id,
    )
    if registered is not None:
        logger.info(
            "Registered new channel %d (%s) in category %d as %s",
            channel.id, channel.name, channel.category_id, registered.cwd_mode,
        )
    # DB 에는 아직 쓰지 않음 — 첫 메시지 때 ensure() 에서 생성
```

### 4-2 `on_guild_channel_update` (이름/카테고리 변경)

```python
@commands.Cog.listener()
async def on_guild_channel_update(self, before, after):
    if not isinstance(after, discord.TextChannel):
        return
    name_changed = before.name != after.name
    category_changed = (before.category_id or 0) != (after.category_id or 0)
    if not (name_changed or category_changed):
        return

    # Step 1: tear down the old state (dirty-preserving)
    was_registered = self._projects.has(after.id)
    if was_registered:
        logger.info(
            "Channel %d changed (name %r→%r, cat %s→%s) — tearing down",
            after.id, before.name, after.name, before.category_id, after.category_id,
        )
        await self._service.cleanup_channel(after.id, reason="name_changed")
        self._projects.unregister_channel(after.id)

    # Step 2: re-evaluate with new name/category
    if not self._projects.has_category(after.category_id or 0):
        return
    resolved = resolve_channel_name(after.name)
    if resolved is None:
        return

    self._projects.register_channel(
        channel_id=after.id,
        channel_name=after.name,
        category_id=after.category_id,
    )
```

- **dirty 보존**: `cleanup_channel` 이 `remove_if_clean` 을 사용하므로 자동 보존.
- **브랜치 rename 안 함** (A4): 새 이름이 `wt-*` 이면 새 slug → 새 브랜치. 구 브랜치는 남음 (clean 이면 다음 `/ch-worktree-cleanup` 으로 제거).

### 4-3 `on_guild_channel_delete`

페이즈 1 구현 재사용. 단 `projects.unregister_channel()` 호출 추가:

```python
@commands.Cog.listener()
async def on_guild_channel_delete(self, channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if not self._projects.has(channel.id):
        return
    result = await self._service.cleanup_channel(channel.id, reason="channel_delete")
    self._projects.unregister_channel(channel.id)
    if result.worktree_reason == "dirty":
        # 사용자 DM 시도 (best effort, 실패 무시)
        with contextlib.suppress(Exception):
            owner = await self.bot.fetch_user(self.bot.owner_id)
            if owner:
                await owner.send(
                    f"⚠️ 삭제된 채널의 worktree 가 dirty 상태로 보존됐습니다: "
                    f"`{channel.name}` — {result.worktree_reason}. "
                    f"`/ch-worktree-cleanup --force` 로 강제 제거 가능."
                )
```

### 4-4 Startup scan (누락 이벤트 복구)

봇이 꺼져 있는 동안 채널이 생성/변경/삭제되면 이벤트가 유실된다. `on_ready` 시점에 전체 길드의 채널을 스캔해 `projects.register_channel` 로 인덱스를 재구성한다.

```python
async def _startup_scan(self):
    for guild in self.bot.guilds:
        for ch in guild.text_channels:
            if not self._projects.has_category(ch.category_id or 0):
                continue
            if resolve_channel_name(ch.name) is None:
                continue
            self._projects.register_channel(ch.id, ch.name, ch.category_id)
    # 이후: DB 에는 있는데 Discord 에는 없는 "orphan" 레코드 목록 로그 출력
    # (자동 삭제 안 함 — 운영자가 /ch-worktree-cleanup 으로 판단)
```

---

## §5 ChannelNameResolver

`claude_discord/services/channel_naming.py` — **순수 모듈, IO 없음**.

```python
import re
from dataclasses import dataclass
from typing import Literal

from ..config.projects_config import CwdMode

# PUBLIC: docs/channel_as_session.md 와 공유되는 상수
MAIN_CHANNEL_PATTERN = re.compile(r"^main$")
WORKTREE_CHANNEL_PATTERN = re.compile(r"^wt-([a-z0-9][a-z0-9_-]*)$")

@dataclass(frozen=True)
class ResolvedChannelName:
    cwd_mode: CwdMode
    slug: str | None  # None iff cwd_mode == "repo_root"

def resolve_channel_name(name: str) -> ResolvedChannelName | None:
    """Classify a Discord channel name.

    - ``"main"`` → ResolvedChannelName(cwd_mode="repo_root", slug=None)
    - ``"wt-<slug>"`` → ResolvedChannelName(cwd_mode="dedicated_worktree", slug=<slug>)
    - any other → None (channel is ignored by ccdb)

    The slug regex is intentionally strict so that invalid git refname
    characters never leak into branch names. See §6 for examples.
    """
    if MAIN_CHANNEL_PATTERN.match(name):
        return ResolvedChannelName(cwd_mode="repo_root", slug=None)
    m = WORKTREE_CHANNEL_PATTERN.match(name)
    if m:
        return ResolvedChannelName(cwd_mode="dedicated_worktree", slug=m.group(1))
    return None
```

**동일 정규식 문자열이 `docs/channel_as_session.md` 의 "이름 패턴 규칙" 섹션에도 사용됨**. 코드가 진실의 원천이고, 문서는 import 해서 재사용.

---

## §6 BranchNamer + path

`channel_naming.py` 같은 파일에 추가:

```python
def branch_name(branch_prefix: str, slug: str) -> str:
    """Combine the project's branch_prefix with the resolved slug.

    Example::
        branch_name("channel-session", "feat-auth")  # -> "channel-session/feat-auth"

    Slug sanitation already happens in WORKTREE_CHANNEL_PATTERN; callers must
    NOT pass raw Discord names here.
    """
    return f"{branch_prefix}/{slug}"
```

### 6-1 worktree path (slug 기반)

`channel_worktree.py::plan_paths` 시그니처 변경:

```python
@staticmethod
def plan_paths(
    repo_root: str | Path,
    worktree_base: str,
    branch_prefix: str,
    slug: str,                # ← channel_id 대신 slug
) -> WorktreePaths:
    repo = Path(repo_root).resolve()
    base = Path(worktree_base)
    if not base.is_absolute():
        base = repo / base
    wt_path = (base / f"ch-{slug}").resolve()
    branch = branch_name(branch_prefix, slug)
    return WorktreePaths(
        repo_root=str(repo),
        worktree_path=str(wt_path),
        branch_name=branch,
        channel_id=0,  # deprecated; kept for struct compatibility (see §11)
    )
```

페이즈 1 `ch-{channel_id}` → 페이즈 2 `ch-{slug}`. 페이즈 1 에서 실제 생성된 `.worktrees/ch-*` 디렉터리 **없음** (DB 로 검증됨), 따라서 경로 마이그레이션 **불필요**.

### 6-2 예시

| 채널 이름 | 매칭 | slug | worktree_path | branch |
|-----------|------|------|---------------|--------|
| `main` | ✅ | — | — (없음) | — |
| `wt-feat-auth` | ✅ | `feat-auth` | `{repo}/.worktrees/ch-feat-auth` | `channel-session/feat-auth` |
| `wt-docs_v2` | ✅ | `docs_v2` | `{repo}/.worktrees/ch-docs_v2` | `channel-session/docs_v2` |
| `wt-Bug123` | ❌ (대문자) | | | |
| `wt-` | ❌ (slug 부재) | | | |
| `wt--double` | ❌ (`-` 시작) | | | |
| `notes` | ❌ | | | |

---

## §7 Hot Reload

### 7-1 전략: mtime polling (15초 간격)

새 의존성 추가 없음 (watchdog/watchfiles 불필요). `asyncio.sleep(15)` 루프에서 `os.stat().st_mtime` 비교.

### 7-2 구현

`claude_discord/services/projects_watcher.py`:

```python
class ProjectsWatcher:
    """Polls projects.json mtime and dispatches changes to subscribers."""

    def __init__(
        self,
        path: str,
        on_change: Callable[[ProjectsConfig], Awaitable[None]],
        *,
        interval_seconds: float = 15.0,
    ) -> None:
        self._path = path
        self._on_change = on_change
        self._interval = interval_seconds
        self._last_mtime: float | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="projects-watcher")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        # 첫 루프에서는 mtime 만 기록하고 dispatch 하지 않음 (봇 기동 시 정상 로드 경로 유지)
        self._last_mtime = await self._current_mtime()
        while True:
            await asyncio.sleep(self._interval)
            mtime = await self._current_mtime()
            if mtime is None or mtime == self._last_mtime:
                continue
            try:
                new_cfg = await asyncio.to_thread(ProjectsConfig.load, self._path)
            except ConfigError as exc:
                logger.warning(
                    "projects.json change detected but load failed — keeping previous config: %s",
                    exc,
                )
                continue
            self._last_mtime = mtime
            try:
                await self._on_change(new_cfg)
            except Exception:
                logger.exception("projects.json on_change handler failed")
```

### 7-3 `on_change` 콜백 (setup.py 에서 주입)

```python
async def on_projects_change(new_cfg: ProjectsConfig) -> None:
    diff = live_projects.replace_categories(new_cfg._categories)
    logger.info(
        "projects.json hot-reload: added=%d removed=%d changed=%d",
        len(diff.added), len(diff.removed), len(diff.changed),
    )
    # RunnerCache 는 category 가 아니라 channel 단위 — 재계산 필요
    # 현재 등록된 모든 채널의 RegisteredChannel 를 순회해서
    # project 가 바뀌었으면 runner 재생성
    channel_runner_cache.reload_from_projects(live_projects)
    # Startup scan 재실행 — 삭제된 카테고리의 채널들 unregister
    await _rescan_from_discord(bot, live_projects)
```

### 7-4 Active session 영향

- `handle_message` 가 이미 `runner.clone()` 된 상태라 cfg 교체가 현 턴에 영향 없음.
- 다음 메시지의 `ensure()` 시점에 RegisteredChannel 재조회 — 그때 신 설정 반영.
- `RunnerCache.reload_from_projects()` 가 호출된 직후 진행 중이던 clone 은 그대로 살아있음 (cache 는 템플릿만 보유, 개별 subprocess 는 clone 으로 독립).

### 7-5 `ProjectsConfigDiff`

```python
@dataclass(frozen=True)
class ProjectsConfigDiff:
    added: set[int]     # category_ids
    removed: set[int]
    changed: set[int]   # config hash 바뀐 category_ids
```

---

## §8 `/ch-worktree-cleanup --force`

기존 커맨드에 인자 추가:

```python
@app_commands.command(name="ch-worktree-cleanup", ...)
@app_commands.describe(
    dry_run="Preview without actually removing anything",
    force="DANGER: remove even dirty worktrees (loses uncommitted changes)",
)
async def ch_worktree_cleanup(self, interaction, dry_run=False, force=False) -> None:
    if force:
        # confirmation ✅/❌ — 페이즈 1 /channel-reset 과 같은 패턴
        await interaction.response.send_message(
            "⚠️ `--force` 지정됨. dirty worktree 까지 전부 제거합니다. "
            "이는 되돌릴 수 없으며 커밋되지 않은 변경사항이 손실됩니다.\n"
            "✅ 60초 이내 확인 / ❌ 취소"
        )
        # (기존 /channel-reset 의 confirm 로직 재사용)
        ...
    ...
```

force 플로우:
1. 사용자 ✅ 대기 (60초 타임아웃).
2. `remove_if_clean` 대신 새 `remove_force` 호출 (dirty 무시하고 `git worktree remove --force`).
3. 결과 임베드에 "제거된 N개 (dirty 포함 M개)" 표시 + 각 dirty 경로 로그.

`ChannelWorktreeManager` 에 신규 메서드:

```python
def remove_force(self, paths: WorktreePaths) -> RemovalResult:
    """Remove a worktree unconditionally (including dirty state).

    Only callable from /ch-worktree-cleanup --force with user confirmation.
    Equivalent to `git worktree remove --force {path}`.
    """
```

**불변식 유지**: `remove_if_clean` 은 여전히 dirty 보존. `remove_force` 는 명시적 요청 전용 탈출구.

---

## §9 `/rewind` Channel 지원

### 9-1 현 Thread 로직 (claude_chat.py L513~)

```python
thread_id = interaction.channel.id
record = await self.repo.get(thread_id)
jsonl_path = find_session_jsonl(record.session_id, record.working_dir)
turns = parse_user_turns(jsonl_path) if jsonl_path else []
...
view = RewindSelectView(turns, jsonl_path, self._active_runners, thread_id)
```

### 9-2 Channel 지원 방법

`SessionLookupService` 로 session_id + working_dir 획득 후 동일 로직:

```python
# 교체 블록 (Channel 분기)
if isinstance(interaction.channel, discord.TextChannel) and self._projects and self._projects.has(interaction.channel.id):
    lookup = await self._session_lookup.resolve(interaction.channel.id)
    if lookup.kind != "channel":
        await interaction.response.send_message("세션이 없습니다.", ephemeral=True)
        return
    session_id = lookup.session_id
    working_dir = lookup.working_dir
    # 진행 중 active runner — channel 용
    active_runners_dict = { interaction.channel.id: self._channel_session_service.active_runner_for(interaction.channel.id) } \
        if self._channel_session_service and self._channel_session_service.active_runner_for(interaction.channel.id) else {}
else:
    # 기존 Thread 로직
    ...
```

그 뒤 JSONL 처리는 동일 (cwd_mode 와 무관 — jsonl 파일은 Claude CLI 가 관리).

### 9-3 RewindSelectView 의 runner interruption

기존 `RewindSelectView` 가 Thread의 `_active_runners` dict 를 받아 선택된 턴 커밋 전에 active runner 를 kill 함. Channel 에서는 다른 dict 구조 — `ChannelSessionService._active` 를 직접 건드리지 않고 `service.active_runner_for(cid) → interrupt` + `service.await_active_task(cid)` 조합으로 교체.

**수정 최소화 전략**: `RewindSelectView` 에 `interrupt_callable: Callable[[], Awaitable[None]] | None = None` 주입. Thread 는 None (기존 dict pop), Channel 은 `service.active_runner_for(...)` interrupt + await 묶은 콜백 주입.

---

## §10 `/skill` Channel 지원

### 10-1 현 Thread 로직 (skill_command.py L285~)

```python
channel = self.bot.get_channel(self.claude_channel_id)
thread = await channel.create_thread(...)
# thread 안에서 run_claude_with_config 호출 (prompt = "/skill <name>")
```

### 10-2 Channel 지원

`ChannelSessionService.run_skill_in_channel` 구현 (현재 NotImplementedError stub):

```python
async def run_skill_in_channel(
    self,
    *,
    channel: discord.TextChannel,
    user: discord.User,  # interaction.user
    skill_name: str,
    args: str | None,
    registered: RegisteredChannel,
) -> None:
    """Channel-as-Session 채널에서 /skill 실행.

    handle_message 와 같은 파이프라인을 쓰되 prompt 만 skill 명령으로 구성.
    """
    # worktree 준비 (handle_message 와 동일 로직, _prepare_cwd 재사용)
    worktree_path, branch_name, working_dir = await self._prepare_cwd(
        channel=channel, registered=registered,
    )
    if working_dir is None:
        return

    # DB ensure + turn increment
    await self._repo.ensure(
        channel_id=channel.id,
        project_name=registered.project.name,
        repo_root=registered.project.repo_root,
        worktree_path=worktree_path,
        branch_name=branch_name,
        cwd_mode=registered.cwd_mode,
        model=registered.project.model,
        permission_mode=registered.project.permission_mode,
        channel_name=registered.channel_name,
        category_id=registered.category_id,
    )
    await self._repo.increment_turn(channel.id)

    # Prompt 구성 — 기존 SkillCommandCog 의 prompt 포맷 재사용
    prompt = f"/{skill_name}" + (f" {args}" if args else "")

    # Runner clone + RunConfig + run_claude_with_config
    # (handle_message 의 단계 4~8 재사용)
    ...
```

### 10-3 SkillCommandCog 훅 지점

```python
# skill_command.py 의 /skill 진입점에서
if (
    isinstance(interaction.channel, discord.TextChannel)
    and self._projects is not None
    and self._projects.has(interaction.channel.id)
):
    registered = self._projects.get(interaction.channel.id)
    await self._channel_session_service.run_skill_in_channel(
        channel=interaction.channel,
        user=interaction.user,
        skill_name=skill_name,
        args=args,
        registered=registered,
    )
    await interaction.response.send_message(f"🛠️ 스킬 `{skill_name}` 실행 중...", ephemeral=True)
    return

# 이후 기존 Thread 생성 경로 유지
```

SkillCommandCog `__init__` 에 `projects` / `channel_session_service` optional 파라미터 추가, setup.py 에서 post-inject.

---

## §11 기존 코드 수정 범위

| 파일 | 수정 요약 | 예상 diff |
|------|-----------|-----------|
| `config/projects_config.py` | 전면 재구조화 — `ProjectConfig` → `CategoryProjectConfig`, `RegisteredChannel` 추가, `_channel_index` | +150/-60 |
| `database/channel_session_models.py` | `_MIGRATIONS` 에 ALTER 2개 + INDEX 1개 | +3 |
| `database/channel_session_repo.py` | `ensure()` 시그니처에 `channel_name`, `category_id` 추가, 스키마 반영 | +10 |
| `services/channel_worktree.py` | `plan_paths(..., slug)` 로 변경, `remove_force()` 추가 | +40/-10 |
| `services/channel_session_service.py` | `handle_message` `_prepare_cwd` 가 `RegisteredChannel` 받도록, `run_skill_in_channel` 실제 구현, `channel_name`/`category_id` 기록 | +80/-30 |
| `services/channel_naming.py` | **신규** — 정규식 상수 + Resolver + BranchNamer | +60 |
| `services/projects_watcher.py` | **신규** — 폴링 기반 Watcher | +90 |
| `migration/__init__.py` + `migration/phase2.py` | **신규** — 마이그레이션 스크립트 | +180 |
| `cogs/channel_session.py` | on_guild_channel_create + update 리스너 추가, startup scan, `/ch-worktree-cleanup --force` | +130 |
| `cogs/claude_chat.py` | `/rewind` Channel 분기 | +40 |
| `cogs/skill_command.py` | Channel 분기 훅 + 주입 파라미터 | +30 |
| `setup.py` | Watcher 기동 + migration.run_if_needed + SkillCommandCog 후주입 | +40 |
| `services/__init__.py`, `claude_discord/__init__.py` | export | +15 |
| `docs/channel_as_session.md` | 페이즈 2 섹션 추가 | +200 |
| `docs/CHANNEL_AS_SESSION_PHASE2.md` | **이 문서** | +1000 |

**총 diff 예상**: +2000 / -100 줄. 신규 파일 3개, 기존 파일 수정 8개.

---

## §12 에러 처리 매트릭스

| 에러 유형 | 탐지 위치 | 대응 |
|-----------|-----------|------|
| 규칙 위반 채널 이름 (`wt-Bug123` 등) | `resolve_channel_name` → None | **조용히 무시** (A2). 로그 debug 수준만. |
| 카테고리 미등록 채널에서 메시지 | `ChannelSessionCog.on_message` → `projects.has(cid)` False | 무시 (기존 동작). |
| `projects.json` 파싱 실패 (hot reload) | `ProjectsWatcher._loop` → ConfigError | 경고 로그 + **기존 cfg 유지**. 다음 mtime 변화 시 재시도. |
| 마이그레이션 시 Discord API 실패 | `migration/phase2.py` | 해당 항목 "unknown_category" 로 임시 등록 + 경고 로그. 다음 재시작 시 재시도. |
| 이름 변경 시 dirty worktree | `on_guild_channel_update` → `service.cleanup_channel` → `remove_if_clean` | **보존** (A4). 로그 + (optional) DM. |
| 이름 변경 시 존재하지 않는 카테고리로 이동 | `on_guild_channel_update` 후반 | 구 상태 해제, 신 상태 등록 안 함 (채널은 더 이상 관리 대상 아님). |
| 브랜치 충돌 (`wt-foo` 새로 만들었는데 `channel-session/foo` 가 이미 다른 worktree 에 체크아웃) | `ChannelWorktreeManager.ensure` → `branch_checked_out_elsewhere` | 기존 처리: 사용자에게 임베드 안내. 다른 worktree 를 먼저 정리해야 함. |
| Startup scan 에서 "Discord 에는 있으나 DB 에는 없는 채널" | `_startup_scan` | `_channel_index` 에만 등록. 메시지 올 때 ensure() 에서 DB 생성. |
| Startup scan 에서 "DB 에는 있으나 Discord 에는 없는 채널" | `_startup_scan` 후처리 | 로그 출력. **자동 삭제 안 함** (A3). 사용자가 `/ch-worktree-cleanup` 으로 정리. |
| `/ch-worktree-cleanup --force` 사용자 timeout | `wait_for(...)` timeout | "취소됨" ephemeral. 기본 `remove_if_clean` 로 대체 실행 안 함. |
| hot reload 후 `project.repo_root` 변경 | 기존 worktree 가 구 repo_root 기준 | 새 메시지 시 `ensure()` 가 신 repo_root 기준으로 worktree 생성 시도 → path 다르므로 새 worktree 생성. 구 worktree 는 고아. 사용자 정리 필요. 로그 경고. |
| `channel_name` DB 컬럼이 NULL (마이그레이션 skip 된 오래된 레코드) | `ChannelSessionService.handle_message` 진입 | ensure() 시 Discord 객체에서 재계산 후 DB UPDATE. |

---

## §13 구현 순서 (의존성 그래프 + 커밋 제안)

```
  channel_naming.py  (순수 상수+함수)
       │
       ├─────────────┐
       ▼             ▼
  channel_worktree  config/projects_config
   (plan_paths)      (CategoryProjectConfig, RegisteredChannel, _channel_index)
       │             │
       ▼             ▼
  channel_session_models  (ALTER TABLE)
       │
       ▼
  channel_session_repo  (ensure 시그니처 확장)
       │
       ▼
  channel_session_service  (_prepare_cwd, handle_message, run_skill_in_channel)
       │                                                       │
       │                                                       ▼
       ▼                                              projects_watcher
  channel_session.py (Cog)                          (ProjectsConfig 소비)
  + on_guild_channel_create                           │
  + on_guild_channel_update                           │
  + startup scan                                      │
  + /ch-worktree-cleanup --force                      │
       │                                              │
       ▼                                              │
  migration/phase2.py                                 │
       │                                              │
       └───────────────┬──────────────────────────────┘
                       ▼
                   setup.py
                   (migration.run_if_needed
                    + Watcher.start()
                    + ProjectsConfig 주입)
                       │
                       ▼
             claude_chat.py (/rewind Channel)
             skill_command.py (Channel hook)
                       │
                       ▼
             docs/channel_as_session.md (§페이즈 2 섹션 추가)
```

### 배치 제안 (페이즈 1 의 배치 A/B/C 형식)

| 배치 | 단계 | 내용 | 의존 |
|------|------|------|------|
| **D** — 순수 로직 + DB | 1 `channel_naming.py` (신규) | `resolve_channel_name`, `branch_name` | 없음 |
| | 2 `config/projects_config.py` 재구조화 | `CategoryProjectConfig`, `RegisteredChannel`, `_channel_index`, `register_channel`/`unregister_channel`/`replace_categories` | 1 |
| | 3 `channel_worktree.py::plan_paths(slug)` + `remove_force()` | slug 기반 path, force 탈출구 | 1 |
| | 4 `channel_session_models.py` ALTER + `channel_session_repo.py` ensure 확장 | channel_name + category_id 컬럼 | — |
| | **D smoke**: 신 ProjectsConfig 시나리오 5개, plan_paths slug 변환, resolver 규칙 8개 | |
| | **D commit**: `feat: phase-2 schema (category-keyed projects, naming resolver, slug-based worktree)` | |
| **E** — Watcher + 서비스 통합 | 5 `channel_session_service.py` — `_prepare_cwd(RegisteredChannel)`, `run_skill_in_channel` 실제 구현 | 2, 3, 4 |
| | 6 `projects_watcher.py` (신규) + on_change 콜백 | 2 |
| | 7 `channel_session.py` — 3 이벤트 리스너 + startup scan + /ch-worktree-cleanup --force | 2, 3, 5 |
| | **E smoke**: ProjectsWatcher mtime 감지, create/update/delete mock 이벤트, force 삭제 unit | |
| | **E commit**: `feat: phase-2 Discord event handlers + hot reload + force cleanup` | |
| **F** — 마이그레이션 + 기존 커맨드 | 8 `migration/phase2.py` — 3범위 마이그레이션 | 2, 4 |
| | 9 `claude_chat.py::/rewind` Channel 분기 | `SessionLookupService` (페이즈 1 완성) |
| | 10 `skill_command.py` Channel 훅 + 주입 | 5 |
| | 11 `setup.py` — 마이그레이션 + Watcher 기동 + post-inject | 전부 |
| | **F smoke**: 페이즈 1 DB + projects.json 으로 마이그레이션 실행 → 백업 존재 + 신 스키마 + 5 레코드 backfill 검증 | |
| | **F commit**: `feat: phase-2 migration + /rewind channel + /skill channel + setup wiring` | |
| **G** — 가동 검증 | 봇 재기동 후 Discord 에서 실제 이벤트 실증 | |
| | 검증 1: 기존 5개 main 채널 정상 동작 (regression) | |
| | 검증 2: 새 `wt-test-phase2` 채널 생성 → 자동 등록 로그 → 메시지 전송 → worktree 생성 확인 | |
| | 검증 3: `wt-test-phase2` → `wt-renamed` 로 변경 → 기존 worktree 해제(clean이면 제거) + 신 worktree 등록 | |
| | 검증 4: `wt-test-phase2` 채널 삭제 → cleanup 자동 실행 | |
| | 검증 5: `projects.json` 수정 → 15초 내 hot reload 로그 | |
| | 검증 6: `/rewind` in Channel-as-Session 채널 | |
| | 검증 7: `/skill` in Channel-as-Session 채널 | |
| | 검증 8: `/ch-worktree-cleanup --force` | |
| | **G commit 없음** — 커밋은 F 까지. G 는 PASS 판정만. | |
| **H** — 문서 | 12 `docs/channel_as_session.md` — 페이즈 2 섹션 추가 (카테고리 스키마, 이름 규칙, 이벤트 동작, hot reload, troubleshooting 업데이트) | |
| | **H commit**: `docs: channel-as-session phase-2 usage guide` | |

### 커밋 단위 총 6개

1. `chore: add CHANNEL_AS_SESSION_PHASE2.md design doc`
2. `feat: phase-2 schema (category-keyed projects, naming resolver, slug-based worktree)` [배치 D]
3. `feat: phase-2 Discord event handlers + hot reload + force cleanup` [배치 E]
4. `feat: phase-2 migration + /rewind channel + /skill channel + setup wiring` [배치 F]
5. `docs: channel-as-session phase-2 usage guide` [배치 H]
6. (릴리즈 노트 필요 시) `chore: bump to phase-2`

---

## §14 불변식 및 설계 원칙 (페이즈 1 에서 유지)

1. **Dirty worktree 는 자동 삭제되지 않는다** — 어떤 경로로도. `/ch-worktree-cleanup --force` 만 명시적 예외 (사용자 ✅ 필수).
2. **스레드 모드와 공존** — Hybrid A (채널 ID 분리) + Hybrid B (excluded_channel_ids 게이트). 페이즈 2 에서도 유지.
3. **EventProcessor 덕 타이핑 계약** — `save(...)`, `update_context_stats(...)` 2개 메서드. 페이즈 2 에서 ensure() 시그니처만 확장, save 는 불변.
4. **`turn_count` 는 사용자 턴당 1 증가** — handle_message, run_skill_in_channel 모두 `increment_turn` 1회씩 호출. save() 는 절대 증가 안 함.
5. **projects.json 은 fail-fast** — 파싱/검증 실패 시 봇 기동 거부 (기존). hot reload 실패는 경고 + 유지.

---

## §15 승인 체크리스트 (구현 전 확인)

- [ ] A1 ~ A6 결정사항 반영 확인
- [ ] 이름 패턴 정규식이 코드와 문서에서 공유되는 구조 (§5)
- [ ] 페이즈 1 DB 레코드 5개가 손상 없이 마이그레이션되는 경로 확인 (§3-4)
- [ ] 마이그레이션 실패 시 복구 절차가 문서화됨 (§3-5)
- [ ] 이름 변경 시 dirty worktree 가 확실히 보존되는지 (§4-2, §12)
- [ ] Startup scan 이 이벤트 유실 복구를 책임지는지 (§4-4)
- [ ] `/ch-worktree-cleanup --force` 가 confirmation 을 필수로 하는지 (§8)
- [ ] `/rewind` Channel 지원이 SessionLookupService 경유로 깔끔한지 (§9)
- [ ] `/skill` Channel 지원이 handle_message 파이프라인을 재사용하는지 (§10)
- [ ] 총 diff 추정이 현실적인지 (§11)

---

승인 시 배치 D 부터 착수.
