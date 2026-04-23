# Channel-as-Session 모드 사용 가이드

> **대상 독자**: ccdb 봇을 Discord 서버에 띄워 여러 프로젝트를 동시에 관리하려는 운영자.
> **현재 버전**: **페이즈 2** — 자동 등록, 이름 패턴 규칙, hot reload. 페이즈 1 레코드는 봇 시작 시 자동 마이그레이션.

---

## 페이즈 2 요약 (최신 변경)

페이즈 2 에서 다음이 자동화됐다:

- **새 채널 생성**: 카테고리가 `projects.json` 에 등록돼 있고 채널 이름이 `main` 또는 `wt-<slug>` 패턴이면 **자동으로 Channel-as-Session 채널로 등록**. `projects.json` 재편집 불필요.
- **채널 이름 변경**: 기존 worktree 는 보존(dirty 보호)되고 새 이름 기준으로 재평가.
- **채널 삭제**: worktree 가 clean 이면 제거, dirty 면 보존 + 봇 owner 에게 DM (best-effort).
- **projects.json 수정**: 15초 내 hot reload. 봇 재시작 불필요.
- **`/ch-worktree-cleanup --force`**: dirty worktree 도 명시적 ✅ 확인 후 제거 (escape hatch).

### 페이즈 1 → 페이즈 2 스키마 변경

| | 페이즈 1 | 페이즈 2 |
|---|---------|---------|
| **projects.json 키** | channel_id | **category_id** (Discord 카테고리 ID) |
| **cwd_mode 결정** | projects.json 필드 | **채널 이름 패턴** (`main` / `wt-<slug>`) |
| **새 채널 추가** | projects.json 편집 + 봇 재시작 | Discord 에서 채널 생성만 |
| **shared_cwd_warning** | 채널 단위 | **카테고리 단위** (해당 카테고리의 `main` 채널에만 적용) |
| **파일** | `projects.json` | `projects.json` (자동 마이그레이션) + `projects.json.pre-phase2.bak` (백업) |

### 이름 패턴 규칙 (엄격 적용)

- `main` → `cwd_mode="repo_root"` (카테고리 메인 채널)
- `wt-<slug>` where `<slug>` matches `[a-z0-9][a-z0-9_-]*` → `cwd_mode="dedicated_worktree"`
- 그 외 이름 → **조용히 무시** (카테고리 안에 잡담 채널 둘 수 있음)

| 채널 이름 | 인식 결과 |
|-----------|-----------|
| `main` | ✅ repo_root |
| `wt-feat-auth` | ✅ dedicated_worktree, slug=`feat-auth` |
| `wt-docs_v2` | ✅ dedicated_worktree, slug=`docs_v2` |
| `wt-Bug123` | ❌ 무시 (대문자) |
| `wt-` | ❌ 무시 (slug 부재) |
| `wt--double` | ❌ 무시 (`-` 시작) |
| `notes`, `discussion` | ❌ 무시 (패턴 미매칭) |

### 자동 마이그레이션 (페이즈 1 → 페이즈 2)

페이즈 1 환경에서 페이즈 2 코드로 첫 부팅 시 자동 실행:

1. `projects.json` 를 `projects.json.pre-phase2.bak` 로 백업.
2. channel_id 키 엔트리들을 category_id 키로 변환 (하드코딩된 5개 main 채널 매핑 사용 — R1).
3. `_meta.schema_version=2` 센티널 기록 (idempotent 보장).
4. DB 의 기존 5개 레코드에 `channel_name='main'` + 매핑된 `category_id` 백필.

마이그레이션 실패 시 `projects.json.pre-phase2.bak` 로 수동 복구 가능.

---

> **대상 독자**: ccdb 봇을 Discord 서버에 띄워 여러 프로젝트를 동시에 관리하려는 운영자.
> **원본 버전**: 페이즈 1 (초기 출시). 기존 "스레드 모드"와 병행 동작.

---

## 1. 개요

**Channel-as-Session 모드**는 "Discord **채널 하나 = Claude Code 세션 하나**"로 동작하는 모드다. 기존 브리지의 기본 동작인 "스레드 모드" (`#채널에 메시지` → `스레드 생성` → `스레드 안에서 Claude 응답`)와 다르다.

