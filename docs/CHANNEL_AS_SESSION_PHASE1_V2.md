# 페이즈 1 설계 v2 — Channel-as-Session 모드 (Diff)

> 이 문서는 [CHANNEL_AS_SESSION_PHASE1.md](./CHANNEL_AS_SESSION_PHASE1.md) 에 대한 델타만 담는다.
> 언급되지 않은 섹션은 v1 그대로 유효하다.

---

## 사전 조사 결과 (페이즈 2 진입 전 1줄 보고)

기존 `database/models.py::init_db` (L148~153)는 `contextlib.suppress(Exception)` 로 모든 예외를 무차별 억제한다 — **v2에서는 `aiosqlite.OperationalError`만 suppress, 나머지는 raise** 방식으로 좁힌다.

---

## §2 신규 파일 목록 — 변경

```diff
  | 파일 | 책임 |
  |------|------|
  | claude_discord/config/projects_config.py       | projects.json 로더. dataclass ProjectConfig. fail-fast. |
  | claude_discord/database/channel_session_repo.py| ChannelSessionRepository (별도 DB). EventProcessor 덕 타이핑 호환. |
  | claude_discord/database/channel_session_models.py | 위 DB의 스키마 + init_db. |
  | claude_discord/services/channel_worktree.py    | ChannelWorktreeManager — .worktrees/ch-{cid} 전용. |
+ | claude_discord/services/runner_cache.py        | RunnerCache — 프로젝트별 ClaudeRunner 인스턴스 관리 (생성/조회/제거). |
+ | claude_discord/services/topic_updater.py       | TopicUpdater — rate-limited 토픽 갱신 + 80% 경고 방출 + hysteresis 해제. |
- | claude_discord/services/channel_session_service.py | ChannelSessionService — Runner 캐시, 세션 실행, 토픽 갱신, 상태 관리. |
+ | claude_discord/services/channel_session_service.py | ChannelSessionService — on_message 수신 이후 오케스트레이션만 (ensure worktree → ensure session → runner 선택 → run_claude_with_config → active task 관리). RunnerCache/TopicUpdater를 생성자 주입으로 받음. |
  | claude_discord/services/session_lookup.py      | SessionLookupService — channel/thread id 라우팅. |
  | claude_discord/cogs/channel_session.py         | ChannelSessionCog — on_message + on_guild_channel_delete + /channel-reset + /ch-worktree-*. |
  | docs/channel_as_session.md                     | 페이즈 3 사용 가이드. |
```

**2-a. Runner 캐시 위치 (갱신)** — `ChannelSessionService` 내부 dict → **`RunnerCache` 별도 클래스**로 이동. 생성자에서 `ProjectsConfig`를 받아 프로젝트별 `ClaudeRunner` 인스턴스를 eager 생성, `get(channel_id) -> ClaudeRunner`로 조회. `invalidate(channel_id)`는 `/channel-reset` 시 호출.

---

## §4 SQLite 스키마 — 마이그레이션 처리 보강

```diff
  마이그레이션: _MIGRATIONS 리스트를 별도로 두되 idempotent 원칙(...) 적용.
- 기존 database/models.py 방식과 동일.
+ 기존 database/models.py 는 contextlib.suppress(Exception) 로 모든 예외를 억제하는데,
+ v2에서는 **aiosqlite.OperationalError 만 suppress** 하고 그 외는 전파한다.
+ 의도: "컬럼 이미 존재" 같은 idempotent 재실행은 통과하되, 구문 오류/디스크 풀 등 진짜 문제는
+ 기동 시점에 raise되어 fail-fast.
```

---

## §8 /channel-reset 흐름 — 80% 플래그 해제 지점 명시

```diff
  ⑦ DB 처리
      - channel_session_repo.delete(channel_id)   # 레코드 전체 삭제
+     # 레코드 삭제가 곧 warned_80pct_at 소실을 의미하므로 clear 호출 생략 가능.
+     # /compact 경로에서는 delete가 아니라 clear_80pct_warned 명시 호출 필요.
```

---

## §9 슬래시 커맨드 분기 전략 — 전면 재작성

### 9-a. 현재 각 커맨드 현황 (표는 v1 그대로)

### 9-b. 해결 전략 — 전환

