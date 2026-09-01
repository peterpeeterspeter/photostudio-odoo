def migrate(cr, version):
    cr.execute(
        """
        UPDATE photostudio_job
           SET writeback_state = CASE
               WHEN status IN ('failed', 'cancelled') THEN 'failed'
               WHEN status = 'completed'
                    AND jsonb_typeof(COALESCE(metadata->'attached_output_ids', 'null'::jsonb)) = 'array'
                    AND jsonb_array_length(metadata->'attached_output_ids') > 0
               THEN 'attached'
               ELSE writeback_state
           END
        """
    )
