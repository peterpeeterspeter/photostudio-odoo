"""Download errors with HTTP status (no Odoo imports)."""


class PhotostudioDownloadError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code
