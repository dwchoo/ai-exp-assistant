"""Diagnostic-only public event identity probe for two real OMP TUI sessions."""

from __future__ import annotations

import argparse
import json
import shutil

from live_omp_probe import OMP_VERSION, _omp_version
from live_tui_draft_probe import run

IDENTITY_FIELDS = {
    "workbench_message_id", "workbench_delivery_attempt_id", "kind",
    "task_id", "revision_id", "run_id",
}


def main(*, provider_request: bool = False) -> int:
    omp = shutil.which("omp")
    if not omp or _omp_version(omp) != OMP_VERSION:
        print(json.dumps({"result": "requires_pinned_omp"}))
        return 2

    evidence = []
    pair_observed = True
    for mode, async_error in (("normal", False), ("post_ack_http_422", True)):
        outcome = run(omp, async_error=async_error, event_surface=True)
        surface = outcome.get("event_surface")
        evidence.append({
            "mode": mode,
            "harnessResult": outcome["result"],
            "apiReturned": outcome.get("api_return_status") == "api_accepted",
            "providerIdentityMatched": outcome.get("provider_identity_matched") is True,
            "providerErrorAfterApiAck": outcome.get("provider_error_after_api_ack") is True if async_error else None,
            "eventSurface": surface,
            "ompChildrenRemaining": outcome.get("omp_children_remaining"),
            "pids": outcome.get("pids"),
        })
        if not isinstance(surface, dict) or surface.get("userIdentityExact") is not True:
            print(json.dumps({"result": "public_event_identity_unknown", "evidence": evidence}, sort_keys=True))
            return 1
        events = surface.get("events")
        users = [(index, event) for index, event in enumerate(events) if isinstance(event, dict)
                 and event.get("name") == "delivery_user_message_end_probe"] if isinstance(events, list) else []
        user_after_ack = len(users) == 1 and all((
            users[0][1].get("afterApiAck") is True,
            users[0][1].get("role") == "worker",
            isinstance(users[0][1].get("sessionId"), str),
            type(users[0][1].get("generation")) is int,
            users[0][1].get("roleMatched") is True,
            users[0][1].get("sessionMatched") is True,
            users[0][1].get("generationMatched") is True,
        ))
        if provider_request:
            requests = [event for event in events if isinstance(event, dict)
                        and event.get("name") == "provider_request_identity_probe"] if isinstance(events, list) else []
            matches = requests[0].get("matches") if len(requests) == 1 else None
            request_exact = len(requests) == 1 and user_after_ack and all((
                requests[0].get("eventObject") is True,
                requests[0].get("afterApiAck") is True,
                requests[0].get("payloadShape") == "object",
                requests[0].get("messagesArray") is True,
                requests[0].get("lastMessageRole") == "user",
                type(requests[0].get("structuredBlockCount")) is int
                    and requests[0].get("structuredBlockCount") == 1,
                requests[0].get("identityFieldsPresent") is True,
                isinstance(matches, dict) and set(matches) == IDENTITY_FIELDS
                    and all(value is True for value in matches.values()),
                requests[0].get("roleMatched") is True,
                requests[0].get("sessionMatched") is True,
                requests[0].get("generationMatched") is True,
                requests[0].get("role") == users[0][1]["role"],
                requests[0].get("sessionId") == users[0][1]["sessionId"],
                requests[0].get("generation") == users[0][1]["generation"],
            ))
            evidence[-1]["providerRequestIdentityExact"] = request_exact
            if not request_exact:
                print(json.dumps({"result": "provider_request_identity_unknown", "evidence": evidence}, sort_keys=True))
                return 1
        error_after_ack = not async_error or (user_after_ack and any(
            isinstance(event, dict)
            and event.get("name") == "delivery_assistant_message_end_probe"
            and event.get("afterApiAck") is True
            and event.get("role") == users[0][1]["role"]
            and event.get("sessionId") == users[0][1]["sessionId"]
            and event.get("generation") == users[0][1]["generation"]
            and event.get("stopReason") == "error"
            and event.get("errorMessagePresent") is True
            for event in events[users[0][0] + 1:]
        ))
        pair_observed = pair_observed and all((
            outcome.get("result") == ("observed_async_error" if async_error else "passed_pair_tui_draft_delivery"),
            outcome.get("api_return_status") == "api_accepted",
            outcome.get("provider_identity_matched") is True,
            outcome.get("provider_error_after_api_ack") is True if async_error else True,
            type(outcome.get("omp_children_remaining")) is int
                and outcome.get("omp_children_remaining") == 0,
            surface.get("identitySpecificOutcome") == "unknown",
            user_after_ack,
            error_after_ack,
        ))
    print(json.dumps({
        "result": "diagnostic_pair_observed" if pair_observed else "diagnostic_pair_unknown",
        "evidence": evidence,
    }, sort_keys=True))
    return 0 if pair_observed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-request-probe", action="store_true")
    raise SystemExit(main(provider_request=parser.parse_args().provider_request_probe))
