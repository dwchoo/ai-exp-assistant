# p27-home-probe-01 결과 — Workbench 전용 OMP 홈(C-D64) 사실 확인

- 역할: worker_senior, probe 전용. 제품 코드 변경은 없다.
- 대상: OMP `omp/18.4.5` (`~/.local/bin/omp`, Bun 단일 바이너리). 날짜: 2026-10-03.
- 방법: `strace -f`(file/network 계열 syscall)와 실행 전후 metadata inventory(path, mode, size, mtime, inode, link target)로 관찰했다. RPC는 `get_state`, `get_available_commands`, `get_login_providers`, `get_available_models`를 썼다(launcher `check_isolation`과 같은 `--mode rpc --no-session --no-title`). TUI는 PTY 120x40에서 8초 띄운 뒤 ctrl+d로 종료했다. 경로 해석은 바이너리 내 JS 문자열(`strings`)로 확인했다.
- 모든 실행에서 `HTTP(S)_PROXY`/`ALL_PROXY`를 `http://127.0.0.1:9`(닫힌 포트)로 두었다. 그래서 provider 요청과 OAuth refresh는 일어날 수 없었고, 모든 connect는 127.0.0.1:9에서 거부됐다. prompt는 보내지 않았다.
- 작업 위치: `/tmp/p27probe-641858`(wb 홈, cwd, 가짜 HOME, trace). 보고서 작성 후 삭제했다.

## 사고 기록 (constraint 관련)

- 초기 strace 설정(`trace=%desc`, `-s 512`)이 `pwrite64` 버퍼를 trace 파일에 기록했다. 그중 공유 `agent.db-wal`의 WAL header 32 B와 frame header 24 B, page 시작부(0 바이트)가 tool 출력에 한 번 표시됐다. 자격증명 payload는 보이지 않았다. header는 magic, salt, checksum 값이다.
- 조치: 모든 trace에서 read/write 계열 버퍼를 즉시 `<REDACTED>`로 치환했다. 이후 실행은 `-s 0`과 `trace=%file,%network,pwrite64,...`로 버퍼를 기록하지 않았다. trace 파일 전체는 작업 종료 시 삭제했다. 이후 Workbench 측정 도구에서 strace를 쓸 때는 `-s 0`을 필수로 한다.

## 핵심 사실 (증거)

### 경로 해석 (바이너리 코드)

| 항목 | OMP 18.4.5 동작 |
|---|---|
| agent dir | `PI_CODING_AGENT_DIR`가 있으면 그 경로를 쓴다. 단 profile(`OMP_PROFILE`/`PI_PROFILE`/`--profile`)이 있으면 무시하고 `~/.omp/profiles/<p>/agent`를 쓴다. |
| config root | `path.join(os.homedir(), PI_CONFIG_DIR \|\| ".omp")`. `path.join`이라 **절대경로를 넣으면 `$HOME/<abs>`가 된다. HOME 기준 상대경로만 유효하다.** |
| agent dir 아래 | `config.yml`/`config.yaml`(global settings), `agent.db`(auth·usage·cache), `models.db`, `history.db`, `sessions/`, `blobs/`, `memories/`, `terminal-sessions/`, `PERSONALITY.md`, `mcp.json`, `models.yml`, `WATCHDOG.*`, `.env`, `prompts/`, `skill-descriptions.db`, `last-changelog-version`, `cache/` |
| config root 아래 | `logs/`, `run/`(daemons, tiny), `cache/*.db`, `install-id`, `natives/`, `plugins/`, `stats.db`, `wt/`, `security/`, `marketplaces.json` |
| XDG_DATA/STATE/CACHE_HOME | agent dir가 기본값일 때만 쓴다. `PI_CODING_AGENT_DIR`로 바꾸면 무시된다. |
| project settings | `<cwd>/.omp/config.yml`, `<cwd>/.omp/settings.json`, `<cwd>/.claude/settings.json`가 global(`<agentDir>/config.yml`)을 덮는다. `--config`는 project 위에 적용된다. |
| setup wizard | 대화형 TUI에서 `setupVersion < CURRENT_SETUP_VERSION(=2)`이면 첫 실행 wizard가 뜬다. `startup.setupWizard`(기본 true)도 영향을 준다. 끝나면 `setupVersion: 2`를 global config.yml에 atomic rename으로 기록한다. |

