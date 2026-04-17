"""
Django management command to send nudge emails to dormant enrolled enterprise learners.

"""
import logging

import snowflake.connector

from django.conf import settings
from django.core.management import BaseCommand

from enterprise_access.apps.track.segment import track_event

LOGGER = logging.getLogger(__name__)

QUERY = '''
    WITH first_video AS (
        SELECT
            course_id,
            ROW_NUMBER() OVER (
                PARTITION BY course_id
                ORDER BY order_index
            ) AS row_,
            block_type,
            display_name,
            lms_web_url
        FROM
            core_sources.course_structure
        WHERE
            block_type = 'video'
            AND is_visible_to_staff_only = FALSE
        QUALIFY
            row_ = 1
    ),

    not_started AS (
        SELECT
            enr.lms_user_id,
            enr.lms_courserun_key,
            SPLIT_PART(enr.course_key, '+', 1) AS org_name,
            enr.course_title,
            enr.consent_created,
            enr.lms_enrollment_created,
            enr.lms_enrollment_mode
        FROM
            enterprise.ent_base_enterprise_enrollment AS enr
        WHERE
            DATE(enr.lms_enrollment_created) BETWEEN CURRENT_DATE - 13 AND CURRENT_DATE - 7
            AND COALESCE(enr.course_progress, 0) = 0
            AND enr.consent_granted = TRUE
    ),

    course_data AS (
        SELECT
            dcr.courserun_key,
            dcr.start_datetime,
            cmcr.min_effort,
            cmcr.max_effort,
            cmcr.enrollment_count,
            CASE
                WHEN cmcr.pacing_type = 'self_paced' THEN 'Self Paced'
                ELSE 'Instructor Paced'
            END AS pacing_type,
            cmcr.weeks_to_complete,
            'https://prod-discovery.edx-cdn.org/' || cmc.image AS course_image
        FROM
            core.dim_courseruns AS dcr
        LEFT JOIN
            discovery.course_metadata_courserun AS cmcr
            ON dcr.courserun_key = cmcr.key
        LEFT JOIN
            discovery.course_metadata_course AS cmc
            ON cmcr.course_id = cmc.id
        WHERE
            cmcr.draft = FALSE
            AND cmc.draft = FALSE
    )

    SELECT
        not_started.lms_user_id AS external_id,
        not_started.org_name,
        not_started.course_title,
        course_data.enrollment_count,
        course_data.min_effort,
        course_data.max_effort,
        course_data.weeks_to_complete,
        course_data.pacing_type,
        CASE
            WHEN pacing_type = 'Instructor Paced' THEN 'Led on a course schedule'
            ELSE 'Move at your speed'
        END AS pacing_subtitle,
        COALESCE(
            CASE
                WHEN first_video.display_name = 'Video' THEN 'First Video'
                ELSE first_video.display_name
            END,
            'First Video'
        ) AS display_name,
        course_data.course_image,
        first_video.lms_web_url,
        'https://courses.edx.org/courses/' || not_started.lms_courserun_key || '/discussion/forum/' AS discussion_link,
        'https://learning.edx.org/course/' || not_started.lms_courserun_key || '/home' AS home_link
    FROM
        not_started
    LEFT JOIN
        first_video
        ON not_started.lms_courserun_key = first_video.course_id
    LEFT JOIN
        course_data
        ON not_started.lms_courserun_key = course_data.courserun_key
    WHERE
        course_data.start_datetime <= CURRENT_DATE
        AND first_video.lms_web_url IS NOT NULL
    QUALIFY
        ROW_NUMBER() OVER (
            PARTITION BY not_started.lms_user_id
            ORDER BY lms_enrollment_created DESC
        ) = 1
'''


class Command(BaseCommand):
    """
    Send nudge emails to dormant enrolled enterprise learners.

    Connects to Snowflake, identifies enterprise learners who enrolled
    7-13 days ago but have zero course progress, then emits Segment events
    that trigger Braze nudge emails.

    Example usage:
        ./manage.py nudge_dormant_enrolled_enterprise_learners
        ./manage.py nudge_dormant_enrolled_enterprise_learners --no-commit
    """
    help = 'Send nudge emails to dormant enrolled enterprise learners via Segment/Braze.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-commit',
            action='store_true',
            dest='no_commit',
            default=False,
            help='Dry run: log messages without emitting Segment events.',
        )

    def get_query_results_from_snowflake(self):
        """
        Connect to Snowflake, execute the dormant-learner query, and yield each row.
        """
        ctx = snowflake.connector.connect(
            user=settings.SNOWFLAKE_SERVICE_USER,
            password=settings.SNOWFLAKE_SERVICE_USER_PASSWORD,
            account=getattr(settings, 'SNOWFLAKE_ACCOUNT', 'edx.us-east-1'),
            database=getattr(settings, 'SNOWFLAKE_DATABASE', 'prod'),
        )
        cs = ctx.cursor()
        try:
            cs.execute(QUERY)
            rows = cs.fetchall()
            yield from rows
        finally:
            cs.close()
        ctx.close()

    def emit_event(self, **kwargs):
        """
        Emit the Segment event that Braze uses to trigger the nudge email.
        """
        track_event(
            kwargs['EXTERNAL_ID'],
            'edx.bi.enterprise.user.dormant.nudge',
            kwargs,
        )
        LOGGER.info(
            '[Dormant Nudge] Segment event fired for nudge email to dormant enrolled enterprise learners. '
            'LMS User Id: {user_id}, Organization Name: {org_name}, Course Title: {course_title}'.format(
                user_id=kwargs['EXTERNAL_ID'],
                org_name=kwargs['ORG_NAME'],
                course_title=kwargs['COURSE_TITLE']
            )
        )

    def handle(self, *args, **options):
        should_commit = not options['no_commit']
        event_count = 0

        LOGGER.info('[Dormant Nudge] Process started.')
        for next_row in self.get_query_results_from_snowflake():
            message_data = {
                'EXTERNAL_ID': next_row[0],
                'ORG_NAME': next_row[1],
                'COURSE_TITLE': next_row[2],
                'ENROLLMENT_COUNT': next_row[3],
                'MIN_EFFORT': next_row[4],
                'MAX_EFFORT': next_row[5],
                'WEEKS_TO_COMPLETE': next_row[6],
                'PACING_TYPE': next_row[7],
                'PACING_SUBTITLE': next_row[8],
                'DISPLAY_NAME': next_row[9],
                'COURSE_IMAGE': next_row[10],
                'LMS_WEB_URL': next_row[11],
                'DISCUSSION_LINK': next_row[12],
                'HOME_LINK': next_row[13],
            }
            if should_commit:
                self.emit_event(**message_data)
                event_count += 1
            else:
                LOGGER.info(
                    '[Dormant Nudge] [DRY RUN] Would send event for LMS User Id: %s, Course: %s',
                    message_data['EXTERNAL_ID'],
                    message_data['COURSE_TITLE'],
                )

        LOGGER.info(
            '[Dormant Nudge] Execution completed. Events emitted: %d (commit=%s)',
            event_count,
            should_commit,
        )
