"""The shape of a merchant API key, how one is minted, and how one is verified.

Everything about the secret material lives here, in one module, so that the format, the
generator and the verifier cannot drift apart. Nothing else in this application constructs a
token, and nothing else compares one.

The format is deliberate:

```text
ar_dev_0199c4f0e2f97a1b8c3d4e5f60718293_<64 hex characters>
^  ^   ^                                ^
|  |   |                                the secret, 256 bits, never stored
|  |   the credential identifier, which is not a secret
|  the environment the credential was minted in, which is a label
the scheme
```

Four properties are what the format is for:

- it is recognisable. `ar_live_` in a log, a paste or a test fixture is unmistakably an
  AgentRank API key, and a scanner or a person can act on it without decoding anything
- the public half is separated from the secret half by a character neither half can contain,
  so parsing is a total function rather than a guess
- the public half is the credential's own identifier, so authentication is one primary key
  lookup rather than a scan of every credential that exists
- the secret half carries enough entropy that guessing it is not a threat model

Hexadecimal rather than base64 or base62, and that is the reason the token is as long as it
is. A URL safe base64 alphabet contains `_`, which is the separator, so a token would be
ambiguous to parse and the ambiguity would be discovered by a credential that happened to
contain one. Hexadecimal cannot collide with the separator, so the regular expression below
either matches a well formed token or matches nothing.

The environment marker is provenance for humans and is never checked. A credential is valid
because a row exists and its verifier matches, not because a string in the token agrees with
this process's configuration. Recording it is what makes a leaked key immediately
identifiable as production or not; treating it as a security boundary would be claiming a
guarantee that a caller supplies the input for.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

SCHEME = "ar"

# 256 bits. The number is here rather than inline because the pattern below and the generator
# have to agree about it, and a token whose length the parser does not expect is a token that
# fails authentication for a reason nobody can see.
SECRET_BYTES = 32

# One way, and stated in the stored value rather than assumed by the code that reads it. A
# verifier that does not say how it was produced cannot be replaced by a different algorithm
# later without guessing what the existing rows are.
HASH_ALGORITHM = "sha256"
HASH_PATTERN = rf"^{HASH_ALGORITHM}:[0-9a-f]{{64}}$"

_TOKEN_PATTERN = re.compile(
    rf"^{SCHEME}_(live|dev)_([0-9a-f]{{32}})_([0-9a-f]{{{SECRET_BYTES * 2}}})$"
)


class TokenMarker(StrEnum):
    """Which environment a credential was minted in, as it appears in the token.

    Two values, and neither is read when a request is authenticated. See the module docstring:
    this is provenance, not a check.
    """

    LIVE = "live"
    DEVELOPMENT = "dev"

    @classmethod
    def of(cls, environment: str) -> Self:
        """The marker for an environment name, defaulting to the one that claims less.

        Anything that is not production is development. Getting this wrong in the safe
        direction means a production key that says `dev`, which understates it; getting it
        wrong the other way would put `ar_live_` on a throwaway key and teach whoever finds
        one to ignore the prefix.
        """
        return cls.LIVE if environment.strip().lower() == "production" else cls.DEVELOPMENT


@dataclass(frozen=True, slots=True)
class ParsedToken:
    """A syntactically well formed token, split into the half that identifies and the half
    that proves.

    Parsing establishes nothing about whether the credential exists or is usable. It says the
    string could be one, which is what lets an obviously malformed value be refused without a
    database round trip.
    """

    credential_id: uuid.UUID
    secret: str


def generate_secret() -> str:
    """Mint one secret, from the operating system's cryptographic source.

    `secrets`, never `random`, never a UUID and never anything derived from a merchant, a slug
    or a clock. A secret that can be reconstructed from something a caller already knows is not
    a secret, and every one of those alternatives is reconstructible.
    """
    return secrets.token_hex(SECRET_BYTES)


def format_token(credential_id: uuid.UUID, secret: str, *, marker: TokenMarker) -> str:
    """Assemble the one string a caller is ever given.

    Built here and returned to exactly one caller, which prints it once. Nothing persists the
    result and nothing can reconstruct it afterwards: the secret half is gone the moment the
    process that minted it exits.
    """
    return f"{SCHEME}_{marker.value}_{credential_id.hex}_{secret}"


def parse_token(raw: str) -> ParsedToken | None:
    """Split a presented token, or report that it is not one.

    Total. Every string either matches the format exactly or produces None, so a caller cannot
    reach the database with a value this did not vouch for, and no malformed input becomes an
    exception a route has to translate.

    None is returned rather than raising, because the caller answers the same 401 for this as
    for every other authentication failure and an exception per shape of garbage would only be
    a way to accidentally answer differently.
    """
    found = _TOKEN_PATTERN.match(raw)
    if found is None:
        return None
    return ParsedToken(credential_id=uuid.UUID(hex=found.group(2)), secret=found.group(3))


def hash_secret(secret: str) -> str:
    """Turn secret material into the verifier that is stored in its place.

    SHA-256 and deliberately not a password hash, which is the one cryptographic decision in
    this module and is worth the paragraph.

    A password hash exists to make guessing expensive, because people choose passwords out of a
    space small enough to search. This secret comes out of `secrets.token_hex(32)`: 256 bits,
    uniformly distributed, chosen by the operating system. There is no dictionary, no reuse
    across sites and no human pattern to exploit, so the search space is the whole space and
    slowing an attacker down by four orders of magnitude changes an infeasible search into an
    infeasible search. What it does change is that every authenticated request pays a
    derivation, on the request path, forever.

    There is no salt for the same reason. A salt defeats precomputation, and precomputing
    against a 256 bit uniform space is not a thing anybody can do.

    The algorithm is written into the stored value rather than assumed, so this can be replaced
    without guessing what the existing rows hold.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"


def verify_secret(secret: str, stored: str) -> bool:
    """Whether this secret is the one that produced this verifier.

    `compare_digest` rather than `==`. The comparison is between a caller supplied value and a
    stored one, which is exactly the shape a timing side channel needs, and the constant time
    version costs nothing.

    A verifier written by an algorithm this does not know answers False rather than raising.
    That is the safe direction: an unreadable verifier authenticates nobody, and a credential
    that cannot be verified is a credential that does not work rather than a request that
    fails with a server error.
    """
    algorithm, separator, digest = stored.partition(":")
    if separator != ":" or algorithm != HASH_ALGORITHM:
        return False
    return hmac.compare_digest(hashlib.sha256(secret.encode("utf-8")).hexdigest(), digest)
