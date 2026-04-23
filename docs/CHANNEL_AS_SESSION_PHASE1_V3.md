# 페이즈 1 설계 v3 — Channel-as-Session 모드 (v2 대비 Diff)

> 이 문서는 [CHANNEL_AS_SESSION_PHASE1_V2.md](./CHANNEL_AS_SESSION_PHASE1_V2.md) 에 대한 델타만 담는다.
> 핵심 변경: **cwd_mode 도입** — 메인 채널(repo_root 공유) vs 작업 채널(dedicated worktree) 구분.
> v2/v1에서 언급되고 v3에서 다루지 않은 섹션은 v2 그대로 유효하다.

---

## §0 (신규) 채널 유형 개념

두 종류의 채널을 지원한다.

| 유형 | cwd_mode | 예시 채널 | 쓰임 |
|------|----------|-----------|------|
| **메인 채널** (프로젝트당 1개, 영구) | `repo_root` | `#dalpha-main` | 평상시 대화, 크론잡 결과 확인, main pull/push, 읽기/질의 |
| **작업 채널** (필요 시 생성/삭제) | `dedicated_worktree` | `#wt-docs-q2` | 기능/리팩토링/문서 세트 전용, 브랜치 격리 |

v2 설계는 작업 채널만 전제. v3에서 메인 채널을 1급 표현.

---

## §3 projects.json 스키마 — 확장

```diff
  {
    "<channel_id>": {
      "name": "...",
      "repo_root": "...",
+     "cwd_mode": "repo_root" | "dedicated_worktree",   // default: "dedicated_worktree"
+     "shared_cwd_warning": true | false,                // default: false, repo_root일 때만 유효
-     "worktree_base": ".worktrees",
-     "branch_prefix": "channel-session",
+     "worktree_base": ".worktrees",                     // dedicated_worktree일 때만 사용
+     "branch_prefix": "channel-session",                // 동일
      "model": "sonnet" | "opus",
      "permission_mode": "acceptEdits" | "default"
    }
  }
```

### ProjectsConfig 검증 (v3 추가)

- `cwd_mode` 는 `{"repo_root", "dedicated_worktree"}` 중 하나, 미지정 시 `"dedicated_worktree"` 로 보정
- `cwd_mode == "repo_root"` 인데 `worktree_base` / `branch_prefix` 명시 → 경고 로그 + **필드 값 무시**
- `cwd_mode == "dedicated_worktree"` 인데 `shared_cwd_warning == true` → 경고 로그 + **false로 강제**
- 기존 검증(필수 필드, 타입) 유지
- 동일 `repo_root` 중복은 경고만 (의도적 공유 허용)

---

## §4 ChannelSessionRepository 스키마 — cwd_mode 반영

```diff
  CREATE TABLE IF NOT EXISTS channel_sessions (
      channel_id        INTEGER PRIMARY KEY,
      session_id        TEXT,
      project_name      TEXT NOT NULL,
      repo_root         TEXT NOT NULL,
-     worktree_path     TEXT NOT NULL,
-     branch_name       TEXT NOT NULL,
+     worktree_path     TEXT,            -- NULLABLE (cwd_mode="repo_root"는 NULL)
+     branch_name       TEXT,            -- NULLABLE (동일)
+     cwd_mode          TEXT NOT NULL DEFAULT 'dedicated_worktree',
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
```

`ensure()` 시그니처에 `cwd_mode: str` 추가. `worktree_path` / `branch_name` 은 `Optional[str]`. UPDATE 가능 필드로 설계 → 런타임 모드 전환(페이즈 1 밖) 대비.

---

## §5-c (신규) cwd_mode 런타임 불일치 처리

`ChannelSessionService.run()` 진입부에서 projects.json 의 `cwd_mode` vs DB 레코드의 `cwd_mode` 비교:

| 변경 방향 | 대응 |
|-----------|------|
| (DB) `repo_root` → (PJ) `dedicated_worktree` | 경고 로그 + 다음 메시지 시 worktree 생성 + DB UPDATE (worktree_path/branch_name/cwd_mode) |
| (DB) `dedicated_worktree` → (PJ) `repo_root` | 경고 로그 + 기존 worktree 보존 + DB UPDATE (cwd_mode만; worktree_path/branch_name은 남김) + 이후 세션은 repo_root 사용. 기존 worktree 정리는 수동(`/ch-worktree-cleanup`) |

---

## §6 on_message — 실행 경로 분기 (의사코드 추가)

