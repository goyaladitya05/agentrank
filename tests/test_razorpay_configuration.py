"""What may be configured for Razorpay, and what may not.

Two properties, and the second one is why this file exists at all.

The first is ordinary: a half configured key pair fails at startup naming the missing variable
rather than on the first request that needs it.

The second is that this project has no live mode, and that is enforced rather than intended. A
live key cannot be loaded into the process. There is no environment variable that relaxes it, no
request field that reaches it and no command line flag that turns it off, so no code path in the
application can reach a live payment however wrong the rest of it is. A test is the right place
for that claim, because a comment saying "test mode only" is not a control.

The third thing checked here is that a secret behaves like one: masked in a repr, masked in a
model dump, and reachable only through the explicit accessor a caller has to type out.
"""

import pytest
from pydantic import SecretStr, ValidationError

from agentrank_api.config import RAZORPAY_TEST_KEY_PREFIX, Settings

TEST_KEY_ID = "rzp_test_0123456789abcd"
LIVE_KEY_ID = "rzp_live_0123456789abcd"
KEY_SECRET = "not-a-real-secret"


def settings_with(**overrides: object) -> Settings:
    """Settings built from explicit values rather than from the process environment.

    `_env_file=None` so a developer's own `.env` cannot make this pass or fail. The point of
    these tests is what the model accepts, not what happens to be on this machine.
    """
    fields: dict[str, object] = {
        "POSTGRES_PASSWORD": "test",
        "_env_file": None,
    }
    return Settings(**(fields | overrides))  # type: ignore[arg-type]


def test_razorpay_is_optional() -> None:
    """An unconfigured integration is an ordinary state, not a startup failure.

    Every existing payment path uses the deterministic fake provider and works without any of
    this. Refusing to start would make an optional integration mandatory, which would break
    every environment that has no reason to talk to Razorpay, CI included.
    """
    assert settings_with().razorpay is None


def test_a_test_mode_key_pair_is_accepted() -> None:
    credentials = settings_with(
        RAZORPAY_KEY_ID=TEST_KEY_ID, RAZORPAY_KEY_SECRET=KEY_SECRET
    ).razorpay

    assert credentials is not None
    assert credentials.key_id == TEST_KEY_ID
    assert credentials.key_secret.get_secret_value() == KEY_SECRET


def test_a_live_key_is_refused() -> None:
    """The structural block. A live credential cannot enter the process.

    Razorpay puts the mode in the key identifier, so this is checkable without a network call
    and without trusting anything the operator wrote elsewhere. Deleting this check is what it
    should take to enable real money, and deleting it is a visible act.
    """
    with pytest.raises(ValidationError) as refused:
        settings_with(RAZORPAY_KEY_ID=LIVE_KEY_ID, RAZORPAY_KEY_SECRET=KEY_SECRET)

    assert RAZORPAY_TEST_KEY_PREFIX in str(refused.value)


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        ({"RAZORPAY_KEY_ID": TEST_KEY_ID}, "RAZORPAY_KEY_SECRET"),
        ({"RAZORPAY_KEY_SECRET": KEY_SECRET}, "RAZORPAY_KEY_ID"),
    ],
)
def test_half_a_key_pair_is_refused_by_name(present: dict[str, str], missing: str) -> None:
    with pytest.raises(ValidationError) as refused:
        settings_with(**present)

    assert missing in str(refused.value)


def test_a_non_positive_timeout_is_refused() -> None:
    """A zero timeout is not "no timeout", it is a client that gives up before it starts."""
    with pytest.raises(ValidationError):
        settings_with(
            RAZORPAY_KEY_ID=TEST_KEY_ID, RAZORPAY_KEY_SECRET=KEY_SECRET, RAZORPAY_TIMEOUT_SECONDS=0
        )


def test_the_secret_does_not_appear_in_a_repr_or_a_dump() -> None:
    """The one property that has to hold in every accidental place a value can end up.

    A settings object gets printed in a traceback, logged at startup, and serialized by a
    debugging endpoint somebody adds later. `SecretStr` is what makes all three safe at once,
    and the accessor being explicit is what makes the two deliberate unwrappings findable.
    """
    settings = settings_with(RAZORPAY_KEY_ID=TEST_KEY_ID, RAZORPAY_KEY_SECRET=KEY_SECRET)

    assert KEY_SECRET not in repr(settings)
    assert KEY_SECRET not in str(settings)
    assert KEY_SECRET not in str(settings.model_dump())
    credentials = settings.razorpay
    assert credentials is not None
    assert KEY_SECRET not in repr(credentials)
    assert isinstance(credentials.key_secret, SecretStr)


def test_the_base_url_loses_a_trailing_slash() -> None:
    """Because the client joins paths onto it, and two slashes is a different URL."""
    credentials = settings_with(
        RAZORPAY_KEY_ID=TEST_KEY_ID,
        RAZORPAY_KEY_SECRET=KEY_SECRET,
        RAZORPAY_API_BASE_URL="https://api.razorpay.com/v1/",
    ).razorpay

    assert credentials is not None
    assert credentials.api_base_url == "https://api.razorpay.com/v1"


def test_a_model_provider_credential_is_optional() -> None:
    """No provider configured is an ordinary state: nothing in the API needs one to answer."""
    configured = settings_with()
    assert configured.openai is None
    assert configured.gemini is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_model_provider_credential_counts_as_absent(blank: str) -> None:
    """A key of zero length is not a key, and a deployment that sets one empty means it.

    `Settings` reads a `.env` file, so emptying the variable in the environment is the only way
    a machine that has a key on disk can say it does not want one used. Reading a blank value as
    configured would send a benchmark to a provider that then refuses it, which is a real run
    spent on nothing.
    """
    configured = settings_with(OPENAI_API_KEY=blank, GEMINI_API_KEY=blank)
    assert configured.openai is None
    assert configured.gemini is None


def test_a_real_model_provider_credential_is_carried_as_a_secret() -> None:
    configured = settings_with(OPENAI_API_KEY="not-a-real-key")
    credentials = configured.openai
    assert credentials is not None
    assert credentials.api_key.get_secret_value() == "not-a-real-key"
    assert "not-a-real-key" not in repr(configured)
