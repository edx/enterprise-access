from rest_framework import serializers

from enterprise_access.apps.testimonials.models import Testimonial


class TestimonialSerializer(serializers.ModelSerializer):
    class Meta:
        model = Testimonial
        fields = (
            "uuid",
            "quote_text",
            "attribution_name",
            "attribution_title",
        )
