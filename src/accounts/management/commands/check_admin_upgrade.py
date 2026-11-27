"""Check whether existing admin usernames can use case-insensitive uniqueness."""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.db.models.functions import Upper

from accounts.models import User


class Command(BaseCommand):
    help = "Check for admin usernames that differ only in case."

    def handle(self, *args: object, **options: object) -> None:
        collisions = (
            User.objects.order_by()
            .values(normalized=Upper("username"))
            .annotate(total=Count("pk"))
            .filter(total__gt=1)
        )
        if collisions.exists():
            raise CommandError(
                "Admin usernames differ only in case. "
                "Give each account a distinct username before applying the upgrade."
            )
