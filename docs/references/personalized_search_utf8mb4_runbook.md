# Personalized Search utf8mb4 migration: deploy, rollback and regression runbook

This runbook covers migration `personalized_search/0003_utf8mb4_tables`. It converts the three Personalized Search tables to `utf8mb4`, so a learner can type an emoji or other 4-byte character without the turn failing with HTTP 500 (MySQL error 1366, "Incorrect string value").

## What the migration does

- It runs on MySQL only; on any other database engine it does nothing.
- It converts only `personalized_search_chatsession`, `personalized_search_chatturn` and `personalized_search_itemfeedback`, and only the ones whose table collation isn't `utf8mb4` already. A table that is already `utf8mb4` is left alone, so a second run does nothing.
- The conversion is `ALTER TABLE … CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`, one table at a time.
- `FOREIGN_KEY_CHECKS` is set to 0 for the migration's own database connection only, because `chatturn` and `itemfeedback` reference `chatsession`'s key and their character sets change one table at a time. It's set back to 1 in a `finally` block. It is never changed globally.
- No other table is touched, and nothing outside this app has a foreign key to these tables.
- Reversing the migration does nothing to the tables. Converting back to `utf8mb3` would fail on any stored emoji; see [Full rollback of the character set](#full-rollback-of-the-character-set).
- Django already connects to MySQL with `charset: utf8mb4`, so only the tables needed to change.

## Before the first deploy to an environment

Run these read-only queries on the environment's enterprise-access database (a read replica is fine):

```sql
SELECT @@version, @@innodb_default_row_format, @@character_set_server, @@collation_server;
SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE();
SELECT ROW_FORMAT, TABLE_COLLATION, COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() GROUP BY 1, 2;
SELECT TABLE_NAME, TABLE_ROWS, TABLE_COLLATION FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'personalized_search_%';
```

| Result | Meaning | Action |
|---|---|---|
| Version 8.0.11 or later | Required by Django 5.2 anyway | None |
| `innodb_default_row_format` is `dynamic` (or the tables are `Dynamic` / `Compressed`) | Indexes can be up to 3,072 bytes. The largest one here, `ps_feedback_unique_per_learner`, is 1,216 bytes in utf8mb4 (913 in utf8mb3, so 0001 has the same requirement) | None |
| Row format `compact` or `redundant` | The 767-byte index limit applies: 0001 or 0003 fails with error 1071, "Specified key was too long" | Stop. Get the row format changed to `DYNAMIC` before deploying |
| Database default already `utf8mb4` | 0001 creates the tables as utf8mb4, so 0003 does nothing | None |
| Database default `utf8mb3` | 0003 converts the three tables. They end up on `utf8mb4_unicode_ci` while the rest of the database stays on `utf8mb3_general_ci`; see [Mixed collations](#mixed-collations) | None for the deploy |
| No `personalized_search_` tables | First deploy: 0001 creates them empty, so 0003 converts empty tables almost instantly | None |
| The tables exist with rows | Each conversion copies the table and blocks writes to it while it runs (seconds for thousands of rows) | Deploy when the feature is quiet, or with the feature flag off |

## During and after the deploy

The migration runs in the normal migrate step. To check it afterwards (read-only):

```sql
SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'personalized_search_%';
-- all three: utf8mb4_unicode_ci (or another utf8mb4_* collation if the database default was already utf8mb4)
SELECT TABLE_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE
 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'personalized_search_%' AND REFERENCED_TABLE_NAME IS NOT NULL;
-- two rows: chatturn.session_id and itemfeedback.session_id -> personalized_search_chatsession
```

Smoke test: with the feature enabled for a test learner, send a message that is only an emoji (for example 😀). The reply should ask what the learner wants to learn, and the turn should not return HTTP 500.

## If the migrate step fails

| Error | Cause | Fix |
|---|---|---|
| 1071 "Specified key was too long" | The COMPACT or REDUNDANT row format | Change the row format to `DYNAMIC`, then re-run migrate |
| 1205 "Lock wait timeout" or 3024/1317 (query interrupted) | Something held a lock on a table during the copy, or the connection dropped | Re-run migrate. It converts only the tables that aren't utf8mb4 yet |
| 1062 "Duplicate entry … for key 'ps_feedback_unique_per_learner'" | Two `item_id` values that differed under the old collation compare equal under `utf8mb4_unicode_ci` (unlikely: the values are course keys and UUIDs) | Find the pair with the query below, delete the older row, then re-run migrate |
| Any error after one or two tables converted | A partly finished run | Re-run migrate (see below) |

A partly finished run is safe to leave briefly. The migration isn't recorded as applied, so the next migrate converts only what's left. Writes keep working in the mixed state: on MySQL 8.0.33, a child table on `utf8mb3` still accepts inserts that reference a parent already converted to `utf8mb4`.

To find duplicates the new collation would create:

```sql
SELECT lms_user_id, enterprise_customer_uuid, kind, item_id COLLATE utf8mb4_unicode_ci AS item, COUNT(*)
  FROM personalized_search_itemfeedback
 GROUP BY lms_user_id, enterprise_customer_uuid, kind, item
HAVING COUNT(*) > 1;
```

## Rollback

Choose the smallest step that fixes the problem.

### 1. Roll back the code only (normal case)

Deploy the previous release. **No database action is needed.** The tables stay `utf8mb4`, which the previous code handles: Django connects as utf8mb4, and utf8mb4 holds every character utf8mb3 can. The migration stays recorded as applied, which is harmless.

### 2. Unrecord the migration (rarely useful)

`./manage.py migrate personalized_search 0002` runs the reverse, which does nothing to the tables. It only removes 0003 from `django_migrations`. The tables stay `utf8mb4`, and the next deploy re-applies 0003, which does nothing because the tables are already converted. Use it only to make the migration history match a release that doesn't contain 0003.

### Full rollback of the character set

Only do this if `utf8mb4` itself causes a problem. Converting back loses or rejects every 4-byte character, so:

1. **Make sure the code can't re-convert.**
   - Deploy a release where 0003 is replaced by a no-op migration with the same name.
   - Or deploy a release from before 0003, and unrecord it as in step 2.
2. **Find the rows holding 4-byte characters** (read-only):
   ```sql
   SELECT 'chatsession', COUNT(*) FROM personalized_search_chatsession
    WHERE LENGTH(first_prompt) <> LENGTH(CONVERT(first_prompt USING utf8mb3))
       OR LENGTH(title) <> LENGTH(CONVERT(title USING utf8mb3))
   UNION ALL
   SELECT 'chatturn', COUNT(*) FROM personalized_search_chatturn
    WHERE LENGTH(user_message) <> LENGTH(CONVERT(user_message USING utf8mb3))
       OR LENGTH(reply_text) <> LENGTH(CONVERT(reply_text USING utf8mb3));
   ```
3. **Decide what to do with those rows**, with the data owner. Either replace the characters (`UPDATE … SET col = CONVERT(col USING utf8mb3)` turns each into `?`), or delete those sessions through Django (`ChatSession.objects.filter(…).delete()`), which also deletes their turns and clears feedback links. The cascade is Django's, not the database's, so a raw SQL `DELETE` on `personalized_search_chatsession` is blocked by the foreign keys.
4. **Convert the tables back**, children first, in one session:
   ```sql
   SET FOREIGN_KEY_CHECKS = 0;
   ALTER TABLE personalized_search_itemfeedback CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;
   ALTER TABLE personalized_search_chatturn CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;
   ALTER TABLE personalized_search_chatsession CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;
   SET FOREIGN_KEY_CHECKS = 1;
   ```
5. **Accept the old failure.** Emoji messages return HTTP 500 again, the bug 0003 fixed. Keep the feature flag narrow until there's a fix that doesn't need utf8mb4 (for example, stripping 4-byte characters before saving).

## Regression: the emoji error comes back

**Symptoms:** a turn returns HTTP 500, and the logs show MySQL 1366 "Incorrect string value: '\xF0\x9F…' for column …" on a `personalized_search_*` table.

**Likely causes:**
- **A new table or column in this app**, created on a database whose default is `utf8mb3`. Columns get the database default; 0003 only converted the three tables that existed then.
- **A migration squash dropped 0003.** It is `elidable=True`, so a squash leaves it out, and a fresh utf8mb3 database never converts.
- **A restore or copy recreated the tables with the database default** (for example a dump without `CHARACTER SET` clauses).

**Fix:**
1. Confirm the cause: run the collation query from [During and after the deploy](#during-and-after-the-deploy). Any `utf8mb3` table or column in this app is the cause.
2. Add a new migration that converts the affected tables, following 0003's pattern (MySQL only, skip tables already utf8mb4). Don't edit 0003: it has already run where it's recorded.
3. For a squash, drop `elidable=True` from the conversion (or keep 0003 out of the squash), so new databases still convert.

## Mixed collations

When the database default is `utf8mb3`, these three tables end up on `utf8mb4_unicode_ci` and every other enterprise-access table stays on `utf8mb3_general_ci`.

- **Django is unaffected:** it never joins these tables to other apps' tables.
- **Hand-written SQL that compares their text columns with another table's still returns correct results.** MySQL converts the utf8mb3 side. It can't use an index on the converted side, though, so large joins run as hash joins (checked on MySQL 8.0.33 against `subsidy_access_policy_subsidyaccesspolicy.enterprise_customer_uuid`).
- **To keep the same collation family as the rest of the database,** use `utf8mb4_general_ci` instead of `utf8mb4_unicode_ci`. Like any change of this kind, it would be a new migration once 0003 has run anywhere.