```diff
- 두 가지 옵션 중 "신규 Cog에 채널 버전 복제" 채택.
+ 전략 전환: 네임스페이스 분리(/ch-*) 철회, **기존 커맨드의 게이트 조건 완화**를 채택.
+ 이유: 사용자가 채널 모드를 의식해야 하는 UX가 비실용적.
```

기존 커맨드의 `isinstance(..., Thread)` 거부 블록을 다음 패턴으로 교체:

```python
if not self._is_session_channel(interaction.channel):
    await interaction.response.send_message(
        "This command can only be used in a Claude chat thread or a "
        "Channel-as-Session channel.", ephemeral=True
    )
    return
lookup = await self._session_lookup.resolve(interaction.channel.id)
if lookup.kind == "none":
    await interaction.response.send_message("No session found.", ephemeral=True)
    return
# 이후 lookup.session_id, lookup.working_dir, lookup.repo를 그대로 사용.
```

`_is_session_channel(channel)` 은 다음을 만족하면 True:
- `isinstance(channel, discord.Thread)`, 또는
- `isinstance(channel, discord.TextChannel) and channel.id ∈ projects_config`

수정 범위는 커맨드당 2~4줄 — 조건 완화 + lookup 호출. 파일 전체 리팩터 X.

### 9-b-1. 커맨드별 확장/제외 정책 (신규 표)

| 커맨드 | 정책 | 수정 라인 수 추정 |
|--------|------|-------------------|
| `/stop` | **확장**: Thread + Channel-as-Session 채널 | 3 |
| `/compact` | **확장** | 4 (working_dir_override를 lookup에서 취득) |
| `/context` | **확장** — 추가로 dirty 필드 표시 (§10 참조) | 5 |
| `/resume-info` | **확장** | 3 |
| `/clear` | **역할 이관** — Channel-as-Session 채널에서 호출 시 ephemeral 안내: "Use `/channel-reset` in this channel." | 3 |
| `/fork` | **유지** — Thread 전용 거부. Channel에서는 "Forking is not supported in Channel-as-Session channels." | 1 |
| `/rewind` | **페이즈 1 밖** — 단 `SessionLookupService` 인터페이스는 현재 working_dir/session_id/repo를 이미 노출하므로 나중에 동일 패턴으로 확장 가능. 코드 변경 없음. | 0 |
| `/sessions`, `/resume`, `/model-*`, `/effort-*`, `/tools-*`, `/sync-*` | **무변경** — Thread 체크 없이 전역 동작. | 0 |
| `/worktree-list`, `/worktree-cleanup` | **유지** — 기존 `WorktreeManager` (wt-{tid}) 전용. | 0 |
| **신설 `/ch-worktree-list`** | projects.json 순회, 각 채널의 `ChannelWorktreeManager` 상태(clean/dirty) 표시 | — |
| **신설 `/ch-worktree-cleanup`** | clean 채널 worktree만 제거. dirty는 절대 스킵. `dry_run` 인자. | — |
| **신설 `/channel-reset`** | §8 그대로 | — |

신설 커맨드는 `/channel-reset`, `/ch-worktree-list`, `/ch-worktree-cleanup` **총 3개**로 축소.

### 9-c. 스킬 커맨드 — 변경 없음 (v1 유지)

---

## §10 공존 규칙 — 토픽 포맷에 dirty 플래그 추가

### 10-c. 채널 토픽 포맷 (신규 서브섹션)

TopicUpdater가 생성하는 포맷:
- clean: `"Context: 42% | Session: a1b2c3d4"`
- dirty: `"⚠️ DIRTY | Context: 42% | Session: a1b2c3d4"`

dirty 판정은 TopicUpdater가 토픽 갱신 시점에 `ChannelWorktreeManager.is_clean(path)` 를 호출해 lazy 계산. `is_clean` 은 `git status --porcelain` 한 번이라 비용 무시 가능.
토픽 실제 write는 기존 rate limit 정책 그대로(5분/5%p 변화시). **dirty 상태가 바뀐 것만으로도 "값이 바뀐" 것으로 취급하여 갱신 트리거**.

`/context` 응답 임베드에도 dirty 필드를 추가:
- Field: `"Worktree"` Value: ``` "`/.../.worktrees/ch-123` — ⚠️ DIRTY" ``` 또는 ``` "`/.../.worktrees/ch-123` — ✅ clean" ```

---

## §11 에러 매트릭스 — 80% hysteresis + dirty 가시화

