# OMP Workbench — 2차 개발 PRD

## Task Inbox & Web Board

**문서 버전:** v0.1  
**작성일:** 2026-09-23  
**상태:** Draft / Deferred — 1차 Workbench 완성 후 개발  
**개발 단계:** Phase 2 / 착수 일정 미정  
**제품명:** OMP Workbench  
**이번 산출물:** 기능 요구사항과 범위만 기록. 서버·MCP·웹 UI는 구현하지 않는다.

> 외부 AI coding agent의 작업 요청을 접수·보관하고, manager가 검토와 필요한 토의를 거쳐 실행 명세로 바꾼 뒤 worker에게 전달한다. 사용자는 웹 게시판에서 요청·검토·실행·결과를 확인한다.

---

## 0. 결정 범위와 개발 순서

### 0.1 사용자 요구로 확정한 사항

- 본 기능은 **1차 Workbench가 완성된 뒤 진행할 2차 개발**이다. 1차 출시 조건에 넣지 않는다.
- 다른 AI coding agent가 manager에게 작업 요청서를 보낼 수 있어야 한다.
- 요청 내용과 관련 정보를 게시판처럼 보관한다.
- Manager가 요청을 검토하고, 불명확하거나 현재 상황과 맞지 않으면 사용자와 토의한다.
- 검토한 요청은 worker에게 전달할 작업 명세서로 구체화한다.
- 사용자가 실제 브라우저로 접속해 남은 작업과 현재 작업을 확인할 수 있는 웹 서버를 제공한다.
- Manager/worker 간 전달에는 mailbox를 활용한다. 요구사항에 맞는 기존 기능이 있으면 우선 재사용한다.

### 0.2 이 문서가 제안하는 기본안

| 항목 | 기본안 |
|---|---|
| 외부 agent 접점 | MCP tools. 공유 HTTP 서비스의 `/mcp` endpoint 사용 |
| 사용자 접점 | 같은 서비스가 제공하는 웹 목록·상세 화면 |
| 저장 | 프로젝트별 요청 저장소 하나. SQLite를 우선 검토 |
| 전달 | 저장 완료 후 1차 Workbench mailbox에 요청 ID 중심의 알림 |
| 판단·실행 | Manager의 검토·명세 확정과 worker의 실행을 분리 |
| 운영 범위 | 한 프로젝트, manager 1개, worker 1개, host terminal 1개 |
| 네트워크 | Localhost 기본. 원격 직접 공개는 별도 선택과 보안 검증 후 |
| 재시작 | 요청은 보존하되, 불명확한 실행·전달을 자동 재시도하지 않음 |

위 기술 선택은 사용자 요구를 구현하기 위한 제안이다. 라이브러리, 상세 API signature, 인증 방식, 수치 제한은 2차 착수 시 결정한다. 이 문서는 구현 완료나 호환성 검증 완료를 뜻하지 않는다.

### 0.3 선행 1차 기준선

1차 완료 기준은 후속 대화에서 합의한 **자체 wrapper + manager OMP + worker OMP + host persistent terminal + 내부 mailbox**를 전제로 한다. 기존 PRD v0.2의 단일 OMP 구성 그대로를 뜻하지 않는다.

1차에서는 두 역할의 사용자 대화, host 환경에서의 실행, shell lifecycle 통지, worker의 예산 내 로그 분석, manager/worker의 명세·질문·답변·보고 전달을 먼저 완성한다. 주기적 모델 분석은 일반 코드의 상태 관측과 달리 토큰을 사용한다.

**본 문서는 1차 PRD 전체의 개정본이 아니라, 이후 합의한 기준선에 붙이는 2차 기능 문서다.** 기존 PRD에서 외부 ingress/MCP adapter를 1차 범위 밖에 둔 결정은 유지한다. [B1]

---

## 1. 제품 가치와 핵심 원칙

외부 요청을 manager의 대화창에 그대로 밀어 넣는 대신, 검토 가능한 요청함으로 만든다. 요청자가 연결을 끊어도 내용과 질문·답변이 남고, manager가 바쁘더라도 나중에 검토할 수 있도록 한다.

