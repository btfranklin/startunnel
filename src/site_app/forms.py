"""Browser forms for admin-operated StarTunnel actions."""

from __future__ import annotations

from django import forms


class AgentCredentialForm(forms.Form):
    name = forms.CharField(
        label="Agent name",
        max_length=100,
        help_text="Use one clear name for one agent. The complete key appears one time.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "Tutorial sender"}),
    )


class TunnelCycleForm(forms.Form):
    cycle_label = forms.CharField(
        label="Cycle label",
        max_length=160,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "Next review"}),
    )
    root_text = forms.CharField(
        label="Root message",
        max_length=65_536,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "State the purpose of this cycle."}),
    )
    expires_in_seconds = forms.IntegerField(
        label="Lifetime in seconds",
        required=False,
        min_value=300,
        max_value=604_800,
        help_text="Leave this empty to use the instance default.",
    )
