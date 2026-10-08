# p27-cw16-b4a-01 — CW-16 G4 최종 evidence validator 테스트 (test_designer, W 단계)

작성 2026-10-08, base `6b837ae`. 쓴 파일은 `tests/integration/test_cw16_final_evidence_validators.py`와 이 결과 파일 2개다. 모델·provider 요청 0, 자격 증명 미열람, 임시 파일은 `/tmp/wb-cw16-g4-*` 아래에만 만들었고 끝나면 삭제했다. commit 없음, `graphify update` 없음. src/omp_bridge/docs/다른 테스트/.workflow(이 파일 제외)는 건드리지 않았다.

## 1. 구성

- `FinalRecordsTests`: env `WB_CW16_FINAL_REQUEST`가 없으면 `setUpClass`에서 **skip**한다. pass로 세지 않는다. env가 있는데 request가 잘못됐으면 skip이 아니라 error로 끝난다.
- `SelfCheckTests`: 항상 실행된다. 합성 records로 같은 battery(`GateBattery`, `AggregateBattery`)를 돌린다. 합성 records는 requirements 3 item, check 2개+log, stale record, 합성 repo root로 만든다. 또 일부러 허용적으로 만든 validator가 battery에 걸리는지 확인한다(meta).
- 원본 records는 수정하지 않는다. requirements·check·log·manifest 입력·evidence는 /tmp로 복사해 변형한다. 변경된 것은 두 가지뿐이다. check의 `log`는 tmp 복사본 경로를 가리키고, aggregate record의 `cwd`는 원래 root와 같을 때만 tmp root로 바꾼다.
- `records_from_checks(...)`: observed check item을 aggregate record로 변환하는 helper다. self-check가 사용하고, Root가 records 파일을 만들 때도 쓸 수 있다. item 하나가 여러 check에 있으면 `sources`(item→check_id)로 하나를 고르게 하고, 고르지 않으면 거절한다.

## 2. Request JSON schema (module docstring에도 있음)

```
{ "schema_version": 1,
  "requirements": "<abs> requirements-p27-cw16.json",
  "candidate": "<C16 64-hex>",
  "checks": ["<abs> check-p27-cw16-<batch>-observed.json", ...],
  "gate_result": "<abs> gate-p27-cw16-final-result.json",
  "stale_check": "<abs>"            # 선택, 기본값 p2.6 check-cw16-final-observed.json (sha cdeca27f… 고정)
  "missing_item": "I-FAULT",        # 선택
  "aggregate": {
    "root": "<abs repo>",           # 선택, 기본값 이 repo
    "records": "<abs> JSON list",
    "expected": {object | abs path}: items, candidate_id, requirements_digest, approval_digest,
                input_manifest {object | abs path}, evidence {item: {path, sha256}} } }
```

record 필드: `item_id, owner_ticket, candidate_id, requirements_digest, approval_digest, input_manifest, required_evidence_level, observed_evidence_level, evidence_kind("actual_runtime"), result, exit_code, unknowns, command(=check argv), cwd(=check cwd), environment, evidence_ref{path,sha256}`(=해당 observed check JSON).

## 3. 최종 모드에서 하는 검사

| 테스트 | 내용 |
|---|---|
| expected_context_is_pinned_by_root | requirements gate id가 PLAN의 7개(`FINAL_ITEMS`, 테스트 상수)이고 모두 runtime이다. expected.items={7개: owner CW-16, runtime}. expected.candidate_id=request candidate다. digest 2개가 hex 형식이다. evidence는 7개 key이고 각 path는 request의 observed check 파일이다 |
| input_manifest_scope_is_complete | VERIFICATION/COMPATIBILITY가 없다. key 집합 = `git ls-files -c -o --exclude-standard -- src omp_bridge tests pyproject.toml docs/…/{PLAN.json,SPEC.md,BRIEF.md,DECISIONS.md}`(존재 파일만) |
| records_match_observed_checks | record의 result/unknowns/level/exit/argv/cwd/candidate(before=after)가 evidence_ref로 가리킨 observed check item과 같다. evidence_kind는 actual_runtime이다 |
| recorded_gate_result_passed | 기록된 gate 결과 == `{"passed":true,"issues":[]}` |
| gate genuine / mutations | 아래 §4. mutation 테스트는 genuine 통과를 먼저 확인한다 |
| aggregate genuine(repo root와 tmp 복사본) / mutations | 아래 §4. 반환값은 7개 id(sorted)다 |

## 4. 음성 대조(정확한 결과를 비교)

Workflow gate는 CLI subprocess로 돌린다. 모든 경우 exit 1, `passed:false`이고 `issues` 리스트 전체가 정확히 같아야 한다.