### 실험 요약

| 실행 | 구성 | 결과 |
|---|---|---|
| E0 | 빈 wb 홈(symlink 없음), RPC | `No models available. Use /login …`로 즉시 종료. wb 홈에 새 `agent.db` 생성. |
| E1/E1b | wb 홈 = `agent.db` symlink만, overlay 없음, `--extension` bridge만 | 인증 인식: `get_login_providers` authenticated=`[openai-codex]`, model `openai-codex/gpt-5.5`, 8개. **ambient 누출**: `~/.agents/skills`(computer-use, orca-cli, orchestration), project `.omp/.claude/.agents` skills, project `AGENTS.md`, custom commands(green, review, annotate), autoqa on. |
| E2 | wb 홈 + config.yml(현 omp-isolation.yml 내용 + `skills.customDirectories` + include/ignored `[]`), `--config` 없음, `--no-extensions --append-system-prompt "" --no-title` | leak 0. 허용 skill `wb-probe-skill`만 보임. prompt 10435자, default prompt 유지, autoqa off. |
| E4 | E2 + project `.omp/config.yml`(`disabledProviders: []`, `dev.autoqa: true`, 다른 customDirectories) | **project config가 wb-home config.yml을 덮는다**: AGENTS.md, project skill, autoqa 누출. |
| E4b | E4 + 같은 내용을 `--config`로도 전달 | leak 0. `--config`가 project config보다 우선한다. |
| E6a | 가짜 HOME(`~/.omp/agent/{config.yml, PERSONALITY.md, AGENTS.md, APPEND_SYSTEM.md, agents/x.md}`) + wb 홈(PI_CODING_AGENT_DIR) | 사용자 config.yml, PERSONALITY.md, AGENTS.md는 **반영 안 됨**. 단 `~/.omp/agent/agents/*.md` task agent는 **반영됨**(user agents dir가 `$HOME/<PI_CONFIG_DIR>/agent/agents`로 고정). |
| E6b | 대조군: 가짜 HOME을 기본 agent dir로 사용 | 위 파일 전부 반영(누출 탐지 수단 검증). |
| E6c | E6a에서 `--append-system-prompt` 생략 | `~/.omp/agent/APPEND_SYSTEM.md`가 **반영됨**. 이 경로도 config-root 기준으로 고정돼 있다. |
| E7 | TUI 8초, wb 홈 + config.yml(setupVersion 없음) + `--config` 2개 | **첫 실행 wizard** "Setup step 1 of 5 / Sign in to your providers"(Codex는 "● logged in"으로 표시). 종료 시 OMP가 wb 홈 config.yml을 `setupVersion: 2`로 다시 썼다(주석 제거, rename). |
| E7b | E7과 같되 setupVersion: 2 사전 기록 | wizard·login 화면 없음, GPT-5.5/openai-codex 상태줄, ctrl+d 정상 종료, config.yml 불변(sha1 동일). |
| E8 | E2 구성 + `PI_CONFIG_DIR=<HOME 기준 상대경로>/wbroot`, agent dir = `wbroot/agent` | leak 0, 인증 인식. logs, run, cache, install-id가 wbroot로 이동. user agents dir, TITLE/APPEND_SYSTEM user 경로, lsp user 경로도 `wbroot/agent`로 이동. |
| E9a | symlink target 디렉터리는 있고 `agent.db` 없음(신규 사용자 모사) | **OMP(SQLite)가 target 위치에 새 `agent.db`(+shm/wal)를 만든다**. 즉 사용자 `~/.omp/agent`에 기록한다. 이후 "No models available"로 종료. |
| E9b | target 디렉터리 없음 | `SQLiteError: Database ".../agent.db": unable to open database file`로 종료. |
| E10 | 권장 구성 TUI 8초(PI_CONFIG_DIR + config.yml(setup 키 + overlay) + `--config` 2개) | wizard 없음. HOME inventory diff는 `~/.omp/agent/agent.db*`의 mtime/size만 바뀜(303104→307200, inode 동일). `~/.omp` 아래 신규·삭제 파일 0개. |
| E11 | E10 RPC + `BUN_RUNTIME_TRANSPILER_CACHE_PATH=<wbroot>/bun-transpiler-cache` | leak 0, 인증 인식. bridge.ts transpile cache(`.pile`)가 `~/.bun/install/cache/@t@` 대신 wbroot로 이동. |
| E12 | 최소 config.yml(setup 키, `disabledProviders`, `skills.customDirectories`, `dev.autoqa: false`), `--config` 없음 | leak 0. prompt와 tool 목록이 E2(전체 overlay)와 byte 단위로 같다(10435자). |

