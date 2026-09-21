import base64
import io
import uuid
from unittest.mock import patch

from PIL import Image
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..utils.download_errors import PhotostudioDownloadError


def png():
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(buffer, format='PNG')
    return buffer.getvalue()


@tagged('post_install', '-at_install', 'photostudio_connector')
class TestDownloadRecovery(TransactionCase):
    def setUp(self):
        super().setUp()
        self.service = self.env['photostudio.connector.service']
        self.client = type(self.env['photostudio.client'])
        self.product = self.env['product.template'].create({'name': 'Recovery fixture'})

    def job(self, **values):
        return self.env['photostudio.job'].create({
            'job_id': str(uuid.uuid4()), 'product_tmpl_id': self.product.id,
            'operation': 'product_image.ghost', 'status': 'completed',
            'replace_strategy': 'replace_main',
            'outputs': [{'type': 'image', 'state': 'ready', 'image_id': 'stable-image',
                         'download_url': 'https://allowed.invalid/old?token=expired'}],
            **values,
        })

    def payload(self, job):
        return {'job_id': job.job_id, 'status': 'completed',
                'outputs': [{'type': 'image', 'state': 'ready', 'image_id': 'stable-image',
                             'download_url': 'https://allowed.invalid/fresh?token=new'}]}

    def test_retry_refreshes_signed_url_before_download(self):
        job = self.job(download_attempts=1)
        def download(url):
            if '/old?' in url:
                raise PhotostudioDownloadError('Expired', status_code=403)
            return png()
        with patch.object(self.client, '_get_job', return_value=self.payload(job)) as get, \
                patch.object(self.client, '_download_output', side_effect=download):
            self.service.attach_isolated(job)
        get.assert_called_once_with(job.job_id)
        self.assertEqual(job.writeback_state, 'attached')

    def test_unexpected_writeback_failure_advances_bounded_attempts(self):
        job = self.job()
        with patch.object(self.client, '_download_output', side_effect=ValueError('secret-url')):
            self.service.attach_isolated(job)
        self.assertEqual(job.download_attempts, 1)
        self.assertEqual(job.writeback_state, 'pending')
        self.assertNotIn('secret-url', job.error_message)

    def test_refresh_failure_exhausts_without_stale_download(self):
        job = self.job(download_attempts=self.service.MAX_DOWNLOAD_ATTEMPTS - 1)
        with patch.object(self.client, '_get_job', side_effect=UserError('transport secret')) as get, \
                patch.object(self.client, '_download_output') as download:
            self.service.attach_isolated(job)
        get.assert_called_once()
        download.assert_not_called()
        self.assertEqual(job.writeback_state, 'expired')
        self.assertNotIn('transport secret', job.error_message)

    def test_poll_batch_isolates_bad_writeback_from_next_job(self):
        bad = self.job(batch_id='recovery-batch', status='processing')
        good = self.job(batch_id='recovery-batch', status='processing')
        payload = {'jobs': [self.payload(bad), self.payload(good)]}
        with patch.object(self.client, '_get_batch', return_value=payload), \
                patch.object(self.client, '_download_output', side_effect=[ValueError('bad image'), png()]):
            self.service.poll_batch_isolated('recovery-batch')
        self.assertEqual(bad.status, 'completed')
        self.assertEqual(bad.download_attempts, 1)
        self.assertEqual(good.writeback_state, 'attached')

    def test_manual_retry_uses_get_only_and_preserves_dedup(self):
        job = self.job(writeback_state='expired', download_attempts=self.service.MAX_DOWNLOAD_ATTEMPTS,
                       metadata={'attached_output_ids': ['stable-image']}, main_output_attached=True)
        with patch.object(self.client, '_get_job', return_value=self.payload(job)) as get, \
                patch.object(self.client, '_download_output') as download, \
                patch.object(self.client, '_create_job_batch') as paid:
            job.action_retry_writeback()
        get.assert_called_once_with(job.job_id)
        download.assert_not_called()
        paid.assert_not_called()
        self.assertEqual(job.writeback_state, 'attached')
        self.assertEqual(job.metadata['attached_output_ids'], ['stable-image'])

    def test_manual_retry_rejects_remote_failed_job(self):
        job = self.job(status='failed', writeback_state='failed')
        with patch.object(self.client, '_get_job') as get, self.assertRaises(UserError):
            job.action_retry_writeback()
        get.assert_not_called()

    def test_success_clears_stale_error(self):
        job = self.job(error_message='Download failed (403).')
        with patch.object(self.client, '_download_output', return_value=png()):
            self.service.attach_isolated(job)
        self.assertEqual(job.writeback_state, 'attached')
        self.assertFalse(job.error_message)
