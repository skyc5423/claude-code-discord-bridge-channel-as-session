# Ralph 실행 지시문

**지금 즉시 Channel-as-Session 페이즈 2 구현을 시작한다.**

## 읽을 것

다음 문서를 먼저 읽고 모든 원칙을 내재화한 뒤 작업에 들어간다:

1. `docs/RALPH_PHASE2_PLAYBOOK.md` — **필수 플레이북**. 실행 순서, 원칙, 모든 결정사항이 들어있다.
2. `docs/CHANNEL_AS_SESSION_PHASE2.md` — 설계 문서 (master spec).
3. `docs/CHANNEL_AS_SESSION_PHASE1_V3.md` — 페이즈 1 설계 (참조용).
4. `docs/channel_as_session.md` — 페이즈 1 사용자 가이드.

## 핵심 원칙 (요약 — 자세한 건 playbook 참조)

- **무조건 끝까지 진행**. 어떤 문제가 발생해도 스스로 해결하고 완주한다.
- **사용자에게 절대 질문하지 않는다.** 판단은 ralph 가 내리고 실행한다.
- **중단하지 않는다.** 막히면 다른 방법을 시도하고, 그래도 안 되면 단순화하고, 최후에는 stub 으로 넘어간다.
- 모호한 결정은 `/tmp/ralph-decisions.md` 에 로그만 남기고 계속 진행.
- 각 배치마다 품질 게이트 (ruff + pytest + import smoke) 통과 후 commit.
- **배치 D → E → F → G-auto → H** 순서 완주.

## 시작

지금 배치 D 부터 착수. playbook 의 §3.1 부터 순차 실행.

완료 시점에는 아래 양식으로 최종 보고를 출력한다:

```
=== PHASE 2 IMPLEMENTATION COMPLETE ===
Batches executed: D, E, F, G-auto, H
Total commits: <N>
Total smoke tests: <N>
Decisions logged: /tmp/ralph-decisions.md
G-auto results: /tmp/ralph-g-auto-results.md

User next steps:
1. Review commits: git log --oneline -15
2. Review decisions: cat /tmp/ralph-decisions.md
3. Restart bot in tmux to apply
4. Run manual Discord validation
```

**시작.**
