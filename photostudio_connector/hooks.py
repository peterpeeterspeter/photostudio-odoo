"""Fresh-install defaults for Photostudio Connector."""


def post_init_hook(env):
    params = env["ir.config_parameter"].sudo()
    params.set_param("photostudio_connector.use_extra_media", "False")
    params.set_param("photostudio_connector.use_webhook", "False")
    if params.get_param("photostudio_connector.priority") == "high":
        params.set_param("photostudio_connector.priority", "normal")