### 차이점

| 측면 | 스레드 모드 (기존) | Channel-as-Session 모드 (신규) |
|------|---------------------|--------------------------------|
| 응답 위치 | 새 스레드 안 | 같은 채널 본문 |
| 세션 경계 | 스레드 = 1 세션 | 채널 = 1 세션 (영속) |
| 스레드 생성 | 매 메시지마다 | 안 함 |
| 프로젝트 구분 | 스레드 이름 | 채널 (카테고리로 그룹화) |
| cwd | `CLAUDE_WORKING_DIR` (전역) | 채널별 (`projects.json`) |
| 설정 | `.env` 의 `DISCORD_CHANNEL_ID` | `/code/.../projects.json` |

### 왜 필요한가

- 프로젝트가 여러 개일 때 **카테고리 + main 채널** 구조로 시각적으로 분리하고 싶다.
- 긴 대화를 하나의 세션으로 유지하고 싶다 — 스레드가 매번 생기면 히스토리가 흩어진다.
- 크론잡 등이 특정 레포 root 에서 돌고 있을 때 Claude 도 같은 cwd 로 붙어 있어야 한다.
- 기능 개발 단위 작업 공간은 별도 워크트리로 격리하고 싶다.

---

## 2. 두 종류의 채널

Channel-as-Session 모드에는 **두 가지 `cwd_mode`** 가 있다.

### 2-1. 메인 채널 — `cwd_mode: "repo_root"` (프로젝트당 1개, 영구)

- **역할**: 평상시 대화, 크론잡 결과 확인, main 브랜치 pull/push, 문서/메모 조회
- **cwd**: `project.repo_root` 그대로 (워크트리 생성 없음)
- **파일 변경**: 직접 main 브랜치에 반영됨 (주의!)
- **예시 채널**: `#dalpha-main`, `#oi-agent-main`

다른 프로세스(크론잡 등)와 같은 레포 root 를 공유하는 경우 `shared_cwd_warning: true` 를 설정한다. 그러면 매 세션 시작 시 Claude 의 시스템 프롬프트에 다음 경고가 주입된다:

```
⚠️ 이 세션의 작업 디렉터리는 다른 프로세스(크론잡, 다른 Discord 세션 등)와
공유됩니다. 파일을 수정하기 전 `git status` 로 외부 변경 여부를 확인하고,
저장 후 충돌 가능성을 고려하세요.
```

### 2-2. 작업 채널 — `cwd_mode: "dedicated_worktree"` (필요 시 생성/삭제)

- **역할**: 기능 개발, 리팩토링, 버그 수정, 큰 단위 문서 작업
- **cwd**: `{repo_root}/.worktrees/ch-{channel_id}` (채널별 git worktree)
- **브랜치**: `channel-session/{channel_id}` 자동 생성
- **파일 변경**: worktree 안에서만 반영 — main 과 격리됨
- **정리**: `/channel-reset` 또는 채널 삭제 시 자동 (dirty 면 보존)
- **예시 채널**: `#wt-docs-q2`, `#wt-feat-auth`

> 페이즈 1 에서는 **메인 채널만 실사용 검증**됐다. 작업 채널은 **스키마/Cog/커맨드 수준 구현 완료**, **실전 검증은 작업 채널 생성 후 진행** 예정.

---

## 3. 설정

### 3-1. 디렉터리 구조

```
/code/claude-hub/
├── projects.json              # 채널 → 프로젝트 매핑
└── data/
    └── channel_sessions.db    # Channel-as-Session 세션 DB (자동 생성)
```

### 3-2. `projects.json` 스키마

```json
{
  "<channel_id_string>": {
    "name":               "사람이 읽는 프로젝트 이름",
    "repo_root":          "/absolute/path/to/git/repo",
    "cwd_mode":           "repo_root" 또는 "dedicated_worktree",
    "shared_cwd_warning": true/false,
    "worktree_base":      ".worktrees",
    "branch_prefix":      "channel-session",
    "model":              "sonnet" / "opus" / "haiku",
    "permission_mode":    "acceptEdits" / "default"
  },
  ...
}
```