## Q1 — 시작 시 읽기·쓰기 경로

**PI_CODING_AGENT_DIR(wb agent dir) 아래 생성되는 것**: `models.db`(+shm/wal, 번들 catalog로 약 7 MB), `skill-descriptions.db`, `history.db`, `sessions/`, `terminal-sessions/`, `cache/`, `last-changelog-version`. config.yml은 setup 완료 시 OMP가 rename으로 다시 쓴다.

**읽기만 하는 것**: `config.yml/.yaml`, `settings.json`, `PERSONALITY.md`, `WATCHDOG.{md,yml,yaml}`, `.env`, `mcp.json`, `models.{yml,yaml,json}`, `prompts/`, `lsp.*`.

**PI_CONFIG_DIR가 없을 때 `~/.omp`에 쓰는 것**(E1, E2, E7):
- `logs/omp.<date>.<pid>.log`, `logs/.omp.<pid>-audit.json`
- 다른(종료된) pid의 audit json 삭제. 예: `.omp.631384/638719/638737-audit.json`
- `run/daemons/<hash>/clients/<pid>-<uuid>.json`
- `cache/legacy-pi-extension-cache.db*`
- `natives/18.4.5` utimensat
- `install-id` 읽기

**PI_CONFIG_DIR가 있을 때**: 위 항목이 전부 wb root로 간다. 남는 것은 `~/.omp/natives/18.4.5`의 **읽기(mmap) + 디렉터리 utimensat(mtime touch) 1회**뿐이다. native loader는 `~/.omp/natives`를 직접 쓴다.

**권장 구성(E10/E11)에서도 HOME에서 읽는 것**(읽기만, 쓰기 없음):
- `~/.claude/settings.json`, `~/.claude/plugins/installed_plugins.json`과 plugin cache의 `marketplace.json`, `lsp.*`. claude plugin LSP config 탐색이며, disabledProviders와 관계없이 읽는다.
- `~/lsp.*`, `~/.lsp.*`
- `~/.env`(dotenv), `~/.aws/credentials`·`~/.config/gcloud/application_default_credentials.json`(provider credential 탐색, 현재 없음)
- `~/.config/tmux`, `~/.tmux.conf`, `~/.npmrc`, `~/.bunfig.toml`, `~/.idapro/ida-config.json`
- cwd에서 `/`까지의 조상: `.git`, `.jj`, `.{omp,claude,codex,gemini}/agents`, `.omp/WATCHDOG.*`, `lsp.*`
- cwd: `.omp/config.yml`, `.omp/settings.json`, `.claude/settings.json`, `.{omp,claude,codex,gemini}/TITLE_SYSTEM.md`, `.env`, 각종 project marker

`~/.agents/skills`, `~/.claude/skills`, `~/.codex`, `~/.gemini`는 isolation 설정이 있으면 읽지 않는다(E2/E8 trace). `~/.config`에는 쓰지 않는다.

**Bun transpile cache**: `~/.bun/install/cache/@t@/*.pile`에 rename으로 기록된다. `BUN_RUNTIME_TRANSPILER_CACHE_PATH`로 옮길 수 있다(E11).

## Q2 — agent.db symlink

