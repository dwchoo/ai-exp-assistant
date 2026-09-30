# Handoff: Graphify Codex skill contribution

## Current State Summary

`graphifyy 0.9.67`의 Codex용 skill에 서로 충돌하는 semantic extraction 지침이 있다. 설치 오류나 로컬 파일 손상이 아니라, 설치된 wheel과 upstream `v8` branch에 같은 문제가 포함되어 있다.

## Important Context

Codex용 Step B2는 subagent 결과를 memory에서 수집하므로 `.graphify_chunk_NN.json` 파일이 생성되지 않는다고 설명한다. 하지만 바로 다음 Step B3는 해당 chunk file의 존재를 성공 조건으로 검사하고, 파일이 없으면 subagent 실패로 처리한다.

문제가 있는 upstream 파일은 `skill-codex.md`이며, 실제 수정 대상은 generator source인 `codex-agenttask.md`와 `core.md`이다. 관련 issue `#273`은 `wait`를 `wait_agent`로 변경한 별도 문제라서 정확한 중복은 아니다.

## Symptoms

- Codex subagent가 정상적인 JSON 결과를 반환해도 chunk file이 없다는 이유로 결과가 누락될 수 있다.
- docs, PDF, image semantic extraction이 실패하거나 빈 결과로 merge될 수 있다.
- code-only AST extraction과 기존 `query`, `path`, `explain` 명령은 정상 동작한다.

## Immediate Next Steps

Upstream `v8` branch를 기준으로 새 issue를 등록하고, Codex의 in-memory collection과 shared disk collection 중 하나로 흐름을 통일하는 PR을 준비한다. Generated `skill-codex.md`만 직접 수정하지 말고 `tools/skillgen` source와 regression test를 함께 수정해야 한다.