**필수 필드**: `name`, `repo_root`
**기본값**:
- `cwd_mode` → `"dedicated_worktree"`
- `shared_cwd_warning` → `false`
- `worktree_base` → `".worktrees"`
- `branch_prefix` → `"channel-session"`

**자동 보정**:
- `cwd_mode=repo_root` 이면 `worktree_base`/`branch_prefix` 무시 (경고 로그).
- `cwd_mode=dedicated_worktree` 이면 `shared_cwd_warning` 강제로 `false` (경고 로그).

**실패 조건** (봇 기동 거부):
- JSON 파싱 오류 (라인/컬럼 포함 에러 메시지).
- 필수 필드 누락 또는 타입 오류 (`channel_id=N, field='X'` 형식으로 정확히 어디가 문제인지 표시).
- `cwd_mode` 값이 두 enum 중 하나가 아님.

### 3-3. `.env` 변수

```bash
# 기존 (스레드 모드용, 유지)
DISCORD_BOT_TOKEN=...
DISCORD_CHANNEL_ID=<기본 #일반 채널 ID>
DISCORD_OWNER_ID=...
CLAUDE_WORKING_DIR=/code/workspace
CLAUDE_COMMAND=claude
CLAUDE_MODEL=sonnet
CLAUDE_PERMISSION_MODE=acceptEdits
MAX_CONCURRENT_SESSIONS=5

# Channel-as-Session 모드 활성화 (추가)
PROJECTS_CONFIG=/code/claude-hub/projects.json
CHANNEL_SESSION_DB=/code/claude-hub/data/channel_sessions.db
```

`PROJECTS_CONFIG` 가 설정되어야만 Channel-as-Session 모드가 활성화된다. 미설정 시 **기존 스레드 모드만 동작** (100% 하위 호환).

### 3-4. Discord 봇 권한 (중요)

봇 역할에 다음 권한 필요 (각 main 채널 또는 카테고리 레벨에서 부여):

| 권한 | 왜 필요 |
|------|---------|
| View Channels | 메시지 읽기 |
| Send Messages | 답장 전송 |
| Read Message History | 이전 메시지 참조 |
| Add Reactions | 🧠/🛠️/💻/✅ 이모지 상태 반응 |
| **Manage Channels** | **채널 토픽 갱신 (cwd 사용률 표시)** |
| Embed Links | 임베드 (툴 결과, 에러, 세션 시작 카드 등) |
| Attach Files | 파일 첨부 응답 |
| Use Slash Commands | `/channel-reset`, `/context` 등 |

> **"Manage Channels" 권한이 없으면** 토픽 갱신이 `403 Forbidden (50013 Missing Permissions)` 로 실패한다. 세션 자체는 정상 동작(api_error 로 suppress) 하지만 채널 토픽에 컨텍스트 % 가 안 뜬다.

---

## 4. 사용 시나리오

### 시나리오 A: 메인 채널에서 평상시 대화

`#dalpha-main` 채널에 메시지 전송:

```
로컬에서 main 브랜치 상태 확인하고, 최근 커밋 요약해줘.
```

- 봇: 채널 본문에 직접 답장 (스레드 생성 없음).
- cwd: `/code/workspace/Dalpha`.
- Claude: `git status`, `git log --oneline -20` 등 실행.

### 시나리오 B: main 브랜치에 pull/push

`#oi-agent-main` 채널에:

```
upstream 변경사항 pull 받고, 최근에 수정한 config 변경 커밋해서 push해줘.
```

- 봇: 채널 본문 답장.
- Claude: `git pull`, `git add`, `git commit`, `git push` 수행.
- `cwd_mode=repo_root` 이므로 worktree 없이 직접 main 에 작업.

### 시나리오 C: 작업 채널 생성하여 기능 개발

1. Discord 에서 `#wt-feat-auth` 채널 수동 생성 (예: 1234567890 ID).
2. `projects.json` 에 추가:
   ```json
   "1234567890": {
     "name": "oi-agent-feat-auth",
     "repo_root": "/code/workspace/oi-agent-fnco-chatbot",
     "cwd_mode": "dedicated_worktree"
   }
   ```
3. 봇 재시작 (`PROJECTS_CONFIG` 리로드).
4. `#wt-feat-auth` 에 메시지:
   ```
   JWT 기반 인증 미들웨어를 추가해줘. middleware/auth.py 에 구현하고 테스트도 작성.
   ```