- **인증 인식: 된다.** symlink가 있으면 `get_login_providers`의 `openai-codex`가 authenticated=true다. `get_available_models`는 8개(openai-codex)이고 TUI에는 login prompt가 없다(E7b, E10). symlink가 없으면 "No models available"로 종료한다(E0). 판단은 비밀이 아닌 신호만으로 했다.
- **-wal/-shm 위치**: SQLite가 `readlink`로 symlink를 해석한 뒤 target 경로를 `O_NOFOLLOW`로 연다. 그래서 `-wal`, `-shm`, `-journal`은 전부 `~/.omp/agent/` 옆에 생기고, wb 홈에는 `agent.db-*`가 생기지 않는다(모든 실행 확인).
- **symlink 보존: 된다.** 시작, 종료, TUI 정상 종료, SIGTERM 종료 모두 `lstat` 결과가 S_IFLNK이고 target과 inode가 그대로였다. agent.db에 대한 rename, unlink, 재생성은 관찰되지 않았다. 단, OMP가 `chmod("<wb>/agent.db", 0600)`을 호출하는데 이 호출은 symlink를 따라가 **사용자 agent.db 권한을 0600으로 설정한다**. 이미 0600이라 실제 변화는 없었다.
- **사용자 agent.db 변화**: 기동마다 쓰기가 일어난다. 예: E10은 main db `pwrite64` 12회, wal 51회, checkpoint 후 ftruncate. 크기는 4 KiB page 단위로 증가했고(299008→303104→307200), inode는 그대로였다. 같은 시간대에 사용자의 wb-urux-sandbox OMP 2개도 같은 db를 쓰고 있었으므로 크기 증가 전체가 probe 때문이라고 단정할 수는 없다.
- **쓰기 내용의 근거**: 내용은 읽지 않았고, 바이너리 SQL 문으로만 판단했다. 기동 시 `auth_schema_version`, `auth_change_revision`(+trigger), `cache`, `usage_history`, `clients`/`client_usage`(install_id 기준), `auth_credential_blocks` 만료 정리, `auth_credential_refresh_leases`를 다룬다. 사용자 omp 기동 때와 같은 정상 동작이다. 네트워크가 막혀 있었으므로 OAuth refresh 쓰기는 이번 관찰에 포함되지 않았다. 실제 운용에서는 refresh lease와 토큰 회전이 이 db에 기록되고, 이는 공유 설계상 의도된 동작이다.

## Q3 — overlay 없는 분리 홈의 ambient, 최소 config

- **분리 홈만으로는 격리되지 않는다**(E1). 남는 ambient: `~/.agents/skills`, project `.omp/.claude/.agents` skills, project `AGENTS.md`, `~/.claude` 계열 custom commands, autoqa on(기본값). 여기에 `~/.omp/agent/agents/*.md`와 `~/.omp/agent/APPEND_SYSTEM.md`가 추가된다. 이 둘은 PI_CONFIG_DIR가 없으면 사용자 경로가 고정이다(E6a, E6c).
- **wb-home config.yml에 overlay를 넣는 것만으로는 부족하다.** project `<cwd>/.omp/config.yml`(그리고 `.omp/settings.json`, `.claude/settings.json`)이 global을 덮기 때문이다(E4). `--config`만 project를 이긴다(E4b). 따라서 **`--config` overlay는 유지해야 한다.**
- **wb-home config.yml의 최소 내용**(E12에서 leak 0, E2와 prompt·tool 동일):
  ```yaml
  setupVersion: 2            # OMP 18.4.5 CURRENT_SETUP_VERSION
  startup:
    setupWizard: false
  disabledProviders: [native, omp-managed, skillshare, agents-md, agents, claude-md, claude-plugins, claude, cline, codex, cursor, gemini, github, mcp-json, omp-plugins, opencode, ssh-json, vscode, windsurf, agent-plugins, builtin-defaults]
  skills:
    customDirectories: ["<repo>/omp_bridge/skills"]
  dev:
    autoqa: false
  ```
  - `skills.enable*`, `memory.backend`, `memories`, `advisor`, `ttsr`, `commands.*`, `mcp.enableProjectConfig`는 이번 환경에서는 결과에 영향이 없었다. 분리 홈에서는 사용자 global 값이 들어오지 않으므로 기본값이 적용된다. 다만 project config가 이 값들을 켤 수 있으므로 **`--config` overlay에는 현재 `omp-isolation.yml` 전체를 유지하는 것을 권장한다.**
  - 기본 Personality/Tone 블록은 그대로 남는다(사용자 결정). PERSONALITY.md는 wb 홈에서만 읽는다(E6a). 따라서 사용자 `~/.omp/agent/PERSONALITY.md` 경고는 분리 홈 적용 후 사라진다.
