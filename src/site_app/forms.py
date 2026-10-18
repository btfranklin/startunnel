"""Browser forms for admin-operated StarTunnel actions."""

from __future__ import annotations

from django import forms

from core.limits import (
    DEFAULT_LIMITS,
    MAX_CREDENTIAL_NAME_CHARACTERS,
    MAX_CYCLE_SECONDS,
    MAX_LABEL_CHARACTERS,
    MAX_SESSION_CHARACTERS,
    MIN_CYCLE_SECONDS,
)


class AgentCredentialForm(forms.Form):
    name = forms.CharField(
        label="Agent name",
        max_length=MAX_CREDENTIAL_NAME_CHARACTERS,
        help_text="Use one clear name for one agent. The complete key appears one time.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "Tutorial sender"}),
    )


class TunnelCycleForm(forms.Form):
    root_session = forms.CharField(
        label="Session label (not verified)",
        max_length=MAX_SESSION_CHARACTERS,
        required=False,
        strip=False,
    )
    cycle_label = forms.CharField(
        label="Cycle label",
        max_length=MAX_LABEL_CHARACTERS,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "Next review"}),
    )
    root_text = forms.CharField(
        label="Root message",
        max_length=DEFAULT_LIMITS.bytes_per_message,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "State the purpose of this cycle."}),
    )
    expires_in_seconds = forms.IntegerField(
        label="Lifetime in seconds",
        required=False,
        min_value=MIN_CYCLE_SECONDS,
        max_value=MAX_CYCLE_SECONDS,
        help_text="Leave this empty to use the instance default.",
    )
