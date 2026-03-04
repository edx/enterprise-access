from django.db.models.functions import Random
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ReadOnlyModelViewSet

from enterprise_access.apps.api.serializers.testimonials import TestimonialSerializer
from enterprise_access.apps.testimonials.models import Testimonial


class TestimonialViewSet(ReadOnlyModelViewSet):
    """
    Read-only API endpoint for retrieving active testimonials.

    Returns only testimonials where is_active=True.
    Used by the frontend checkout sidebar to display rotating testimonials.
    """
    serializer_class = TestimonialSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Testimonial.objects
            .filter(is_active=True)
            .order_by(Random())
        )