5. 봇 첫 메시지 시 자동으로:
   - `{repo_root}/.worktrees/ch-1234567890/` 워크트리 생성.
   - `channel-session/1234567890` 브랜치 체크아웃.
   - 해당 디렉터리에서 Claude 가 파일 수정.

### 시나리오 D: 작업 완료 후 정리

1. 작업이 끝난 `#wt-feat-auth` 에서 `/channel-reset` 슬래시 커맨드.
2. 봇: 확인 메시지 (worktree 경로, dirty 여부 표시) + ✅/❌ 리액션 대기.
3. ✅ 클릭 → 확인.
   - **worktree 가 clean 이면**: `git worktree remove` 실행 + DB 레코드 삭제.
   - **worktree 가 dirty 이면**: worktree 보존 + DB 레코드 삭제 + 로그. 사용자가 수동으로 `git worktree remove` 해야 함 (데이터 보호 invariant).
4. Discord 채널 자체는 남아있음 — 필요 시 수동으로 삭제.

채널을 Discord 에서 삭제하면 `on_guild_channel_delete` 이벤트로 같은 정리 로직이 자동 실행 (dirty 보존 규칙 동일).

---

## 5. 슬래시 커맨드 호환성

| 커맨드 | Thread 모드 | Channel-as-Session 모드 |
|--------|-------------|---------------------------|
| `/help` | ✅ 그대로 | ✅ 그대로 |
| `/stop` | ✅ | ✅ — ChannelSessionService 에 SIGINT |
| `/compact` | ✅ | ✅ — 세션에 `/compact` prompt |
| `/clear` | ✅ | ❌ `/channel-reset` 으로 안내 (dirty check 없어 위험) |
| `/rewind` | ✅ | ⏳ 페이즈 2 예정 — 현재 안내 메시지만 |
| `/fork` | ✅ | ❌ 개념상 의미 없음 (채널 개념) |
| `/resume-info` | ✅ | ✅ — Channel 세션 ID 표시 |
| `/context` | ✅ | ✅ — Working dir / Worktree + clean/dirty 필드 추가 |
| `/channel-reset` | ❌ (Thread 에선 무의미) | ✅ 전용 |
| `/ch-worktree-list` | ❌ | ✅ 전용 |
| `/ch-worktree-cleanup` | ❌ | ✅ 전용 |
| `/sessions`, `/resume`, `/model-*`, `/effort-*`, `/tools-*`, `/sync-*` | ✅ 전역 | ✅ 전역 (어느 채널에서든) |
| `/worktree-list`, `/worktree-cleanup` | ✅ 스레드 모드의 `wt-{tid}` 전용 | — Channel-as-Session worktree 는 `/ch-worktree-*` 사용 |

### 5-1. `/channel-reset` 흐름

```
유저가 Channel-as-Session 채널에서 /channel-reset
  ↓
봇: 현재 상태 조회 (worktree 존재, dirty 여부, session_id, turn_count)
  ↓
봇: cwd_mode 별 확인 메시지 + ✅/❌ 리액션 (60초 대기)
  ├ repo_root : "세션 상태만 리셋합니다. 파일은 건드리지 않습니다."
  └ dedicated : "worktree를 삭제합니다. (dirty면 보존)"
  ↓
유저가 ✅
  ↓
봇: (1) 활성 runner 있으면 interrupt
    (2) cwd_mode=dedicated 이면 remove_if_clean (dirty 보존)
    (3) DB 레코드 delete
    (4) RunnerCache invalidate (다음 메시지에서 재생성)
    (5) 결과 임베드 표시
```

### 5-2. `/ch-worktree-cleanup`

모든 `cwd_mode=dedicated_worktree` 채널의 worktree 를 순회하며 clean 만 제거. `dry_run=True` 로 실행하면 실제 삭제 없이 계획만 표시.

---

## 6. 운영 이슈 및 Troubleshooting

### 6-1. 봇 권한 부족 (`403 Missing Permissions`)

**증상**: 로그에 `TopicUpdater: channel.edit failed ... 403 Forbidden (50013)` 반복.