가장 중요한 경계는 다음과 같다.

> **접수됨 ≠ 수락됨 ≠ 실행 중 ≠ 목표 달성.**

외부 요청서는 원하는 일을 설명하는 자료다. Worker에게 실행 권한을 부여하는 명세가 아니다. 승인된 작업 명세는 manager가 별도로 작성·확정하며, 실행 상태는 실제 Workbench 보고에 근거한다.

웹 UI는 OMP TUI나 terminal을 대체하지 않는다. 복잡한 토의와 개발은 manager 화면에서, 실험 운영은 worker와 host terminal에서 계속 수행한다.

---

## 2. 범위

### 포함

| 기능 | 2차 최소 범위 |
|---|---|
| 요청 접수 | 제목, 본문, 배경·제약·기대 결과·참조 정보 저장 |
| 요청 조회 | 상태별 목록, 상세 내용, 접수·수정 시각, 요청 출처 |
| 토의 기록 | Manager 질문, 사용자·요청 agent의 추가 답변, 검토 메모 |
| 명세 연결 | 원본 요청과 승인된 task ID / 명세 revision 연결 |
| 내부 전달 | Manager에게 새 요청 알림, worker에게 승인 명세 전달 |
| 실행 추적 | 연결된 작업·command ID, 최근 관측 상태, 결과 요약 |
| 웹 화면 | 목록·상세·상태 필터·댓글, 현재 연결 상태 표시 |
| 결과 회수 | 요청 agent가 MCP로 진행 상태·질문·최종 결과 조회 |
| 저장·중복 방지 | 재시작 후 요청 보존, 같은 접수 재시도의 중복 억제 |

게시판이 책임지는 대상은 접수한 요청과 그에 연결된 명세·실행이다. 모든 OMP 대화나 과거 실험을 자동으로 ticket화하는 기능은 포함하지 않는다.

### 제외

범용 scheduler, 동적 worker 생성, 여러 host의 실험 배치, 임의 shell 실행 API, 원격 terminal 조작, 범용 chatbot 웹 UI, 전체 대화 history 동기화, 다중 조직용 SaaS, 파일 첨부·자동 다운로드, 외부 agent로의 callback/webhook delivery는 최소 범위에서 제외한다.

웹 화면의 drag-and-drop 상태 변경, 우선순위 자동 계산, 의존성 DAG, 자동 merge, 무제한 요청 자동 처리도 만들지 않는다. 알림을 받기 위해 외부 agent가 장시간 MCP 호출을 유지해야 하는 구조는 사용하지 않는다.

---

## 3. 권장 구성

```text
외부 AI coding agent                    사용자 브라우저
        │                                     │
        │ MCP tools                           │ 목록 / 상세 / 댓글
        ▼                                     ▼
┌─────────────────────────────────────────────────────────┐
│ 선택적 Workbench Board 서비스                           │
│ /mcp + Web UI + 요청 처리 로직 + 단일 요청 저장소         │
└───────────────────────┬─────────────────────────────────┘
                        │ 저장 후 request.available 알림
                        ▼
               1차 Workbench mailbox / bridge
                        │
                        ▼
                   Manager OMP
            검토 · 사용자 토의 · 필요한 개발
                        │ 승인된 명세
                        ▼
                   Worker OMP
                        │
                        ▼
               Host persistent terminal
                        │ 결과 / 관측
                        └── Worker → Manager → 게시판 결과
```

Board 서비스는 선택적 부속 프로세스로 두는 것을 기본안으로 한다. OMP의 LLM turn이 웹 요청을 직접 처리하지 않으며, Board가 PTY·화면 상태를 소유하지도 않는다. 서버가 켜져 있으면 manager가 offline이어도 접수·조회는 가능해야 한다. OS 서비스 설치나 자동 시작은 최소 요구사항이 아니다.

**서버 하나, 요청 저장소 하나, 내부 연결 adapter 하나**로 시작한다. 별도 MCP DB, 웹 DB, 메시지 broker를 각각 만들지 않는다. 웹 서버가 없어도 1차 Workbench는 실행되어야 한다.

