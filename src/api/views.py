"""Self-hosted API reference page."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def api_docs(request: HttpRequest) -> HttpResponse:
    return render(request, "api/docs.html")
