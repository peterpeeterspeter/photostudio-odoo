"""Quarantine legacy jobs without trusted server-recorded authorization context.

Odoo runs the retained 19.0.1.0.2 migration first when upgrading from .1.
Never infer a requester from client-writable metadata or reset attached jobs.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE photostudio_job
           SET authorization_blocked = TRUE,
               writeback_state = CASE WHEN writeback_state = 'attached' THEN 'attached' ELSE 'failed' END,
               error_message = %s
         WHERE (requesting_user_id IS NULL OR company_id IS NULL)

        """,
        (
            "Automatic processing disabled: this legacy job has no trusted "
            "requesting user or company. Contact an administrator; existing "
            "images and job history have been preserved.",
        ),
    )
