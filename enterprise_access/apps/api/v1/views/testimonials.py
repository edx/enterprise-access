from django.db.models.functions import Random
from rest_framework.viewsets import ReadOnlyModelViewSet
from enterprise_access.apps.testimonials.models import Testimonial
from enterprise_access.apps.api.serializers.testimonials import TestimonialSerializer


class TestimonialViewSet(ReadOnlyModelViewSet):
    serializer_class = TestimonialSerializer

    def get_queryset(self):
        return (
            Testimonial.objects
            .filter(is_active=True)
            .order_by(Random())
        )