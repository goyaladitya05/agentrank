"""Merchant API credential application service.

Four operations: issue one, list a merchant's, revoke one, and authenticate a presented token.
The first three are trusted provisioning and are reachable only from the operator command line.
The fourth is what every authenticated HTTP request runs.

Two rules shape this module:

- the raw secret exists in exactly one place, which is the return value of `issue`, and for
  exactly as long as the process that called it. Nothing writes it, nothing logs it, and no
  method anywhere returns it a second time
- authentication answers with a principal or with nothing. It never explains itself. Which of
  the several ways a token can fail to authenticate happened is not a caller's business, and a
  service that returned a reason would be a service somebody eventually surfaced
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.auth.models import MAX_LABEL_LENGTH, MerchantApiCredential
from agentrank_api.auth.principal import AuthenticatedMerchant
from agentrank_api.auth.repository import MerchantCredentialRepository
from agentrank_api.auth.tokens import (
    TokenMarker,
    format_token,
    generate_secret,
    hash_secret,
    parse_token,
    verify_secret,
)
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.mutation import BenchmarkMutationGuard
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import NotFoundError

CREDENTIAL_RESOURCE = "merchant_api_credential"
CREDENTIAL_ISSUED = "credential.issued"
CREDENTIAL_REVOKED = "credential.revoked"

# Issuing and revoking a credential are things this application did because somebody with a
# shell and the database environment told it to. `SYSTEM` is the honest role for that, and it is
# the same one an operator abandonment carries. There is deliberately no new `OPERATOR` actor
# type, because there is no authenticated operator behind these commands, and adding the label
# without the evidence would be adding something that looks like attribution and is not. See
# docs/security.md.
PROVISIONING_ACTOR = ActorType.SYSTEM


def validate_label(label: str) -> str:
    """Check a credential label and return it trimmed, or refuse it.

    Required rather than optional, because the label is the only thing that distinguishes three
    keys belonging to one merchant, and the moment it matters is the moment somebody has to
    decide which of them to revoke.

    Bounded and single line, so a value written here cannot reformat a terminal or a log line
    that prints it back. It must never carry a secret: it is printed by the operator listing and
    recorded in an append only table that refuses UPDATE and DELETE.
    """
    trimmed = label.strip()
    if not trimmed:
        raise ValueError("a credential label cannot be blank")
    if len(trimmed) > MAX_LABEL_LENGTH:
        raise ValueError(
            f"a credential label is at most {MAX_LABEL_LENGTH} characters, got {len(trimmed)}"
        )
    if not trimmed.isprintable():
        raise ValueError("a credential label must be a single line of printable characters")
    return trimmed


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """A new credential and the one string that will ever carry its secret.

    The token is here and nowhere else. It is not stored, it cannot be recomputed from the row,
    and there is no method on any service that returns it again. An operator who loses it issues
    another credential and revokes this one, which is the same thing they would do if it leaked.

    Two fields rather than one because both are wanted at once: the caller prints the token for
    a human and prints the identifier so the same human can find the credential again.
    """

    credential: MerchantApiCredential
    token: str


@dataclass(frozen=True, slots=True)
class CredentialRevocation:
    """A credential after a revocation, and whether this call is what revoked it.

    `changed` is what makes the idempotence observable. A second revocation returns the same
    credential with the same timestamp and reports False, and the caller records no second
    event.
    """

    credential: MerchantApiCredential
    changed: bool


class MerchantCredentialService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._merchants = MerchantRepository(session)
        self._credentials = MerchantCredentialRepository(session)
        self._audit = AuditRepository(session)

    async def issue(
        self, *, merchant_id: uuid.UUID, label: str, marker: TokenMarker
    ) -> IssuedCredential:
        """Mint a credential for one merchant and record that it was issued, in one transaction.

        The merchant is looked up first so that an unknown one is a refusal naming the merchant
        rather than a foreign key violation surfacing as a driver error.

        The secret is generated, hashed, and the hash is what is written. The secret itself is
        returned to the caller and is then unrecoverable: this method is the only place in the
        application where it exists, and the row it leaves behind cannot produce it.

        Both writes happen in one transaction and one commit. If the audit append fails, the
        credential is not persisted either: a key that can authenticate with no record of having
        been issued is exactly what the trail exists to prevent.
        """
        merchant = await self._merchants.get_by_id(merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))

        secret = generate_secret()
        credential = await self._credentials.create(
            merchant_id=merchant_id,
            secret_hash=hash_secret(secret),
            label=validate_label(label),
        )
        await self._append(credential, CREDENTIAL_ISSUED, {"label": credential.label})
        await self._session.commit()
        return IssuedCredential(
            credential=credential,
            token=format_token(credential.id, secret, marker=marker),
        )

    async def issue_for_benchmark(
        self, *, capability: BenchmarkRunCapability, label: str, marker: TokenMarker
    ) -> IssuedCredential:
        """Mint a credential that may mutate only the run currently owning this world.

        The run capability is checked against the durable RUNNING row before the credential is
        written.  Its database foreign key then proves the bound run belongs to this merchant;
        ordinary credential issuance has no parameter that can set this binding.
        """
        await BenchmarkMutationGuard(self._session).require_active(capability)
        merchant = await self._merchants.get_by_id(capability.merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(capability.merchant_id))

        secret = generate_secret()
        credential = await self._credentials.create(
            merchant_id=capability.merchant_id,
            secret_hash=hash_secret(secret),
            label=validate_label(label),
            benchmark_run_id=capability.run_id,
        )
        await self._append(
            credential,
            CREDENTIAL_ISSUED,
            {"label": credential.label, "benchmark_run_id": str(capability.run_id)},
        )
        await self._session.commit()
        return IssuedCredential(
            credential=credential,
            token=format_token(credential.id, secret, marker=marker),
        )

    async def list_for_merchant(self, merchant_id: uuid.UUID) -> Sequence[MerchantApiCredential]:
        """Every credential one merchant holds, revoked ones included.

        The merchant is looked up first so that a mistyped identifier is a refusal rather than
        an empty listing that reads as "this merchant has no keys".
        """
        merchant = await self._merchants.get_by_id(merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))
        return await self._credentials.list_for_merchant(merchant_id)

    async def revoke(self, credential_id: uuid.UUID) -> CredentialRevocation:
        """Withdraw a credential and record it, once.

        Terminal and immediate. There is no counterpart that restores one, the database refuses
        the update, and the next request presenting this token finds nothing: the authentication
        read has the revocation condition in its SQL, so there is no cache and no window to wait
        out. A request that was already authenticated when this committed is not retroactively
        unauthenticated, and that is the honest boundary rather than a gap. See docs/security.md.

        Idempotent. Revoking an already revoked credential returns it unchanged and appends
        nothing, so a repeated command cannot move the original timestamp or produce a second
        event.

        Read without a lock, and that is deliberate rather than an omission. The transition is
        decided by `revoked_at IS NULL` on the row itself and the second writer's UPDATE sees
        the first one's committed value, so the worst a race produces is two commands both
        reporting that they revoked it. The state is the same either way, and there is no
        authorization decision resting on which of them wrote first. Every other terminal
        transition in this application locks because something reads the row and then acts on
        what it read; nothing does that here.
        """
        credential = await self._credentials.get(credential_id)
        if credential is None:
            raise NotFoundError("merchant_api_credential", str(credential_id))

        changed = await self._credentials.revoke(credential)
        if changed:
            await self._append(credential, CREDENTIAL_REVOKED, {"label": credential.label})
        # Committed either way. When nothing changed this just closes the read.
        await self._session.commit()
        return CredentialRevocation(credential=credential, changed=changed)

    async def authenticate(self, presented: str) -> AuthenticatedMerchant | None:
        """Turn a presented token into a principal, or into nothing.

        The three steps are in this order for a reason. Parsing first, so a value that is not
        even shaped like a token never reaches the database. Then one primary key lookup with
        the revocation condition in the SQL, so an unknown credential and a revoked one produce
        the same absence. Then a constant time comparison of the secret against the stored
        verifier.

        No reason is returned, and none is recorded. A caller learns that authentication failed
        and nothing else, because every distinction available here is a distinction worth
        keeping: whether that identifier exists, whether the key was revoked, whether the secret
        was merely wrong.

        The secret does not travel any further than this method. What is returned carries two
        identifiers, so nothing downstream is holding key material it could log.

        Timing is not equalised between "no such credential" and "wrong secret", and that is a
        deliberate non goal. The distinction it could leak is whether a credential identifier
        exists, and a credential identifier is not a secret: it is printed by the operator
        listing and recorded in audit events. The secret is what is compared in constant time,
        and that is the comparison that matters.
        """
        parsed = parse_token(presented)
        if parsed is None:
            return None

        credential = await self._credentials.get_active(parsed.credential_id)
        if credential is None:
            return None

        if not verify_secret(parsed.secret, credential.secret_hash):
            return None

        return AuthenticatedMerchant(
            merchant_id=credential.merchant_id,
            credential_id=credential.id,
            benchmark_capability=(
                None
                if credential.benchmark_run_id is None
                else BenchmarkRunCapability(
                    merchant_id=credential.merchant_id,
                    run_id=credential.benchmark_run_id,
                )
            ),
        )

    async def _append(
        self, credential: MerchantApiCredential, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Record one thing that happened to a credential.

        `credential_id` is deliberately not set on these events. That column says which
        credential authorized a request, and neither of these was authorized by a credential:
        issuing and revoking are done by somebody holding a shell. Filling it in with the
        credential the event is about would turn evidence about who acted into a restatement of
        `resource_id`.

        The payload carries the label and nothing else. There is no secret to record, no
        verifier belongs in a payload that is read in more places than the table is, and the
        identifier is already `resource_id`.
        """
        await self._audit.append(
            merchant_id=credential.merchant_id,
            actor_type=PROVISIONING_ACTOR,
            event_type=event_type,
            resource_type=CREDENTIAL_RESOURCE,
            resource_id=credential.id,
            payload=payload,
        )
