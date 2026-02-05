""" Testimonial models. """

from uuid import uuid4

from django.db import models
from django_extensions.db.models import TimeStampedModel


class Testimonial(TimeStampedModel):
    """
    Stores customer testimonials for checkout sidebar display.

    .. pii: Stores testimonial attribution name and title.
    .. pii_types: name
    .. pii_retirement: local_api
    """

    uuid = models.UUIDField(primary_key=True, default=uuid4, editable=False)

    quote_text = models.TextField()
    attribution_name = models.CharField(max_length=255)
    attribution_title = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return f"Testimonial({self.attribution_name})"
