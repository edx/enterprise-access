from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from rest_framework import status
from enterprise_access.apps.testimonials.models import Testimonial


User = get_user_model()


class TestimonialAPITestCase(APITestCase):

    def setUp(self):
        # Create test user and grant staff privileges so IsAdminUser permission passes
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123"
        )
        self.user.is_staff = True
        self.user.save()

        # Authenticate user via DRF client; providing a dummy token helps bypass JWT checks
        self.client.force_authenticate(user=self.user, token="test-token")

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