---

## 4. MCP와 웹 UI의 역할

MCP는 외부 agent가 구조화된 도구를 호출하는 인터페이스다. 사람이 사용하는 게시판 화면이나 업무 상태 관리 기능을 자동으로 제공하는 것은 아니다. 이 제품에서 접수·조회 도구와 웹 UI를 같은 요청 처리 로직 위에 구현한다. MCP tools의 schema 및 구조화된 응답을 활용한다. [S1]

여러 client와 웹 UI가 공유하는 서비스이므로 **Streamable HTTP**를 기본 후보로 둔다. 해당 MCP transport는 HTTP 기반의 client-server 통신을 정의한다. 이 문서는 2025-11-25 사양을 참고했으며, 2차 개발 시 지원할 protocol/SDK 버전을 다시 고정한다. [S2]

예상 경로는 다음과 같다. 경로 이름은 설계안이다.

```text
/mcp                    외부 agent용 MCP endpoint
/requests               사용자용 요청 목록
/requests/<request_id>  요청 상세 / 질문·답변 / 연결 명세 / 결과
```

일반적인 JSON REST endpoint만 제공하고 이를 MCP라고 부르지 않는다. 선택한 MCP SDK로 초기화, capability, tools 호출, 인증과 protocol 동작을 검증한다. Stdio 전용 client용 adapter는 실제 필요가 확인된 뒤 추가한다.

---

## 5. Mailbox 사용 원칙

### 5.1 게시판과 mailbox의 책임 분리

| 구성 | 책임 |
|---|---|
| 요청 저장소 | 원문, 질문·답변, 검토 상태, 승인 명세 참조, 결과의 기준 기록 |
| Mailbox | 검토할 요청이나 답변이 생겼다는 통지와 내부 agent 간 전달 |
| OMP 세션 | 현재 검토·토의·실행 판단의 맥락 |

요청 본문을 mailbox와 OMP history에 계속 복제하지 않는다. 통지는 요청 ID와 변경 종류를 중심으로 보내고, manager가 필요할 때 상세 내용을 읽는다.

예시 이벤트는 **Workbench에서 제안하는 내부 형식**이며, 기존 OMP API 이름이 아니다.

```json
{
  "type": "request.available",
  "request_id": "req-0042",
  "change": "created",
  "recipient": "manager"
}
```

### 5.2 기존 기능 재사용 경계

1차에서 선택·검증한 mailbox를 그대로 재사용한다. 검토한 OMP `hub` 문서는 peer messaging을 process-global bus로 설명하므로, 독립 OMP 프로세스 두 개가 자동으로 연결된다고 가정하지 않는다. [S4]

공개 기능이 독립 세션 연결과 필요한 전달 의미를 충족하면 재사용한다. 그렇지 않으면 1차에서 확보한 wrapper-local bridge를 사용한다. 2차 기능 때문에 OMP private mailbox 구현을 import하거나 다른 전달 체계를 새로 만들지 않는다.

### 5.3 알림 정책

요청을 저장한 뒤 알림을 시도한다. 알림 실패로 저장 성공을 실패로 뒤집지 않는다. Manager가 busy/approval/offline이면 검토 대기로 남긴다. 자동 검토가 꺼져 있을 때는 기록과 미검토 표시만 갱신한다.

여러 새 요청·댓글이 짧은 시간에 도착하면 일반 코드가 통지를 합친다. 웹 조회, MCP 조회, 상태 확인을 위해 LLM을 호출하지 않는다. 실제 검토에만 manager 모델을 사용하며, 자동 검토 예산은 1차의 자동화 제어를 따른다.

재연결 시 저장소에서 미검토 항목을 찾을 수 있어야 한다. 이것을 이미 승인·전달된 실험의 자동 재실행으로 연결하지 않는다.

---

## 6. 주요 사용자 흐름

### Flow A — 일반 요청

