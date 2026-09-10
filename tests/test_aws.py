"""The one place `aws.py` is exercised directly rather than through `conftest.FakeAws`.

Everything else fakes this module wholesale, which is the right trade for testing the
state machine -- but it means the translation from a raw `describe_instances` response
into an `Instance` is never covered, and that translation is where a rebuilt game server
turns into an `IndexError` instead of an error anything can handle.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from botocore.exceptions import ClientError

from pzbot.aws import INSTANCE_GONE, Aws, AwsError, is_instance_gone


class FakeEc2:
    def __init__(self, response: dict) -> None:
        self.response = response

    def describe_instances(self, **_):
        return self.response


def aws_with(response: dict) -> Aws:
    aws = Aws("us-east-1")
    aws._ec2 = FakeEc2(response)
    return aws


async def test_an_empty_result_is_an_aws_error_not_an_indexerror():
    # An instance terminated moments ago can drop out of DescribeInstances before EC2
    # starts rejecting its id outright. `resp["Reservations"][0]` raised IndexError on
    # that, which is not an `AwsError` -- so it escaped `/pz start`'s `except AwsError`
    # budget gate (turning a deliberate fail-open into a refusal) and the presence loop
    # (killing it, and with it the heartbeat that is supposed to notice).
    aws = aws_with({"Reservations": []})
    with pytest.raises(AwsError) as caught:
        await aws.describe("i-0gone")
    assert is_instance_gone(caught.value)


async def test_a_reservation_with_no_instances_is_the_same_case():
    aws = aws_with({"Reservations": [{"Instances": []}]})
    with pytest.raises(AwsError) as caught:
        await aws.describe("i-0gone")
    assert is_instance_gone(caught.value)


async def test_a_normal_response_still_becomes_an_instance():
    aws = aws_with(
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0real",
                            "State": {"Name": "running"},
                            "PrivateIpAddress": "10.20.1.171",
                            "InstanceType": "m7i.xlarge",
                        }
                    ]
                }
            ]
        }
    )
    instance = await aws.describe("i-0real")
    assert instance.instance_id == "i-0real"
    assert instance.is_running
    assert instance.private_ip == "10.20.1.171"
    assert instance.public_ip == ""  # absent from the response, not None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ClientError({"Error": {"Code": INSTANCE_GONE, "Message": ""}}, "D"), True),
        (ClientError({"Error": {"Code": "AccessDenied", "Message": ""}}, "D"), False),
        (ClientError({}, "D"), False),
        (RuntimeError("not an AWS error at all"), False),
    ],
)
def test_only_the_vanished_instance_code_means_re_discover_me(exc, expected):
    # `server._describe_game` re-points the bot at whatever carries the gameserver tag
    # when this is true. Every other AWS failure must propagate instead.
    assert is_instance_gone(exc) is expected


# --- Presigned downloads -----------------------------------------------------------------

BUCKET = "pz-prod-backups-000000000000"
NAME = "2026-08-22T19-24-30Z__scheduled.tar.zst"
KEY = f"backups/prod/{NAME}"


@pytest.fixture
def signing_creds(monkeypatch):
    """Static credentials, so signing is arithmetic instead of a trip to IMDS."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FQoGZXIvYXdzEXAMPLETOKEN")


async def test_a_download_link_is_signed_for_one_object_and_one_window(signing_creds):
    url = await Aws("us-east-1").presign_backup(BUCKET, KEY, expires_in=900, filename=NAME)
    plain = unquote(url)

    assert url.startswith("https://")
    assert KEY in plain
    assert "X-Amz-Expires=900" in url

    # SigV4, and this assertion is the whole reason this test exists. botocore will
    # presign S3 with SigV2 unless told otherwise, and S3 rejects SigV2 outright on any
    # bucket created after June 2020 -- which `pz-<stack>-backups` is. The link is
    # generated without complaint either way; the difference only shows up as an
    # `InvalidRequest` in an admin's browser, where nothing here can see it.
    assert "X-Amz-Signature=" in url
    assert "AWSAccessKeyId=" not in url  # the SigV2 tell
    # Without the session token the URL is unusable from an instance profile, which is
    # the only way this ever runs in production.
    assert "X-Amz-Security-Token=" in url
    # And the archive lands under its own name rather than under whatever a browser
    # makes of a URL carrying a kilobyte of query string.
    assert f'attachment; filename="{NAME}"' in plain

    # render.download puts this inside a markdown masked link, `[label](url)`, which a
    # literal ")" would truncate -- producing a link that is silently broken rather than
    # obviously absent. botocore percent-encodes the query, so this holds; the assertion
    # is here because the coupling is invisible from either file on its own.
    assert ")" not in url


async def test_signing_asks_for_nothing_the_bot_role_does_not_already_have(signing_creds):
    # `pz-bot-role` grants s3:GetObject on backups/* and nothing else on this bucket.
    # A presigned URL is signed locally and evaluated against that role when it is used,
    # so this must be a plain GET -- an upload or a delete link would be signed happily
    # here and fail confusingly at S3.
    url = await Aws("us-east-1").presign_backup(BUCKET, KEY, expires_in=300)
    assert "X-Amz-SignedHeaders=host" in url
    assert "response-content-disposition" not in url  # omitted when no filename is given
