from django.contrib import admin
from .models import Testimonial


@admin.register(Testimonial)
class TestimonialAdmin(admin.ModelAdmin):
    list_display = (
        "attribution_name",
        "attribution_title",
        "is_active",
        "created",
    )
    list_filter = ("is_active",)
    search_fields = (
        "quote_text",
        "attribution_name",
        "attribution_title",
    )
    ordering = ("-created",)
    readonly_fields = ("created", "modified")