1. 외부 agent가 제목·본문·배경·제약·기대 결과를 MCP로 제출한다.
2. 서버는 유효성·권한·중복을 확인한 뒤 요청을 저장하고 `request_id`를 즉시 반환한다.
3. Manager는 mailbox 통지를 통해 검토 필요를 알게 된다.
4. Manager가 현재 코드·자원·사용자 의도와의 적합성을 확인한다.
5. 실행 가능한 요청이면 승인된 작업 명세를 만들고 worker에게 전달한다.
6. Worker는 1차 실행 규칙에 따라 같은 host terminal에서 작업하고 결과를 보고한다.
7. Manager가 완료 기준 충족 여부를 판단하고 게시판에 결과를 남긴다.
8. 사용자와 요청 agent는 같은 요청의 상태·결과를 조회한다.

**Submit 호출은 실험 완료까지 기다리지 않는다.** 응답은 접수 사실이지 성공 약속이 아니다.

### Flow B — 불명확하거나 부적절한 요청

Manager는 `NEEDS_INFO`와 질문을 기록하고, 사용자와 manager 화면에서 토의하거나 요청 agent의 추가 답변을 받는다. 답변은 해당 요청의 댓글로 보관한다. 정보가 부족한 상태에서 worker에게 추측한 명세를 보내지 않는다.

수행하지 않기로 결정하면 이유와 함께 요청을 닫는다. 외부 agent가 재촉하거나 우선순위를 높여 달라고 해도 승인 권한이 생기지 않는다.

### Flow C — Worker가 이미 실행 중

새 요청 접수와 manager 검토는 가능하다. 승인된 명세는 `READY`로 보관하고, manager가 다음 작업을 명시적으로 선택한다. Board는 현재 실험을 중단하거나 terminal에 명령을 투입하지 않는다. 대기 목록은 scheduler가 아니다.

### Flow D — 요청 내용이 뒤늦게 변경됨

외부 agent의 추가 설명은 댓글로 추가한다. 승인된 명세를 몰래 덮어쓰지 않는다. 실행에 영향을 주는 변경은 manager가 새 명세 revision으로 검토하며, 실행 중 작업을 그대로 변경하지 않는다.

---

## 7. 요청 데이터와 식별자

### 7.1 최소 저장 정보

| 필드 | 의미 |
|---|---|
| `request_id` | Board가 발급한 영속 업무 요청 ID |
| `requester_id` | 인증된 client identity에서 결정한 요청자 |
| `client_request_id` | 접수 재시도의 중복 방지 키 |
| `title`, `description` | 원본 제목과 요청 본문 |
| `context`, `constraints`, `expected_outcome` | 선택적 배경·제약·기대 결과 |
| `references` | 참조 문자열·링크. 자동 다운로드하지 않음 |
| `status`, `outcome` | 검토·실행 상태와 종료 사유 |
| `created_at`, `updated_at` | 생성·갱신 시각 |
| `comments` | 작성자·시각이 있는 질문, 답변, 검토 기록 |
| `task_id`, `spec_revision` | 연결된 승인 명세 |
| `execution_refs` | 기존 Workbench run / command ID 참조 |
| `result_summary`, `result_refs` | 외부에 제공 가능한 결과 요약과 근거 참조 |
| `last_observed_at` | 실행 상태를 마지막으로 관측한 시각 |

초기에는 제목·본문·중복 방지 키만 제출 필수값으로 삼는 것을 제안한다. 명확한 완료 기준이 없어도 요청은 받을 수 있으며, 그 불명확함을 manager가 검토한다. 프로젝트·허용 workspace는 서버 설정에서 고정하고 외부 입력의 임의 경로를 실행 대상으로 사용하지 않는다.

### 7.2 ID의 의미 분리

`request_id`는 MCP 연결의 session ID나 JSON-RPC request ID가 아니다. Client 재연결 후에도 같은 업무 요청을 조회할 수 있어야 한다. 업무 요청 하나를 승인된 명세 하나에 연결하는 것부터 시작하며, 범용 parent/child ticket 구조는 만들지 않는다.