- bridge extension은 `--no-extensions` 아래에서도 `--extension`으로 load된다. `extension_errors`는 없었다. 다만 bridge socket이 없으므로 연결은 하지 않는다.

## Q4 — 사용자 파일을 다시 읽게 만드는 설정·env

- `OMP_PROFILE`, `PI_PROFILE`, `--profile <p>`: profile이 `PI_CODING_AGENT_DIR`보다 우선한다. 이때 `~/.omp/profiles/<p>/agent`를 쓰고 그 디렉터리를 만든다. → launcher는 두 env를 반드시 제거하고, 사용자 `--omp-arg`/`WORKBENCH_OMP_ARGS`의 `--profile`은 거부하거나 경고해야 한다. 이 시나리오는 사용자 `~/.omp`에 profile 디렉터리를 만들기 때문에 실행하지 않았고 코드로만 확인했다.
- `PI_CONFIG_DIR`가 없으면 config root가 `~/.omp`이고, user agents dir와 `APPEND_SYSTEM.md`/`TITLE_SYSTEM.md`/`lsp.*`의 user 경로가 `~/.omp/agent/...`로 남는다(E6a, E6c, E2 trace).
- `PI_CODING_AGENT_DIR`를 설정하면 `XDG_DATA_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME`은 무시된다. `XDG_CONFIG_HOME`은 gh(`hosts.yml`)·lspmux 같은 도구 경로에만 쓰인다.
- 경로 개별 override env: `OMP_WORKTREE_DIR`, `OMP_GITHUB_CACHE_DB`, `OMP_COMMIT_CACHE_DB`, `OMP_JUDGMENT_CACHE_DB`, `OMP_AUTH_BROKER_SNAPSHOT_CACHE`. 사용자 env에 있으면 사용자 위치를 쓴다(현재 환경에는 없음). → OMP child env에서 제거를 권장한다.
- `HOME` 변경은 전부를 옮기지만 OMP bash tool 등 하위 작업의 HOME(git, ssh 등)까지 바뀌므로 **권장하지 않는다**.
- 사용자 `~/.omp/agent/config.yml`은 분리 홈에서 읽지 않는다(E6a: 가짜 사용자 config의 `autoqa: true`, `disabledProviders: []` 미반영).
- 홈 분리와 관계없이 남는 것: `~/.claude/settings.json`, claude plugin LSP config, `~/lsp.*`, `~/.env`. `lsp.enabled: false`를 쓰면 LSP 탐색을 끌 수 있지만 lsp tool도 사라지므로 사용자 결정이 필요하다.

## Q5 — 위험과 권장 동작

