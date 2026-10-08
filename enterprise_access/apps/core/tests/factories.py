"""
Factoryboy factories.
"""
import factory
from faker import Faker

from enterprise_access.apps.core.models import User

USER_PASSWORD = 'password'

FAKER = Faker()


class UserFactory(factory.django.DjangoModelFactory):
    """
    Test factory for the `User` model.
    """
    id = factory.Faker('bothify', text='#########')
    # make this pretty random to avoid flaky tests.
    username = factory.Faker('bothify', text='fake-username-???###')
    password = factory.PostGenerationMethodCall('set_password', USER_PASSWORD)
    email = factory.Faker('email')
    first_name = factory.Faker('first_name')
    last_name = factory.Faker('last_name')
    is_active = True
    is_staff = False
    is_superuser = False
    # A sequence, not a random int: FAKER.pyint() draws from 0-9999, so a suite that
    # builds a few users per test collides often enough to break any view looking a
    # user up by lms_user_id. Offset past the ids tests hardcode (max 98123).
    lms_user_id = factory.Sequence(lambda n: 10000000 + n)

    class Meta:
        model = User