접수 중복은 `(requester_id, client_request_id)`로 억제한다. 같은 키와 같은 내용이면 기존 요청을 반환하고, 내용이 다르면 충돌로 응답한다. 댓글도 client 측 재시도 키를 사용할 수 있도록 한다.

---

## 8. 외부 MCP tools — 제안 인터페이스

아래 이름은 새로 만들 도구의 설계안이다. 이미 존재하는 OMP/MCP 도구라고 주장하지 않는다.

| Tool | 기능 |
|---|---|
| `workbench_request_submit` | 요청 접수 후 ID와 현재 접수 상태 반환 |
| `workbench_request_list` | 조회 권한이 있는 요청 목록·상태 조회 |
| `workbench_request_get` | 요청 내용·질문·검토 상태·공개 가능한 결과 조회 |
| `workbench_request_comment` | 해당 요청에 보완 설명·답변·변경 요청 추가 |

외부 호출자에게 `approve`, `dispatch`, `terminal_run`, `terminal_interrupt`, worker 직접 메시징 도구를 노출하지 않는다. Tool 이름을 숨기는 것에 그치지 않고 서버에서 쓰기 권한을 검사한다.

접수 응답 예시:

```json
{
  "request_id": "req-0042",
  "status": "NEW",
  "board_path": "/requests/req-0042",
  "message": "요청이 접수되었습니다. Manager 검토 전이며 실행은 승인되지 않았습니다."
}
```

목록 조회는 제한된 페이지 크기와 pagination을 사용한다. 결과 확인은 `get`으로 시작하며, 자동 callback이나 업무 완료를 기다리는 장시간 MCP 호출은 필요조건이 아니다. MCP 자체의 비동기 task 확장 도입도 이 제품의 요청 ID 모델에 필수는 아니다.

---

## 9. 상태 모델

업무 상태는 여섯 개로 시작한다.

| 상태 | 의미 |
|---|---|
| `NEW` | 저장되었고 아직 검토 전 |
| `REVIEWING` | Manager가 검토 중 |
| `NEEDS_INFO` | 사용자·요청 agent의 답변이나 결정을 기다림 |
| `READY` | 명세가 승인되었고 실행 대기 중 |
| `RUNNING` | 승인된 작업 수행이 시작됨 |
| `CLOSED` | 완료 판단 또는 수행하지 않기로 한 결정이 기록됨 |

기본 흐름:

```text
NEW → REVIEWING ↔ NEEDS_INFO
             ├─ READY → RUNNING → CLOSED
             └─ CLOSED
```

`CLOSED`에는 `success`, `failed`, `rejected`, `cancelled` 중 사유와 설명을 기록한다. 부분 결과가 있으면 요약에 명시하고 완료 기준을 충족하지 않았는데 success로 표시하지 않는다.

메일 수신 ACK만으로 `RUNNING`으로 바꾸지 않는다. 시작 보고에 근거해 갱신한다. 또한 command의 exit code 0만으로 요청 전체를 `CLOSED(success)`로 바꾸지 않는다. Manager가 명세의 완료 기준으로 판단한다.

연결 끊김은 업무 실패와 다르다. 마지막 상태와 `last_observed_at`, 별도 offline/확인 필요 표시를 함께 보여준다. 실제 완료 여부를 추정해 상태를 바꾸지 않는다.

---

## 10. 검토, 명세, 실행 권한

**외부 요청 원문과 내부 승인 명세를 별도로 보관**한다. Manager는 현재 목표, 코드 상태, host 환경, 사용 가능한 자원, 기존 실험, 비용·중단 조건을 검토하고 필요하면 사용자와 토의한다.

사용자가 이미 위임한 범위 안에서는 manager가 검토 후 명세를 확정할 수 있다. 새로운 권한, 큰 비용, 공유 환경 변경, 파괴적인 작업 등 위임 범위를 벗어나는 결정은 사용자 확인을 받는다. 외부 agent의 요청 문장은 사용자 승인으로 취급하지 않는다.