| case | 변형 | 기대 issues |
|---|---|---|
| missing_check | `missing_item`(I-FAULT)를 담은 check 전체를 제거 | 남은 check가 덮지 못하는 gate마다 `{"id","kind":"missing"}`(I-FAULT 포함 필수) |
| stale_record | genuine + p2.6 6dc51f record | P-C-AC-19·I-FLOW·I-SHELL·I-POLICY·I-FAULT·I-COMPAT=`[stale,failed_or_not_run,unknown]`, P-C-AC-20=`[stale]`(check_id cw16-final-01) |
| unknowns | 첫 occurrence item에 `unknowns:["x"]` | 그 item 1건 `[unknown]` |
| log_tamper | 해당 check의 log 복사본에서 가운데 1바이트 XOR | 그 check의 모든 item `[log_mismatch]` |
| other_candidate | candidate 인자 변경 | 모든 (item, check) `[stale]` |
| fixture_level | 한 item `observed_evidence_level:"fixture"` | `[evidence_level]` |
| acceptance_mismatch | acceptance id 하나 제거(1개면 다른 AC로 교체) | `[acceptance_mismatch]` |

Aggregate `validate()`는 `EvidenceError`를 내야 하고 메시지가 정확히 일치해야 한다.

| case | 변형 | 메시지 |
|---|---|---|
| missing_record | record 1개 제거 | missing, duplicate or unowned gate items |
| duplicate_record | 중복 추가 / 다른 record를 중복으로 대체 | 같음 |
| unowned_record | 외부 id 추가 / id 변경 | 같음 |
| changed_input | tmp root의 manifest 파일 1개에 1바이트 추가 / 삭제 | current integrated input changed |
| failed_or_unknown | result failed / unknowns ["x"] / exit_code 1 | failed check or required unknown |
| changed_candidate | candidate_id 변경 | changed candidate_id |
| insufficient_level | observed fixture / evidence_kind fixture | insufficient evidence level / fixture-only runtime evidence |
| evidence_changed | evidence 파일 1바이트 flip / 삭제 | evidence content changed/missing |
| fixture_key | pinned observed JSON에 `"fixture": true` 추가(expected·record sha도 함께 갱신) | synthetic evidence requires explicit provenance-only fixture mode |

파일을 변형한 경우는 `finally`에서 원래 내용으로 되돌린다. self-check가 변형 뒤에도 genuine이 다시 통과하는지 확인한다.

## 5. Self-check 결과(실행 기록)

- `/tmp/cw02-g1-venv/bin/python -m unittest tests.integration.test_cw16_final_evidence_validators -v`(PYTHONDONTWRITEBYTECODE=1): **Ran 7, OK(skipped=1)**. skip 1건은 `FinalRecordsTests`(env 없음)이고 약 0.7초 걸린다. `discover -s tests/integration -p test_cw16_final_evidence_validators.py`로 돌려도 결과가 같다.
  - gate genuine + 7 mutation 통과, 원본 log sha 불변.
  - stub gate 2종(항상 pass / 틀린 kind)에서 7 case가 모두 AssertionError를 낸다(battery가 잡아냄).
  - aggregate genuine + 9 case 통과, 변형 뒤 genuine 재통과.
  - 허용적 validator와 틀린 메시지 validator에서 9 case가 모두 AssertionError를 낸다.
  - `load_request`는 정상 request를 받고, 잘못된 request 8종(schema, candidate, checks 빈/상대 경로, gate_result·aggregate·records·approval_digest 누락)은 거절한다.
  - **p2.6 상수 확인**: 실제 `requirements-p27-cw16.json`과 실제 `check-cw16-final-observed.json`(sha `cdeca27f…`)으로 gate를 돌린 결과가 `P26_STALE_ISSUES`와 정확히 일치한다.
- 최종 모드 dry run(테스트 파일 밖, /tmp, 이후 삭제): 실제 p2.7 requirements, 합성 check 3개(compat/flow/fault 묶음, 7 item), /tmp git repo root, records_from_checks로 request를 만들었다. `WB_CW16_FINAL_REQUEST`를 설정하자 FinalRecordsTests **9개 OK**(전체 16개 OK)였다. records 하나를 unknowns ["x"]로 바꾸자 genuine 2개·mutation 1개가 error, consistency 1개가 fail로 나와 실패가 정상 검출됐다.

## 6. Root에 넘길 사항

- R 단계 뒤 Root는 records 파일(7 record, item마다 observed check 1개)과 expected context를 만든다. expected context는 records에서 파생하지 않고 C16 snapshot·approved bundle·PLAN에서 고정한다. 그다음 request JSON을 쓰고 `WB_CW16_FINAL_REQUEST=<request> python -m unittest tests.integration.test_cw16_final_evidence_validators -v`를 실행한다.
- `input_manifest_scope_is_complete`는 실행 시점의 git 작업 트리를 기준으로 한다. 따라서 C16 이후 src/tests 등에 파일이 추가되거나 바뀌면 실패한다. 이는 의도한 동작이다(입력 변경 검출). docs의 VERIFICATION/COMPATIBILITY 작성은 영향을 주지 않는다.
- 같은 item이 check 여러 개에 있으면(예: P-C-AC-20이 compat와 shell 두 check에 있는 경우) records 작성 때 `sources`로 하나를 고른다. gate 음성 대조의 기대값은 occurrence 단위로 계산하므로 별도 조치는 필요 없다.
- 이 테스트 파일은 C16 candidate에 포함된다(tests/**). R 단계 정식 check 전에 확정해야 한다.