- **동시 사용**: OMP는 agent.db를 다중 프로세스용으로 설계했다. WAL 모드, `synchronous=NORMAL`, `auth_credential_refresh_leases`(owner, expires), `auth_change_revision` trigger, `data_version` 확인이 그 근거다. probe 동안 사용자의 sandbox Workbench OMP 2개가 같은 db를 동시에 쓰고 있었지만 오류는 없었다. 전제 조건은 로컬 FS(WAL은 NFS 불가)와 같은 사용자 계정이다.
- **logout/login 전파**: 같은 row를 공유하므로 사용자 omp에서 logout(credential 삭제)하면 wb OMP도 다음 auth 조회 때 인증을 잃는다. 반대로 wb TUI에서 `/login`하면 사용자 저장소에 기록된다(양방향). OAuth refresh 토큰 회전도 한 row에서 일어나므로 symlink 공유가 맞고, 복사는 회전과 충돌한다(C-D64 판단과 일치).
- **신규 사용자(agent.db 없음)**: symlink를 그대로 두면 OMP가 **사용자 `~/.omp/agent/agent.db`를 새로 만든다**(E9a). 디렉터리까지 없으면 SQLiteError로 종료한다(E9b). → 권장: launcher는 symlink를 만들기 전에 `~/.omp/agent/agent.db`가 같은 uid의 **일반 파일**인지 `lstat`으로 확인한다. 아니면 OMP pane을 띄우지 않고 "먼저 `omp`를 실행해 로그인하세요" 안내를 보여준다. wb가 사용자 저장소를 만들지 않는다.
- **symlink 대체 위험**: OMP가 agent.db를 rename하거나 재생성하는 경로는 관찰되지 않았다. 다만 OMP는 config.yml을 rename으로 쓰므로, 향후 버전이 agent.db도 같은 방식으로 다루면 symlink가 일반 파일로 바뀌어 인증이 갈라질 수 있다. → 시작 시와 isolation check 전후로 symlink 상태를 검증하고, wb 홈에 `agent.db-wal`/`-shm`이 생기면 실패로 본다.
- **chmod 전파**: OMP가 `<wb>/agent.db`에 0600 chmod를 호출하면 target(사용자 파일)에 적용된다. 현재는 이미 0600이라 무해하다.
- **install-id·usage**: PI_CONFIG_DIR를 쓰면 wb는 별도 install-id를 갖고, 사용자 agent.db의 `clients`/`client_usage`에 별도 client로 기록된다. 사용자 usage 통계에 wb 사용량이 별도 client로 섞인다.
- **probe가 사용자 `~/.omp`에 남긴 것**(PI_CONFIG_DIR 적용 전 E0–E7에서 OMP 자체가 생성). constraint상 `~/.omp` 내용은 건드리지 않아 그대로 두었다:
  - `~/.omp/logs/omp.2026-10-03.{644605,646321,646419,647006,648010,648116,649429,650271}.log`, `.omp.650271-audit.json`
  - `~/.omp/run/daemons/85766810a63ade05/clients/{644605,646321,646419,647006,648010,648116}-*.json`
  - OMP가 종료된 pid의 audit json 3개(631384, 638719, 638737)를 정리 동작으로 삭제했다.
  - 사용자 확인 후 위 로그와 client 파일은 삭제해도 된다.

## 권장 launcher 설계

### 디렉터리 (data dir 아래, 0700)
```
<data-dir>/omp-root/              # PI_CONFIG_DIR 대상 (HOME 기준 상대경로로 전달)
<data-dir>/omp-root/agent/        # PI_CODING_AGENT_DIR (절대경로)
<data-dir>/omp-root/agent/agent.db -> ~/.omp/agent/agent.db   # 유일한 symlink
<data-dir>/omp-root/agent/config.yml                          # Workbench가 매 시작 재생성 (0600)
<data-dir>/omp-root/bun-transpiler-cache/                     # BUN_RUNTIME_TRANSPILER_CACHE_PATH
```
- manager와 worker는 같은 root를 공유해도 된다. OMP는 다중 프로세스 설계이고, 역할별 차이는 `--config` role overlay가 담당한다.
- OMP가 config.yml을 rename으로 다시 쓰므로(setup 완료, /settings 변경) **매 시작마다 config.yml을 다시 생성한다**. config.yml은 symlink로 두지 않는다.
- `agent.db-wal`, `-shm`, `-journal`은 symlink로 만들지 않는다. SQLite가 target 기준으로 처리한다.

### OMP child env
- 설정: `PI_CODING_AGENT_DIR=<abs agent dir>`, `PI_CONFIG_DIR=<os.path.relpath(omp-root, HOME)>`(OMP에 전달하는 HOME 기준), `BUN_RUNTIME_TRANSPILER_CACHE_PATH=<omp-root>/bun-transpiler-cache`
- 제거: `OMP_PROFILE`, `PI_PROFILE`, `PI_AUTO_QA`(기존), `OMP_WORKTREE_DIR`, `OMP_GITHUB_CACHE_DB`, `OMP_COMMIT_CACHE_DB`, `OMP_JUDGMENT_CACHE_DB`, `OMP_AUTH_BROKER_SNAPSHOT_CACHE`
- host persistent shell에는 위 변수를 export하지 않는다. 사용자가 그 shell에서 `omp`를 실행하면 사용자 홈을 써야 하기 때문이다.
- OMP bash tool 하위 프로세스는 이 env를 상속하므로, worker가 tool 안에서 `omp`를 실행하면 wb root를 쓴다. 의도와 맞다.

### argv
- 현재 C-D59와 같다: `--config omp-isolation.yml --config <role overlay> --no-extensions --append-system-prompt "" --no-title [user args] --extension bridge.ts`
- `--config`는 project `.omp/config.yml`을 이기는 유일한 계층이라 필수다.
- `--append-system-prompt ""`는 project `.omp/.claude/.codex/.gemini/APPEND_SYSTEM.md` 차단에 계속 필요하다.
- 사용자 args 안의 `--profile`은 거부한다.