명세는 1차에서 사용하는 task ID·revision·실행 조건·완료 기준을 재사용한다. Board가 별도의 명세 포맷이나 worker 실행 규칙을 만들지 않는다. Manager는 실행할 명세를 명시적으로 선택하며, worker가 busy인 동안 요청 도착 순서만으로 자동 선점하지 않는다.

동일 요청·명세의 반복 전달이 새 실험을 중복 실행시키지 않도록 1차의 task 식별 규칙과 연결한다. 장애 후 전달·시작 여부가 불명확하면 확인 필요로 남기고 자동 재전송하지 않는다. Exactly-once 실행은 이 기능의 보장이 아니다.

취소 요청은 즉시 process kill 명령으로 바꾸지 않는다. 실행 전이면 권한 있는 결정으로 닫을 수 있다. 실행 중이면 1차 interrupt 경로와 종료 확인을 거친 뒤 상태를 갱신한다.

---

## 11. 웹 게시판 요구사항

### 목록

요청 ID, 제목, 출처, 상태, 접수 시각, 최근 변경, 연결 작업을 표시한다. 기본 필터는 미검토·정보 대기·실행 대기·실행 중·종료이며, 간단한 제목 검색을 제공한다.

대기 요청이 몇 개이고 무엇이 막혀 있는지 확인할 수 있어야 한다. Manager/worker 연결 상태와 마지막 동기화 시각을 보여주되, 연결이 끊겼을 때 정지된 화면을 최신 상태인 것처럼 표시하지 않는다.

### 상세

원본 요청, 질문·답변, 검토 결과, 승인 명세 참조, 연결된 실험, 결과 요약, 시간 순서의 변경 기록을 보여준다. 사용자는 보완 의견을 댓글로 남길 수 있다. 복잡한 토의는 기존 manager TUI에서 진행하고 중요한 결론을 게시판에 기록한다.

초기 UI는 조회·댓글에 집중한다. 임의 상태 편집, 원격 shell, 한 번의 클릭으로 검토를 우회하는 실행 버튼은 제공하지 않는다. 짧은 간격의 일반 HTTP 갱신이나 수동 새로고침으로 시작할 수 있으며, WebSocket은 필수 요구사항이 아니다. UI 갱신은 모델 호출을 발생시키지 않는다.

Terminal raw log, 환경변수, 비밀 정보가 웹에 자동 공개되지 않도록 한다. 결과 참조는 서버가 허용한 artifact만 노출하고, 임의 host 경로를 읽거나 내려받는 API로 확장하지 않는다.

---

## 12. 보안과 신뢰 경계

단순한 로컬 도구로 시작하되, agent 입력을 host 실행으로 이어 붙일 수 있으므로 접수 권한과 실행 권한을 분리한다.

| 경계 | 기본 정책 |
|---|---|
| 네트워크 | Localhost 기본. 인증 없는 전체 interface 공개 금지 |
| 호출자 식별 | 본문의 `source` 문자열이 아니라 인증된 identity 기준 |
| 외부 agent | 자기 요청 제출·조회·댓글이 기본. 다른 요청은 명시적 공유 권한 필요 |
| 사용자·manager | 해당 프로젝트의 검토·결정 권한. 외부 client credential과 분리 |
| 외부 본문 | 검토 자료로 취급. 시스템 지시·실행 승인으로 승격하지 않음 |
| 입력·웹 표시 | Schema·크기 제한, Markdown/HTML 안전 렌더링, script 실행 방지 |
| 브라우저 쓰기 | 인증 및 CSRF 방어. 허용 Origin/Host와 CORS 정책 명시 |
| 참조 링크·경로 | 자동 다운로드·shell interpolation·임의 host 파일 조회 금지 |
| 과도한 요청 | 크기·빈도 제한과 통지 합치기. 요청마다 manager를 무제한 호출하지 않음 |

MCP Streamable HTTP 사양은 Origin 검증, 로컬 binding, 인증에 관한 보안 요구·권고를 둔다. 이를 선택한 SDK와 함께 검증한다. [S2] HTTP 인증을 구현할 때는 MCP authorization 사양과 client 지원 범위를 확인하며, 임의 토큰 방식이 모든 client에 호환된다고 단정하지 않는다. [S3]

