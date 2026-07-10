"""hermes-setup plugin entry point.

Thin glue only, per this project's convention (see hermes-kb's
``__init__.py``): wires the ``/setup`` slash command and the
``pre_gateway_dispatch`` wizard hook implemented in ``wizard.py`` into the
``ctx`` the hermes plugin loader hands us. No logic lives here.
"""

from __future__ import annotations

from . import wizard as setup_wizard


def register(ctx) -> None:
    setup_wizard.register(ctx)  # /setup slash command + pre_gateway_dispatch onboarding wizard
