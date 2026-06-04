"""Tests for testimonials API endpoints."""

from rest_framework import status
from rest_framework.test import APITestCase

from enterprise_access.apps.testimonials.models import Testimonial


class TestimonialAPITestCase(APITestCase):
    """Tests for the testimonials API endpoint."""

    def setUp(self):
        # Create active testimonial
        self.active_testimonial = Testimonial.objects.create(
            quote_text="Active testimonial",
            attribution_name="Ganesh",
            attribution_title="Software Engineer",
            is_active=True,
        )

        # Create inactive testimonial
        self.inactive_testimonial = Testimonial.objects.create(
            quote_text="Inactive testimonial",
            attribution_name="Hidden User",
            attribution_title="Manager",
            is_active=False,
        )

        self.url = "/api/v1/testimonials/"

    def test_allows_unauthenticated_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_returns_only_active_testimonials(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Pagination envelope should be present
        self.assertIn("count", response.data)
        self.assertIn("results", response.data)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)

        results = response.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["attribution_name"], "Ganesh")

    def test_excludes_inactive_testimonials(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        names = [item["attribution_name"] for item in response.data.get("results", [])]
        self.assertNotIn("Hidden User", names)

    def test_response_structure(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        result = response.data.get("results", [])[0]

        self.assertIn("uuid", result)
        self.assertIn("quote_text", result)
        self.assertIn("attribution_name", result)
        self.assertIn("attribution_title", result)