**원인**: 봇 역할에 해당 채널에서 **Manage Channels** 권한이 없다.

**해결**: Discord 서버 설정 → Roles → 봇 역할 → "Manage Channels" 체크.
또는 채널별 overrride 에서 허용.

세션 자체에는 영향 없음 (TopicUpdater 가 에러를 suppress 하고 토픽 갱신만 스킵).

### 6-2. `projects.json` 편집 후 봇 재시작 없이 반영 안 됨

페이즈 1 에서는 **봇 기동 시 1회 로드**. 런타임 reload 는 페이즈 2 예정.

임시 방안: 봇 재시작.

```bash
pkill -f "claude_discord.main"
uv run python -m claude_discord.main > /tmp/bot.log 2>&1 &
```

재시작 후에도 기존 `channel_sessions.db` 의 session_id / context stats 는 보존됨 (ensure() sync-or-create 동작).

### 6-3. Dirty worktree 가 자동 삭제되지 않음

**Invariant**: dirty worktree 는 어떤 경로로도(`/channel-reset`, 채널 삭제, `/ch-worktree-cleanup`) **자동 삭제되지 않는다**. 이건 의도된 보호 장치다.

**수동 정리**:
```bash
cd /code/workspace/<project>
git status .worktrees/ch-<channel_id>
# 필요한 변경사항 commit 또는 stash
git worktree remove .worktrees/ch-<channel_id>
```

### 6-4. Channel-as-Session 채널에서 메시지를 보냈는데 봇이 무반응

확인 순서:
1. **`projects.json` 에 그 채널 ID 가 등록돼 있는가?** (`channel_session_repo` 에 아무 레코드 없으면 등록 안 됨)
2. **봇이 실행 중인가?** `ps -ef | grep claude_discord`
3. **로그에 `Channel-as-Session enabled: N project(s)` 있는가?** (없으면 `PROJECTS_CONFIG` 미로드)
4. **봇에게 해당 채널 View/Send 권한이 있는가?**

### 6-5. cwd_mode 를 바꿨는데 기존 상태가 남아있음

`projects.json` 의 `cwd_mode` 를 변경하면 다음 메시지부터 새 모드가 적용된다. 기존 worktree 는 자동 정리되지 않으므로 (데이터 보호) `/ch-worktree-cleanup --dry-run` 후 수동 정리 권장.

```
dedicated_worktree → repo_root: 기존 worktree 는 남아있음 (수동 정리)
repo_root → dedicated_worktree: 다음 메시지에서 새 worktree 생성
```

### 6-6. 스레드 모드와 동시에 쓰고 있는데, 기존 `#일반` 스레드가 생성 안 됨

`projects.json` 에 `#일반` 채널 ID 를 실수로 등록했을 가능성. 등록되면 ClaudeChatCog 의 on_message 게이트에서 그 채널은 skip 된다.

`projects.json` 에서 해당 ID 제거 후 봇 재시작.

---

## 7. 한계 및 향후 계획

### 페이즈 1 의 한계

- **`/rewind` Channel 모드 미지원** — 현재 거부 메시지만 출력. 구현은 페이즈 2.
- **projects.json hot reload 없음** — 설정 변경 시 봇 재시작 필요.
- **작업 채널 실전 검증 미완** — dedicated_worktree 모드의 git 워크트리 생성/정리 로직은 unit test 로 검증됐으나 실제 Discord 환경에서 end-to-end 가동은 페이즈 2.
- **`/skill` Channel 모드 미완** — `SkillCommandCog` 는 기본적으로 스레드 생성 후 스킬 실행. Channel 채널에서 호출 시 동작은 페이즈 2 에서 정식 통합.
- **런타임 cwd 전환 커맨드 없음** — `/worktree-mode` 같은 커맨드로 채널 하나를 repo_root ↔ dedicated 로 전환하는 기능은 페이즈 1 밖. DB 스키마는 대비됨.

### 페이즈 2 계획 (예고)

- 작업 채널 실전 검증 + 문서 보강
- projects.json hot reload
- `/rewind` Channel 모드
- `/skill` Channel 모드
- 대시보드 — 프로젝트별 활성 세션 카드

---

## 8. 예시 Discord 서버 구조

