def migrate(cr, version):
    cr.execute(
        """
        UPDATE ir_config_parameter
           SET value = 'False'
         WHERE key = 'photostudio_connector.use_webhook'
        """
    )
    cr.execute(
        """
        DELETE FROM ir_config_parameter
         WHERE key = 'photostudio_connector.default_output'
        """
    )
    cr.execute(
        """
        UPDATE ir_config_parameter
           SET value = 'True'
         WHERE key = 'photostudio_connector.use_extra_media'
        """
    )
    cr.execute(
        """
        INSERT INTO ir_config_parameter (key, value)
        SELECT 'photostudio_connector.use_extra_media', 'True'
         WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter
             WHERE key = 'photostudio_connector.use_extra_media'
         )
        """
    )
    cr.execute(
        """
        UPDATE ir_config_parameter
           SET value = 'normal'
         WHERE key = 'photostudio_connector.priority'
           AND value = 'high'
        """
    )
    cr.execute("DELETE FROM photostudio_webhook_event")
