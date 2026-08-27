"""The one place `aws.py` is exercised directly rather than through `conftest.FakeAws`.

Everything else fakes this module wholesale, which is the right trade for testing the
state machine -- but it means the translation from a raw `describe_instances` response
into an `Instance` is never covered, and that translation is where a rebuilt game server
turns into an `IndexError` instead of an error anything can handle.
"""

from __future__ import annotations

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