```
🏠 Claude Code Channels (서버)
│
├─ # 일반                            ← 스레드 모드 (CLAUDE_WORKING_DIR)
│
├─ 📁 Dalpha (카테고리)
│   ├─ # dalpha-main                ← cwd_mode=repo_root, shared_cwd_warning=true
│   ├─ # wt-docs-q2                 ← cwd_mode=dedicated_worktree (작업 채널, 페이즈 2)
│   └─ # wt-migrate-notion          ← cwd_mode=dedicated_worktree
│
├─ 📁 oi-agent-fnco-chatbot (카테고리)
│   ├─ # oi-agent-main              ← cwd_mode=repo_root
│   └─ # wt-feat-auth               ← cwd_mode=dedicated_worktree
│
├─ 📁 fnco-databricks (카테고리)
│   └─ # fnco-databricks-main       ← cwd_mode=repo_root
│
├─ 📁 dalpha-dynamic-edge (카테고리)
│   └─ # dalpha-dynamic-edge-main   ← cwd_mode=repo_root
│
└─ 📁 oi-oliveyoung-crawling (카테고리)
    └─ # oi-oliveyoung-main         ← cwd_mode=repo_root
```

### 해당 `projects.json` 예시

```json
{
  "1496803518508699798": {
    "name": "Dalpha-main",
    "repo_root": "/code/workspace/Dalpha",
    "cwd_mode": "repo_root",
    "shared_cwd_warning": true,
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  },
  "1496803536762175489": {
    "name": "oi-agent-main",
    "repo_root": "/code/workspace/oi-agent-fnco-chatbot",
    "cwd_mode": "repo_root",
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  },
  "1496803553484734504": {
    "name": "fnco-databricks-main",
    "repo_root": "/code/workspace/fnco-databricks",
    "cwd_mode": "repo_root",
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  },
  "1496803565858066533": {
    "name": "dalpha-dynamic-edge-main",
    "repo_root": "/code/workspace/dalpha-dynamic-edge",
    "cwd_mode": "repo_root",
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  },
  "1496803585906708562": {
    "name": "oi-oliveyoung-main",
    "repo_root": "/code/workspace/oi-oliveyoung-crawling",
    "cwd_mode": "repo_root",
    "model": "sonnet",
    "permission_mode": "acceptEdits"
  }
}
```

---

## 9. 페이즈 1 가동 검증 요약

페이즈 1 출시 전 실제 Discord 환경에서 검증된 시나리오 3건:

| # | 시나리오 | 결과 |
|---|----------|------|
| 1 | `PROJECTS_CONFIG` 미설정으로 기동 → 기존 스레드 모드만 동작 | ✅ PASS |
| 2 | `PROJECTS_CONFIG` 설정 → 스레드 모드(#일반) + Channel-as-Session(`#dalpha-main`) 공존, shared_cwd_warning 주입 확인 (Claude 가 YES 응답) | ✅ PASS |
| 3 | 5개 main 채널 (`Dalpha`, `oi-agent`, `fnco-databricks`, `dalpha-dynamic-edge`, `oi-oliveyoung`) 순차 메시지 → DB 5개 레코드 전부 `cwd_mode=repo_root`, `worktree_path=NULL`, `turn_count=1`, `error_count=0` | ✅ PASS |

추가 검증된 invariant:
- 5개 프로젝트 레포 중 어느 repo 에도 `.worktrees/` 디렉터리 생성 없음 (repo_root 모드 정책 준수).
- `turn_count` 는 사용자 메시지당 정확히 +1 (EventProcessor 가 save() 를 여러 번 호출해도 중복 증가 없음 — `ChannelSessionService.handle_message` 에서 `increment_turn` 1회 호출로 의미 보존).

---

## 10. 질문 / 버그 리포트

코드 외 운영 이슈 (권한, 레포 미등록 등) 는 이 문서 §6 참조.

코드 버그 의심 시:
1. `/tmp/bot-*.log` 뒷부분 공유
2. `/code/claude-hub/data/channel_sessions.db` 의 해당 채널 레코드 (session_id, turn_count, error_count)
3. `projects.json` 전체 (토큰은 마스킹)

세 가지를 함께 제출하면 재현 가능성이 높다.