```diff
  # 5. 실제 실행 위임
- await self._service.run(
-     channel=message.channel,
-     user_message=message,
-     project=project,
-     prompt=prompt,
-     images=images,
- )

+ # ChannelSessionService.run() 내부 분기 (의사코드)
+ async def run(self, *, channel, user_message, project, prompt, images):
+     record = await repo.get(channel.id)
+     effective_mode = project.cwd_mode                  # PJ 우선
+     worktree_path, branch_name = None, None
+
+     if effective_mode == "dedicated_worktree":
+         paths = wt_manager.plan_paths(project.repo_root,
+                                        project.worktree_base,
+                                        project.branch_prefix,
+                                        channel.id)
+         ensure_result = await asyncio.to_thread(wt_manager.ensure, paths)
+         if not ensure_result.ok:
+             return await self._report_worktree_error(channel, ensure_result)
+         worktree_path = paths.worktree_path
+         branch_name   = paths.branch_name
+         working_dir   = worktree_path
+     else:  # "repo_root"
+         working_dir = project.repo_root
+
+     await repo.ensure(channel_id=channel.id,
+                       project_name=project.name,
+                       repo_root=project.repo_root,
+                       worktree_path=worktree_path,
+                       branch_name=branch_name,
+                       cwd_mode=effective_mode,
+                       model=project.model,
+                       permission_mode=project.permission_mode)
+
+     runner = runner_cache.get(channel.id).clone(
+         thread_id=channel.id,
+         working_dir=working_dir,
+     )
+
+     system_prompt_parts: list[str] = []
+     if effective_mode == "repo_root" and project.shared_cwd_warning:
+         system_prompt_parts.append(_SHARED_CWD_WARNING_TEXT)
+
+     # run_claude_with_config(RunConfig(repo=channel_session_repo, registry=None, ...))
+     ...
```

`_SHARED_CWD_WARNING_TEXT` (모듈 상수):

```
⚠️ 이 세션의 작업 디렉터리는 다른 프로세스(크론잡, 다른 Discord 세션 등)와 공유됩니다.
파일을 수정하기 전 `git status` 로 외부 변경 여부를 확인하고, 저장 후 충돌 가능성을 고려하세요.
```

---

## §8 /channel-reset — cwd_mode 분기

```diff
  ② 세션/워크트리 현황 조회
-     - record = channel_session_repo.get(channel_id)
-     - paths  = wt_manager.plan_paths(...)
-     - is_dirty = wt_manager.exists(paths) and not wt_manager.is_clean(paths.worktree_path)
+     - record = channel_session_repo.get(channel_id)
+     - if record.cwd_mode == "dedicated_worktree":
+           paths   = wt_manager.plan_paths(...)
+           is_dirty = wt_manager.exists(paths) and not wt_manager.is_clean(paths.worktree_path)
+       else:  # "repo_root"
+           paths, is_dirty = None, None   # dirty 판정 불필요 (파일 건드리지 않음)

  ③ Confirmation 프롬프트
+     if record.cwd_mode == "repo_root":
+         문구: "세션 상태만 리셋합니다. 파일은 건드리지 않습니다.
+                 - session: {session_id or '(none)'}
+                 - turns:   {turn_count}
+                 React ✅ within 60s to confirm, ❌ to cancel."
+     else:
+         (v2 문구 그대로: worktree 경로, dirty 여부 표시)

  ⑥ worktree 처리
-     - is_dirty → 보존 + 로그
-     - clean    → wt_manager.remove_if_clean(paths)
+     if record.cwd_mode == "dedicated_worktree":
+         is_dirty → 보존 + 로그
+         clean    → wt_manager.remove_if_clean(paths)
+     else:
+         (스킵 — worktree 없음)
```

**불변식은 v2 그대로 유지**: dirty worktree는 어떤 경로로도 자동 삭제되지 않음.

---

## §9-b-1 커맨드 — `/ch-worktree-list`, `/ch-worktree-cleanup` 보강

```diff
  | /ch-worktree-list    | projects.json 순회, 각 채널의 ChannelWorktreeManager 상태 표시 |
+ |                      |   단 cwd_mode == "repo_root" 채널은 목록에서 제외 (worktree 개념 없음) |
  | /ch-worktree-cleanup | clean 채널 worktree만 제거. dirty는 절대 스킵. dry_run 인자. |
+ |                      |   대상: cwd_mode == "dedicated_worktree" 채널만 |
```

---

## §10-c 채널 토픽 포맷 — cwd_mode별