### role overlay 변경점
- 사용자 `omp config get disabledProviders/task.disabledAgents` union은 더 이상 필요 없다. 분리 홈이라 사용자 global이 로드되지 않는다. C-D64 "wb 독자 설정"과도 맞는다. 단 사용자가 일부러 끈 **model provider**를 wb에서도 끄고 싶은지는 사용자 결정이 필요하다.
- `task.disabledAgents`는 **nearest `<ancestor>/.omp/agents`(project)만** union한다. user agents dir는 PI_CONFIG_DIR 적용 후 `<omp-root>/agent/agents`(wb 소유, 비어 있음)가 된다. `task_agent_names(home=…)`의 user 경로도 `omp_user_dir(HOME, env)`가 PI_CONFIG_DIR를 반영하므로, child env를 그대로 넘기면 일관된다.
- `ambient_prompt_files`의 user 경로도 같은 env로 계산하면 wb root를 가리킨다. 이때 PERSONALITY 경고는 사라진다.

### config.yml 내용 (매 시작 생성)
- `setupVersion: 2`(OMP 버전별 `CURRENT_SETUP_VERSION`. evidence 버전과 다르면 drift로 보고), `startup.setupWizard: false`
- `omp-isolation.yml` 전체(defense in depth)
- `skills.customDirectories: [<repo>/omp_bridge/skills]`, `includeSkills: []`, `ignoredSkills: []`
- 위 최소형으로도 E12에서 leak 0이었다.

### 시작 전 검사 (OMP spawn 전)
1. `lstat(~/.omp/agent/agent.db)`가 같은 uid의 일반 파일인지 확인한다. 아니면 pane을 시작하지 않고 로그인 안내를 보여준다. 내용은 열지 않는다.
2. wb `agent/agent.db`가 없으면 symlink를 만든다. 있으면 S_IFLNK이고 target이 `~/.omp/agent/agent.db`인지 확인한다. 일반 파일이거나 target이 다르면 실패로 보고하고 덮어쓰지 않는다.
3. wb agent dir에 `agent.db-wal`, `-shm`, `-journal`이 있으면 실패로 본다(공유가 갈라진 신호).
4. env에 `OMP_PROFILE`, `PI_PROFILE`이 없고 argv에 `--profile`이 없는지 확인한다.

### isolation-check 기준 (기존 RPC check 확장, model 호출 0)
- 기존 leak 기준을 유지한다: `context_files`, Workbench 외 skills, rules, MCP, bundled 외 task agent, commands, `system_prompt:replaced`, `autoqa`.
- `get_login_providers` 추가: authenticated provider가 1개 이상이면 ok. 0개면 state `auth_missing`(leak과 구분, 비밀 아님).
- check 전후 `lstat` 비교: wb `agent.db` symlink, target, 사용자 agent.db inode가 같아야 하고 wb 홈에 `agent.db-*`가 없어야 한다.
- (테스트·evidence 전용) 실행 전후 `~/.omp` metadata inventory에서 `agent/agent.db*` mtime/size 외에 신규·삭제 파일 0개. 허용 예외는 `natives/<ver>` 디렉터리 mtime뿐이다.

## 미결정·후속 확인 (사용자 결정 필요)
- `~/.claude/settings.json`·claude plugin LSP config·`~/lsp.*`·`~/.env`는 홈 분리 후에도 읽힌다. 차단하려면 `lsp.enabled: false`가 필요하지만 lsp tool이 사라진다. dotenv(`~/.env`, provider API key)는 인증 공유 범주로 볼지 정해야 한다.
- wb TUI의 `/login`, `/logout`이 사용자 저장소에 양방향으로 반영되는 점을 그대로 둘지, 안내 문구만 둘지.
- 사용자 `disabledProviders`(model provider) 상속 여부.
- 실제 네트워크 환경의 OAuth refresh(lease, 회전) 동작은 provider 요청 금지 constraint 때문에 관찰하지 않았다. 적용 단계에서 사용자 승인 아래 1회 확인을 권장한다.
