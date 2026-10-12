"""Exercise the public synthetic privacy fixture offline; never read operator material."""

from __future__ import annotations

import time

import click

from lychd.domain.privacy.contracts import PreparationPolicy, PrivacyRefused
from lychd.domain.privacy.fixtures import diagnostic_fixture, fixture_binding
from lychd.domain.privacy.preparation import CutAuthority, make_plan, prepare_cut, public_event


@click.command()
@click.option("--show-candidate", is_flag=True, help="Print the sanitized public synthetic fixture after verification.")
def cli(show_candidate: bool) -> None:  # noqa: FBT001 - Click boolean option
    """Prove closed-diagnostic@1 preparation without a model, credentials or network.

    A successful exit proves this synthetic case only. No source file, private
    document, runtime, or remote target can be supplied to this probe.
    """
    now = int(time.time())
    projection, binding = diagnostic_fixture(), fixture_binding()
    policy, authority = PreparationPolicy(), CutAuthority()
    try:
        plan = make_plan(projection, binding, policy, expires_at=now + 300)
        result = prepare_cut(projection, plan, policy, authority, now=now)
        authority.verify_seal(result.cut, result.candidate, plan, binding, policy, now=now)
    except PrivacyRefused:
        message = "Synthetic privacy preparation refused."
        raise click.ClickException(message) from None
    click.echo(public_event().model_dump_json())
    if show_candidate:
        click.echo(result.candidate.decode("utf-8"))


if __name__ == "__main__":
    cli()
