"""A client executes a stored agreement revision without resending the Task-T.

The agreement is a durable object on the server side. This test proves the client can reach it:
it sends one reference, no Task-T document and no typed statement, and still gets a Task whose
plan digest is the revision it named.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
import pytest
from a2a.client.client import ClientCallContext
from a2a.types import SendMessageRequest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI, MetadataContent, NegotiationContext
from a2a_t.negotiation.content import NegotiationEndingData
from demo import ran_data
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import FIDELITY_FAST, WINDOW_POLICY_ANY, SimConfig
from protocol.client_flow import (
    FINAL_STATE_TASK_COMPLETED,
    ClientTransport,
    run_client_flow,
)
from protocol.decision_provider import ScriptedDecisionProvider
from protocol.messages import metadata_to_dict
from protocol.nms_client import NmsClient, NmsConfig, NmsSseTransport
from shared.agreement import AGREEMENT_METADATA_KEY, REQUEST_ID_METADATA_KEY
from shared.typed_payload import TYPED_TASK_T_METADATA_KEY

TOKEN_URL = "http://sim/rest/plat/smapp/v1/sessions"
A2A_BASE = "http://sim/a2a/json"
SEND_URL = "http://sim/a2a/json/message:send"
USER = "snbitestDefaultNorth"
VALUE = "Changeme_123"


@pytest.fixture(autouse=True)
def _reset_plans() -> None:
    ran_data.reset()


def _config() -> SimConfig:
    return SimConfig(fidelity=FIDELITY_FAST, window_policy=WINDOW_POLICY_ANY)


def _prompt() -> str:
    return "\n".join(
        [
            "## Operation Type",
            "",
            "Create",
            "",
            "## Task Object",
            "",
            "The area is A2ATestRegion.",
            "",
            "## Task Context",
            "",
            "Energy saving time: 00:00~06:00; guaranteed throughput: 30 Mbps.",
        ]
    )


def _body(prompt: str) -> dict[str, object]:
    return {
        "message": {
            "messageId": "m-1",
            "role": "ROLE_USER",
            "parts": [{"text": "create ran energy saving task"}],
            "metadata": {TASK_T_EXTENSION_URI: prompt},
        }
    }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "A2A-Extensions": TASK_T_EXTENSION_URI}


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL,
        json={"grantType": "password", "userName": USER, "value": VALUE},
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _agreement_of(document: Mapping[str, object]) -> dict[str, object]:
    task = document.get("task") if isinstance(document.get("task"), Mapping) else document
    metadata = task.get("metadata") if isinstance(task, Mapping) else None
    raw = metadata.get(AGREEMENT_METADATA_KEY) if isinstance(metadata, Mapping) else None
    assert isinstance(raw, dict), document
    return raw


class _NeverUsedPromptClient:
    """SDK prompt facade that fails the test if the flow generates a Task-T at all."""

    def generate_task_prompt_from_text(self, text: str, template_uri: str) -> MetadataContent:
        del text, template_uri
        raise AssertionError("An agreement reference must not generate a Task-T prompt.")

    def generate_task_prompt_from_data_with_schema(
        self,
        data: Mapping[str, object],
        schema: Mapping[str, object],
        template_uri: str,
    ) -> MetadataContent:
        del data, schema, template_uri
        raise AssertionError("An agreement reference must not generate a Task-T prompt.")

    def generate_negotiation_accept_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        del text, context, template_uri
        raise AssertionError("An agreement reference must not open a negotiation.")

    def generate_negotiation_accept_prompt_from_data(
        self, data: NegotiationEndingData, template_uri: str
    ) -> MetadataContent:
        del data, template_uri
        raise AssertionError("An agreement reference must not open a negotiation.")

    def generate_negotiation_reject_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        del text, context, template_uri
        raise AssertionError("An agreement reference must not open a negotiation.")

    def generate_negotiation_abort_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        del text, context, template_uri
        raise AssertionError("An agreement reference must not open a negotiation.")

    def generate_negotiation_propose_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        del text, context, template_uri
        raise AssertionError("An agreement reference must not open a negotiation.")

    def validate_task_prompt_and_data_filling(
        self,
        prompt: str,
        schema: Mapping[str, object],
        template_uri: str,
    ) -> object:
        del prompt, schema, template_uri
        raise AssertionError("An agreement reference must not validate a Task-T.")


@dataclass
class _SentRequest:
    """One request the flow handed to its transport, kept for the wire assertions."""

    request: SendMessageRequest
    call_context: ClientCallContext


class _RecordingTransport:
    """Transport decorator recording every outbound request without changing the flow."""

    def __init__(self, inner: ClientTransport) -> None:
        self._inner = inner
        self.requests: list[_SentRequest] = []

    async def send(
        self,
        request: SendMessageRequest,
        call_context: ClientCallContext,
    ) -> list[dict[str, object]]:
        self.requests.append(_SentRequest(request=request, call_context=call_context))
        return await self._inner.send(request, call_context)


async def test_a_client_executes_a_stored_agreement_without_a_task_t() -> None:
    app = build_sim_app(_config())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sim"
    ) as http:
        token = await _token(http)
        created = await http.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        assert created.status_code == 200, created.text
        agreement = _agreement_of(created.json())
        original_task_id = str(created.json()["task"]["id"])

        nms = NmsClient(
            NmsConfig(
                a2a_base=A2A_BASE,
                token_url=TOKEN_URL,
                token_user=USER,
                token_value=VALUE,
                wire_log=False,
            ),
            http_client=http,
        )
        transport = _RecordingTransport(NmsSseTransport(nms))
        result = await run_client_flow(
            prompt_client=_NeverUsedPromptClient(),
            transport=transport,
            user_request="execute the agreement",
            provider=ScriptedDecisionProvider([]),
            agreement_ref={"agreementId": agreement["agreementId"], "revision": 1},
        )

        assert len(transport.requests) == 1
        sent = transport.requests[0]
        metadata = metadata_to_dict(sent.request.message.metadata)
        # The reference is the whole request: no Task-T document, no typed statement.
        assert TASK_T_EXTENSION_URI not in metadata
        assert TYPED_TASK_T_METADATA_KEY not in metadata
        assert metadata[AGREEMENT_METADATA_KEY] == {
            "agreementId": agreement["agreementId"],
            "revision": 1,
        }
        assert metadata[REQUEST_ID_METADATA_KEY]
        # The extension header still makes the peer classify the hop as A2A-T.
        parameters = sent.call_context.service_parameters or {}
        assert TASK_T_EXTENSION_URI in str(parameters.get("A2A-Extensions", ""))

        assert result.final_state == FINAL_STATE_TASK_COMPLETED
        assert result.task_id is not None
        assert result.task_id != original_task_id
        # The executed Task must name the revision the client asked for. A fresh id here would mean
        # the reference selected a plan but not the promise the client was relying on.
        assert result.agreement_id == agreement["agreementId"]
        assert result.revision == 1

        fetched = await http.get(
            f"http://sim/a2a/json/tasks/{result.task_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        executed = _agreement_of(fetched.json())
        assert executed["planDigest"] == agreement["planDigest"]
        assert executed["agreementId"] == agreement["agreementId"]
        assert executed["revision"] == 1

        # The agreement names a reversal, so the audit asks the reversal question -- and answers it
        # honestly: this run did not abort, so nothing was restored.
        assert executed["reversal"]
        links = {link["name"]: link["status"] for link in result.audit["links"]}
        assert links["execution->restored"] == "not-applicable"
