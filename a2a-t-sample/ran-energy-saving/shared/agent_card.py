"""AgentCard identity constants of the RAN energy-saving sample.

The constants live in ``shared`` (instead of :mod:`protocol.server_app`) because the client also
needs them for discovery: importing the server assembly from the client process would pull in the
server protocol modules and an unused server runtime at import time.
"""

from __future__ import annotations

from typing import Final

from a2a_t.core.plan_binding import CANONICAL_PROFILE

from shared.assurance import ASSURANCE_RECOMPUTED
from shared.typed_payload import TYPED_SCHEMA_URI

#: AgentCard name registered in the registry.
AGENT_CARD_NAME: Final[str] = "RAN NMS Agent"

#: AgentCard description.
AGENT_CARD_DESCRIPTION: Final[str] = (
    "RAN network management agent: validates and negotiates energy-saving tasks, then "
    "evaluates, applies and reports energy-saving plans."
)

#: Provider organization of the sample card.
AGENT_CARD_ORGANIZATION: Final[str] = "Huawei"

#: Provider URL of the sample card.
AGENT_CARD_PROVIDER_URL: Final[str] = "https://www.huawei.com"

#: Skill id of the card.
AGENT_CARD_SKILL_ID: Final[str] = "energy-saving"

#: Sample negotiation example advertised by the card.
AGENT_CARD_EXAMPLE: Final[str] = (
    "Create a RAN energy-saving task for XX Region to maximize energy savings. The "
    "time-segment-based guaranteed throughput target is {00:00-07:00,2Mbps}, "
    "{07:00-17:30,10Mbps}, {17:30-23:00,20Mbps}, {23:00-24:00,2Mbps}."
)

#: What this agent can promise about the contract, published before the first message.
#:
#: The values are answers a client needs *before* it decides how much proof it will get: which
#: statement schema the peer reads, which canonical form its digests use, the best assurance grade
#: it can earn, and whether it publishes an agreement resource and a reversal. Discovering any of
#: that from the result means the run already happened, which is the wrong time to learn it.
TASK_T_CAPABILITY_PARAMS: Final[dict[str, object]] = {
    "schemaUri": TYPED_SCHEMA_URI,
    "canonicalForm": CANONICAL_PROFILE,
    "assurance": ASSURANCE_RECOMPUTED,
    "agreementResource": True,
    "reversal": True,
}