```diff
  | 80% 경고 중복 방지 | EventProcessor 후처리 | ... mark_80pct_warned. ...
-   clear_80pct_warned는 /channel-reset과 /ch-compact 성공 후에만 호출. |
+   clear_80pct_warned는 다음 조건 중 하나 충족 시 호출:
+     (a) /channel-reset 성공 (또는 channel_session_repo.delete로 자동 소실)
+     (b) /compact 성공 후
+     (c) **자동 hysteresis**: EventProcessor 후처리에서 context_used/context_window < 0.65
+   임계값은 services/topic_updater.py 상단에 상수화:
+     WARN_THRESHOLD  = 0.80
+     CLEAR_THRESHOLD = 0.65 |

+ | dirty worktree 가시성 | TopicUpdater.compute_topic / /context | 매 세션 종료 시점에
+   is_clean() 호출, 토픽 접두사 + /context 임베드 필드에 반영. |
+
+ | 마이그레이션 예외 | ChannelSessionModels.init_db for loop |
+   aiosqlite.OperationalError만 suppress (컬럼 이미 존재 등 idempotent 케이스).
+   그 외 예외는 raise → 봇 기동 거부. |
```

---

## §12 구현 순서 — 재정렬

```diff
- | 1 | config/projects_config.py |
- | 2 | database/channel_session_models.py + channel_session_repo.py |
- | 3 | services/channel_worktree.py |
- | 4 | services/session_lookup.py |
- | 5 | services/channel_session_service.py |
- | 6 | cogs/channel_session.py (on_message + on_guild_channel_delete만) |
- | 7 | claude_chat.py + setup.py + __init__.py |
- | 8 | /channel-reset |
- | 9 | /ch-* 슬래시 커맨드들 |
- | 10 | docs |

+ | 1  | config/projects_config.py                                                                 | chore: add ProjectsConfig loader |
+ | 2  | database/channel_session_models.py + channel_session_repo.py                              | chore: add ChannelSession DB layer |
+ | 3  | services/session_lookup.py                                                                | chore: add SessionLookupService (앞당김) |
+ | 4  | services/channel_worktree.py                                                              | feat: add ChannelWorktreeManager |
+ | 5  | services/runner_cache.py                                                                  | feat: add RunnerCache |
+ | 6  | services/topic_updater.py                                                                 | feat: add TopicUpdater (rate-limited + 80% warn + hysteresis) |
+ | 7  | services/channel_session_service.py                                                       | feat: add ChannelSessionService (orchestration only) |
+ | 8  | cogs/channel_session.py (on_message + on_guild_channel_delete + /channel-reset + /ch-worktree-* 전부 포함) | feat: add ChannelSessionCog with project routing + commands |
+ | 9  | cogs/claude_chat.py 수정 (excluded_channel_ids + /stop /compact /context /resume-info 게이트 완화; /clear 안내; /fork 안내) | refactor(claude_chat): relax session-command gates for Channel-as-Session (diff: ~15 lines) |
+ | 10 | cogs/session_manage.py 수정 (/context /resume-info 게이트 완화)                             | refactor(session_manage): relax session-command gates (diff: ~10 lines) |
+ | 11 | setup.py + __init__.py                                                                    | feat: integrate ChannelSession into setup_bridge |
+ | 12 | docs/channel_as_session.md                                                                | docs: add channel-as-session usage guide |
```

각 단계 끝에 `python -c "import claude_discord"` smoke test.
단계 9~10은 **커밋 메시지에 실제 diff 라인 수 명시**.

### 의존성 그래프 — 갱신

```
  ProjectsConfig
     │
     ├─────────────────────────┐
     ▼                         ▼
  ChannelSessionModels    SessionLookupService
     │                         │
     ▼                         │
  ChannelSessionRepository ◀───┤
     │                         │
     ▼                         │
  ChannelWorktreeManager       │
     │                         │
     ▼                         │
  RunnerCache                  │
     │                         │
     ▼                         │
  TopicUpdater                 │
     │                         │
     ▼                         │
  ChannelSessionService        │
     │                         │
     ▼                         ▼
  ChannelSessionCog ──── 기존 Cog 게이트 완화 (SessionLookup 주입)
     │
     ▼
  setup.py + __init__.py export
```

---

v2 diff 끝. OK 하면 페이즈 2 단계 1부터 착수한다.