원격 직접 접속이 필요하면 HTTPS, 인증·권한 검증, 배포 경계를 2차 착수 시 별도 확정한다. 기본 로컬 범위를 조용히 인터넷 공개 서버로 바꾸지 않는다. 복잡한 조직별 RBAC는 요구하지 않지만, 실제 노출 범위에 필요한 보호는 생략하지 않는다.

---

## 13. 저장과 장애 처리

요청·댓글·상태 변경을 하나의 DB에 보관한다. 승인된 명세와 결과는 기존 artifact의 안정된 참조 또는 허용된 사본으로 연결한다. Mailbox만을 영속 요청 저장소로 삼지 않는다.

| 상황 | 기대 동작 |
|---|---|
| Manager offline | 서버가 살아 있으면 접수·조회 지속. 검토 대기 표시 |
| DB 저장 실패 | 접수 성공으로 응답하지 않음 |
| 저장 성공 후 알림 실패 | 요청 유지. 미검토 표시와 재연결 후 조회 가능 |
| Board 서비스 종료 | 1차 OMP·terminal 작업을 종료시키지 않음 |
| Board 재시작 | 요청·댓글·검토 결과 보존. 실험 자동 재실행 없음 |
| Workbench 연결 상실 | 마지막 관측 시각과 확인 필요 표시. 실행 실패로 단정하지 않음 |
| 최종 결과 동기화 실패 | 실행과 별도로 동기화 지연 표시. 기존 결과 참조로 재확인 |

이 문서는 서버·host 장애에도 진행 중 프로세스를 복구한다고 보장하지 않는다. 수동 확인으로 상태를 맞출 수 있는 최소 기능을 제공하되, 분산 transaction이나 자동 복구 scheduler로 확장하지 않는다.

---

## 14. Acceptance Criteria

아래는 2차 구현의 검증 조건이다. 현재 통과했다는 의미가 아니다.

| ID | 조건 |
|---|---|
| P2-AC-01 | Board/MCP 기능을 실행하지 않아도 1차 Workbench가 정상 사용된다. |
| P2-AC-02 | 외부 agent가 MCP로 제출하면 저장된 요청 ID를 받고, 실험 종료까지 호출이 대기하지 않는다. |
| P2-AC-03 | 같은 요청자가 같은 키·내용으로 재시도하면 ticket이 중복 생성되지 않는다. 같은 키의 다른 내용은 충돌로 처리한다. |
| P2-AC-04 | Manager가 busy/offline이어도 서버가 살아 있으면 접수되고, 웹에서 검토 대기로 조회된다. |
| P2-AC-05 | 웹과 MCP가 같은 요청·댓글·상태를 보여주며, 단순 조회·새로고침은 모델 호출을 발생시키지 않는다. |
| P2-AC-06 | Manager 질문 → 요청 agent 또는 사용자 답변 → 재검토가 하나의 요청 기록으로 이어진다. |
| P2-AC-07 | 검토 전 원문이나 외부 client 호출만으로 worker 전달·terminal 실행·승인 상태 변경이 일어나지 않는다. |
| P2-AC-08 | 승인 명세에 request/task/revision 연결이 남고, 전달 ACK와 실제 작업 시작을 구분한다. |
| P2-AC-09 | 연결된 command가 성공해도 목표 충족 검토 전에는 요청 전체가 자동 성공 처리되지 않는다. |
| P2-AC-10 | Worker가 busy이면 새 요청은 대기하며, 현재 terminal을 선점하거나 기존 실험을 자동 중단하지 않는다. |
| P2-AC-11 | 요청 도착 알림이 합쳐져도 저장된 요청은 모두 조회된다. 불명확한 재전송으로 실험을 중복 실행하지 않는다. |
| P2-AC-12 | 재시작 후 기록이 보존되고, 연결 끊김·동기화 지연이 최신 정상 상태와 구분되어 표시된다. |
| P2-AC-13 | 요청자 권한을 넘는 조회·승인·worker 직접 호출이 차단되고, 외부 본문의 script가 웹에서 실행되지 않는다. |
| P2-AC-14 | 결과를 MCP로 회수할 수 있으면서 host 비밀 정보·임의 경로의 파일을 열람할 수는 없다. |

