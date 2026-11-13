"""
Store 4-byte characters (emoji, some CJK) in the Personalized Search tables (test-plan N-05).

On a MySQL database whose default character set is ``utf8mb3`` (devstack's is), saving an emoji to
``ChatSession.first_prompt`` fails with error 1366 and the turn returns HTTP 500. This converts the app's three tables
to ``utf8mb4``, on MySQL only and only where a table isn't ``utf8mb4`` already, so it's a no-op on a database that
already uses it and on any other engine. The reverse is a no-op: going back to ``utf8mb3`` would fail on stored emoji.
"""
from django.db import migrations

TABLES = ('personalized_search_chatsession', 'personalized_search_chatturn', 'personalized_search_itemfeedback')


def to_utf8mb4(apps, schema_editor):  # pylint: disable=unused-argument
    connection = schema_editor.connection
    if connection.vendor != 'mysql':
        return
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() '
            f"AND TABLE_NAME IN ({', '.join(['%s'] * len(TABLES))}) AND TABLE_COLLATION NOT LIKE 'utf8mb4%%'",
            list(TABLES),
        )
        pending = [row[0] for row in cursor.fetchall()]
        if not pending:
            return
        # ChatTurn and ItemFeedback reference ChatSession's key: its charset changes while theirs do, so foreign
        # key checks are off for the conversion only.
        cursor.execute('SET FOREIGN_KEY_CHECKS = 0')
        try:
            for table in pending:
                cursor.execute(f'ALTER TABLE `{table}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        finally:
            cursor.execute('SET FOREIGN_KEY_CHECKS = 1')


class Migration(migrations.Migration):

    dependencies = [
        ('personalized_search', '0002_chatturn_rating'),
    ]

    operations = [
        migrations.RunPython(to_utf8mb4, migrations.RunPython.noop, elidable=True),
    ]
