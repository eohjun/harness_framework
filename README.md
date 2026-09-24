# Harness Framework

Claude Code로 기능 구현을 여러 step으로 나누고, step마다 독립된 헤드리스 세션을 실행한 뒤 검증·커밋까지 자동으로 이어가는 프로젝트 템플릿이다. [jha0313/harness_framework](https://github.com/jha0313/harness_framework)의 fork다.

## 구성

```
CLAUDE.md                      # 프로젝트 규칙 (템플릿 — 채워서 사용)
docs/                          # PRD, ARCHITECTURE, ADR, UI_GUIDE (템플릿 — 채워서 사용)
.claude/
├── commands/harness.md        # /harness — 탐색 → 논의 → step 설계 → 파일 생성 → 실행
├── commands/review.md         # /review — 변경 사항을 아키텍처·ADR·CRITICAL 규칙으로 검토
├── hooks/dangerous_cmd_guard.py  # PreToolUse(Bash): 되돌릴 수 없는 명령 차단
├── hooks/stop_verify.py       # Stop: 변경이 있으면 lint/build/test, 실패 시 종료를 막음
└── settings.json              # 위 훅 등록
scripts/
├── execute.py                 # step 실행기
└── test_*.py                  # 실행기·훅 테스트
phases/                        # /harness가 생성 (task별 index.json, step{N}.md)
```

템플릿 기본값(CLAUDE.md의 명령어, `docs/ARCHITECTURE.md`의 디렉토리 구조, Stop 훅의 `npm run lint/build/test`)은 Next.js + npm 프로젝트를 가정한다. 다른 스택이면 함께 바꾼다.

## 요구 사항

- git, Python 3 (3.14에서 검증), [Claude Code](https://claude.com/claude-code) CLI (`claude`)
- npm — Stop 훅이 `package.json`이 있는 프로젝트에서 사용

## 사용 흐름

1. 이 템플릿으로 새 프로젝트를 만들고 `CLAUDE.md`와 `docs/*.md`의 `{...}` 자리표시자를 채운다. 이 문서들은 매 step 프롬프트에 그대로 주입된다.
2. Claude Code에서 `/harness`를 실행한다. 문서를 읽고 결정 사항을 논의한 뒤, 승인한 step 설계를 `phases/{task}/` 아래 파일로 만든다.
3. 실행한다.

   ```bash
   python3 scripts/execute.py {task}          # 순차 실행
   python3 scripts/execute.py {task} --push   # 완료 후 feat-{phase} 브랜치 push
   ```

4. 필요하면 `/review`로 결과를 검토한다.

각 step 파일(`step{N}.md`)에는 `## Acceptance Criteria` 절과 비대화형 ` ```bash ` 블록이 있어야 한다. 형식은 `.claude/commands/harness.md`의 D-3절을 따른다.

## execute.py가 하는 일

- `phases/` 밖에 커밋되지 않은 변경이 있으면 시작하지 않는다. step 커밋이 `git add -A`로 전부 담기 때문이다.
- `feat-{phase}` 브랜치를 만들거나 checkout한다.
- step마다 `claude -p --dangerously-skip-permissions` 세션을 띄운다. 프롬프트에는 CLAUDE.md, `docs/*.md`, 완료된 step의 summary, 직전 실패 원인이 들어간다.
- 세션이 `completed`를 보고해도 AC 블록을 `bash -e -o pipefail`로 직접 재실행해 통과해야 완료로 인정한다.
- step이 추가한 줄(`phases/`, `*.md` 제외)에 lint·타입 억제 주석(`# noqa`, `# type: ignore`, `eslint-disable`, `@ts-ignore` 등)이 있으면 커밋하지 않고 재시도한다. 린트 설정 변경으로 우회하는 것은 프롬프트 규칙으로만 금지한다.
- 실패하면 원인을 피드백해 새 세션으로 최대 3회 재시도한다. 실패 원인에는 AC 출력, 세션의 `error_message`, 비정상 종료·timeout(30분), index.json 파손, pre-commit 훅에 막힌 커밋이 포함된다.
- 커밋은 execute.py만 한다. 코드는 `feat(...)`, index.json은 `chore(...)`로 나누고, error·blocked로 끝난 step의 부분 작업은 `wip(...)`로 남긴다.

종료 코드:

| 코드 | 의미 | 다음 행동 |
|------|------|-----------|
| 0 | 모든 step 완료 | 결과 확인 |
| 1 | step error 또는 실행 오류 (git, 작업 트리, 파일·AC 블록 누락, claude CLI 없음, 커밋·push 실패) | `index.json`의 `error_message`나 출력의 ERROR를 확인 |
| 2 | step blocked (API 키, 인증 등 사용자 개입 필요) | `blocked_reason` 해소 |

error·blocked를 해소한 뒤에는 해당 step의 `status`를 `"pending"`으로 되돌리고 `error_message`/`blocked_reason`을 지운 다음 다시 실행한다. 메인 세션의 운영 지침은 `.claude/commands/harness.md`의 E-1절에 있다.

## 훅

- **dangerous_cmd_guard.py** — `rm -rf`류(플래그 순서·분리 무관), `git reset --hard`, `git push -f/--force`, `DROP TABLE`, 그리고 `bash -c` 안의 같은 명령을 exit 2로 막는다. 재생성되는 빌드 경로(`node_modules`, `.next`, `dist` 등)와 `/tmp` 아래 삭제는 허용한다. step 세션이 권한 확인 없이 돌기 때문에 프로젝트 안에서는 이 훅이 유일한 차단 장치다. 셸 문자열만 보므로, 이름으로 호출한 스크립트 안의 명령은 막지 못한다.
- **stop_verify.py** — 턴이 끝날 때 `package.json`이 있고 커밋되지 않은 변경이 있으면 `npm run lint`, `build`, `test`를 순서대로 실행한다. 실패하면 exit 2로 종료를 막고 출력을 Claude에게 넘긴다. 한 번 막은 뒤에는 통과시켜 루프를 피한다.

## 개발

```bash
uvx --with pytest pytest scripts/     # 또는 pytest가 설치돼 있으면 python3 -m pytest scripts/
ruff check . && ruff format --check .
```

`ruff.toml`은 사용자 전역 pre-commit 설정과 같은 규칙을 쓰고, CLI 출력(`execute.py`)과 훅의 stderr 출력만 `print` 규칙에서 제외한다.
