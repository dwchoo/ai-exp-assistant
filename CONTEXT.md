# 도메인 용어

정의의 출처: [Phase 2 PRD](docs/OMP_Workbench_Phase2_Task_Inbox_PRD_v0.1.md)와 [Core 인터뷰 결정](docs/features/core-workbench/BRIEF.md).
요구사항의 합의 상태와 미결정 사항은 해당 기능의 BRIEF에서 관리한다.

| 용어 | 의미 |
|---|---|
| OMP Workbench | Manager OMP, worker OMP, host persistent terminal과 내부 mailbox를 연결하는 작업 환경. PRD는 1차 core와 2차 부속 기능을 구분한다. |
| Core Workbench / 1차 | 자체 wrapper 안에서 manager와 worker의 대화·명세 전달·실행·관측을 제공하는 기반 기능. |
| Manager | 사용자와 논의하고 작업을 계획하며 코드를 수정하는 역할. Worker에게 실행할 작업을 전달한다. |
| Worker | 실험 실행, 로그 분석과 결과 보고를 맡는 역할. 코드 수정은 manager의 역할로 구분한다. |
| 위임 범위 | 사용자가 manager에게 자율 판단을 맡긴 작업의 경계. 구체적인 허용 행동과 예산·재확인 조건은 제품 동작 규약에서 정한다. |
| 자동 복구 재시도 | 실패한 작업을 복구하기 위한 재실행 시도. 최초 실행과 승인된 정상 개선 실험은 구분하며, commit 변경만으로 다른 작업이 되지 않는다. |
| Hang 의심 | 실행 진행이 정체되었을 가능성이 있어 로그와 프로세스 상태의 추가 조사가 필요한 상태. 단순한 무출력이나 실행 시간 초과를 뜻하지 않는다. |
| 실행용 worktree | Worker가 기준 commit의 코드를 실행하기 위해 사용하는 별도 Git 작업 디렉터리. |
| 개발용 worktree | Manager 또는 사용자가 코드를 수정하는 Git 작업 디렉터리. 실행용 worktree와 분리한다. |
| Terminal 입력 제어권 | Host terminal에 입력할 주체를 구분하는 권한. 사용자가 가져오면 worker의 자동 입력을 멈추며, 실행 중 프로세스를 중단하는 동작과는 구분한다. |
| Host persistent terminal | Workbench가 실행되는 host의 shell 환경과 상태를 유지하며 명령을 실행하고 출력을 보여주는 terminal. 사용자·agent의 입력 권한과 앱 종료 후 생존 여부는 별도 동작 정책이다. |
| 화면 분리 / detach | 사용자 화면 연결만 닫는 동작. 같은 host에서 Workbench 실행·manager·worker는 유지된다. 주기 모델 점검은 자동화 활성 중 계속되고 일시정지 중 새로 시작하지 않는다. |
| Workbench 종료 | 화면 분리와 구분되는 명시적 실행 환경 종료 동작. 실행 중 실험의 중단·정리 순서는 제품 동작 규약에서 정한다. |
| Task Inbox & Web Board / Phase 2 | 외부 요청을 접수·보관하고 manager 검토, 승인 명세 및 실행 결과에 연결하는 선택적 부속 기능. |
| 요청 / request | 외부 agent가 제출한 검토 대상 자료. 접수 자체는 실행 승인이 아니다. |
| 승인 명세 / task specification | Manager가 권한 범위 안에서 확정하는 실행 조건과 완료 기준. 원본 요청과 구분하며 task ID와 revision으로 식별한다. |
| Task | 하나의 목표·완료 조건과 실행·복구 이력을 묶는 작업 단위. 명세 revision이나 commit 변경만으로 별도 작업이 되지 않는다. |
| 명세 revision | 작업 명세의 한 버전. 실행 중 변경은 새 버전으로 기록하며 기존 실행에 사용한 버전과 결과 이력을 보존한다. |
| 작업 완료 | Manager가 승인된 완료 조건과 worker의 검증 근거를 대조해 목표 달성을 확정한 상태. 명령의 정상 종료와 구분한다. |
| Run | 특정 작업 명세와 실행 입력에 연결된 한 번의 실행 시도. 재시도는 별도 run으로 구분한다. |
| 자동화 일시정지 | 새 자동 지시와 manager·worker의 새 자동 모델 작업을 보류하고 manager OMP의 진행 중 turn 중단을 요청한 상태. 1분 worker 모델 점검은 멈추며 로그·프로세스 수집과 이미 실행 중인 host 실험은 계속한다. |
| 일시정지 중 | 일시정지 요청 수락부터 사용자의 명시적 재개 또는 작업 취소 전까지의 상태. Turn 중단 요청·실제 중단 확인·도구 결과 불명은 서로 구분하며 수동 조작만으로 자동화가 재개되지 않는다. |
| Turn 중단 확인 | Manager OMP에서 진행 중이던 turn의 종료 event를 관측한 사실. 이미 실행된 도구의 결과나 rollback을 뜻하지 않는다. |
| 실험 환경 | 사용자가 선택한 Python/conda/venv와 실행 설정. Workbench 자체를 구동하는 환경과 구분한다. |
| 모델 점검 중지 | 주기 worker 모델 점검을 멈추는 동작. 자동화 일시정지 중에는 적용되며, 로그·프로세스 수집이나 실행 중 실험의 종료를 뜻하지 않는다. |
| Mailbox | 검토할 변경의 통지와 내부 역할 간 전달 수단. 영속 요청 저장소와 구분한다. |