---

## 15. 로드맵 기록

| 단계 | 범위 | 완료·착수 조건 |
|---|---|---|
| **1차 — Core Workbench** | 자체 wrapper, manager/worker 두 OMP, host terminal, lifecycle, 내부 mailbox, 역할·분석 예산 | 이 경로를 먼저 완성하고 실제 실험으로 검증 |
| **2차 — Task Inbox & Web Board** | 외부 MCP 접수, 요청 저장, manager 검토, 승인 명세 전달, 웹 조회, 결과 회수 | **1차 완료 후 착수. 현재는 PRD만 유지** |
| 추후 별도 검토 | 원격 공개 운영, callback, 다중 worker, 고급 scheduling | 자동 포함하지 않고 필요를 확인한 뒤 새 범위 결정 |

2차 착수 시에는 지원 MCP client·SDK·인증 방식, 로컬/원격 접속 범위, 1차 mailbox adapter, 저장 위치·retention, 요청 크기·검토 예산을 확정한다. 이 선택 때문에 1차를 지연시키지 않는다.

**1차 문서에 추가할 roadmap 문구:**

> Phase 2 / Deferred: 외부 AI coding agent의 작업 요청을 MCP로 접수하고 영속 요청함에 보관한다. Manager가 요청을 검토하고 필요한 토의를 거쳐 승인 명세를 worker mailbox로 전달한다. 웹 게시판에서 요청·검토·실행 상태·결과를 조회한다. 1차 Workbench 완료 전에는 구현하지 않으며, 1차 release의 acceptance criteria에 포함하지 않는다.

---

## 16. 근거와 검증 범위

**[B1] 기존 PRD v0.2 및 후속 사용자 요구**  
기존 문서의 §0.2·§5는 외부 ingress/MCP adapter를 1차 범위 밖에 두고, §8은 host persistent shell을, §16은 공개 Extension API와 bridge 경계를 정의한다. Manager/worker 두 역할, mailbox 선호, 이번 요청함의 2차 배치는 이후 대화에서 추가된 요구다. 기존 문서가 이미 이번 기능을 포함한다고 해석하지 않는다.

**[S1] MCP Tools — 2025-11-25 사양**  
도구 schema, 호출, 구조화된 결과를 참고했다. 도구 이름과 게시판 동작은 본 문서의 제안이다.  
`https://modelcontextprotocol.io/specification/2025-11-25/server/tools`

**[S2] MCP Transports — 2025-11-25 사양**  
Streamable HTTP와 로컬 서버의 Origin·binding·인증 관련 요구를 참고했다. 실제 지원 버전은 구현 시 다시 고정한다.  
`https://modelcontextprotocol.io/specification/2025-11-25/basic/transports`

**[S3] MCP Authorization — 2025-11-25 사양**  
HTTP 인증과 권한 경계를 정할 때의 기준이다. 인증 구현 방식이나 client 호환성을 이 문서에서 실증한 것은 아니다.  
`https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization`

**[S4] OMP hub 문서 — 이전 검토에서 확인한 소스 snapshot**  
Peer messaging의 process-global 범위와 process supervision의 cross-instance 기능을 구분하는 근거다. 최신 release 전체에 대한 검증이나 독립 두 OMP 간 전달 실험 결과를 뜻하지 않는다.  
`https://github.com/can1357/oh-my-pi/blob/ef6d8b2d0c2af26417c633619d8dcce1cc61a226/docs/tools/hub.md`

---

## 최종 제품 정의

**Task Inbox & Web Board는 원격 명령 실행 서버가 아니라, 외부 요청을 manager의 검토와 내부 실행 명세로 연결하는 영속 요청 게시판이다.**

실험 실행과 관측은 1차 Workbench가 계속 책임지고, 2차는 요청 접수·토의·검토·결과 공개를 추가한다.
