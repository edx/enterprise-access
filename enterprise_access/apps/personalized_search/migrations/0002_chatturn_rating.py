from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('personalized_search', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatturn',
            name='rating',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
    ]