```diff
- clean: "Context: 42% | Session: a1b2c3d4"
- dirty: "⚠️ DIRTY | Context: 42% | Session: a1b2c3d4"

+ # repo_root 모드
+ clean: "Context: 42% | Session: a1b2c3d4"
+ dirty: "⚠️ repo dirty | Context: 42% | Session: a1b2c3d4"
+
+ # dedicated_worktree 모드
+ clean: "Context: 42% | Session: a1b2c3d4"
+ dirty: "⚠️ worktree dirty | Context: 42% | Session: a1b2c3d4"
```

dirty 체크 기준 경로:
- `repo_root` → `project.repo_root`
- `dedicated_worktree` → `record.worktree_path`

`/context` 임베드 필드:
- `repo_root` 채널: 필드명 `"Working dir"`, 값 ``` "`{repo_root}` — ⚠️ dirty" ``` / ✅ clean (+ shared_cwd_warning 이면 `" 🔀 shared"` 접미)
- `dedicated_worktree` 채널: 필드명 `"Worktree"`, 값 ``` "`{worktree_path}` — ⚠️ dirty" ``` / ✅ clean

---

## §11 에러 매트릭스 — v3 추가 행

```diff
+ | cwd_mode=repo_root 인데 repo_root가 git 레포 아님 | ChannelSessionService.run() 진입 직전 |
+   세션은 돌지만 /context의 dirty 체크 등은 실패. → 경고 로그 1회 + 토픽에
+   "⚠️ not a git repo" 접두 표시. 실행은 차단하지 않음. |
+
+ | cwd_mode 변경 (projects.json 수정 후 재시작) | ProjectsConfig.load() vs DB 기존 레코드 |
+   repo_root → dedicated_worktree: 경고 로그 + 다음 메시지 시 worktree 생성 + DB UPDATE.
+   dedicated_worktree → repo_root: 경고 로그 + 기존 worktree 보존 + DB UPDATE(cwd_mode만).
+   이후 세션은 repo_root 사용. 기존 worktree 정리는 수동(/ch-worktree-cleanup). |
+
+ | cwd_mode == "repo_root" 인데 shared_cwd_warning 없이 충돌 | 런타임 감지 없음 (사용자 책임) |
+   정책: 운영자가 projects.json에 명시적으로 true 설정. ccdb는 자동 감지 X. |
```

---

## §12 구현 순서 — 변경 없음 (v2 순서 유지, 각 단계 내부에서 cwd_mode 반영)

단 다음 단계에서 v3 변경사항이 구현에 스며든다:

| 단계 | v3 영향 |
|------|---------|
| 1 `projects_config.py` | `cwd_mode` / `shared_cwd_warning` 필드 추가. 검증 로직 §3 따라. |
| 2 `channel_session_repo.py` | 스키마 §4 반영. `ensure()` 시그니처에 `cwd_mode`. `worktree_path`/`branch_name` nullable. |
| 4 `channel_worktree.py` | 변경 없음 (repo_root 모드는 이 매니저를 아예 호출하지 않음). |
| 6 `topic_updater.py` | 토픽 포맷 §10-c. dirty 체크 대상 경로 분기. |
| 7 `channel_session_service.py` | `run()` 분기 §6. 시스템 프롬프트 주입. cwd_mode 불일치 처리 §5-c. |
| 8 `channel_session.py` | `/channel-reset` §8 분기. `/ch-worktree-*` §9 필터. |
| 12 `docs/channel_as_session.md` | "두 종류의 채널" 섹션 + 예시 서버 구조 포함. |

---

## §13 (신규) 예시 Discord 서버 구조

```
🏠 Claude Code Channels (서버)
│
├─ 📁 Dalpha (카테고리)
│   ├─ # main                     ← cwd_mode: repo_root, shared_cwd_warning: true
│   ├─ # wt-docs-q2               ← cwd_mode: dedicated_worktree
│   └─ # wt-migrate-notion        ← cwd_mode: dedicated_worktree
│
└─ 📁 oi-agent (카테고리)
    ├─ # main                     ← cwd_mode: repo_root
    ├─ # wt-feat-auth             ← cwd_mode: dedicated_worktree
    └─ # wt-bugfix-1234           ← cwd_mode: dedicated_worktree
```

projects.json 예시:

```json
{
  "1234567890": {
    "name": "dalpha-main",
    "repo_root": "/code/workspace/Dalpha",
    "cwd_mode": "repo_root",
    "shared_cwd_warning": true,
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  },
  "1234567891": {
    "name": "dalpha-wt-docs-q2",
    "repo_root": "/code/workspace/Dalpha",
    "cwd_mode": "dedicated_worktree",
    "worktree_base": ".worktrees",
    "branch_prefix": "channel-session",
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  }
}
```

---

v3 diff 끝.
