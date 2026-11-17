from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('personalized_search', '0003_utf8mb4_tables'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='chatsession',
            index=models.Index(fields=['last_message_at'], name='ps_session_last_message'),
        ),
    ]
