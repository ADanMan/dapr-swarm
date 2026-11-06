"""Small offline checks for the public launch configuration."""
import tempfile
import unittest
from pathlib import Path

from scripts.start import load_settings


class PublicSettingsTests(unittest.TestCase):
    def test_missing_settings_fail_without_reading_hermes(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'BEEGATE_API_KEY'):
                load_settings(Path(directory), {})

    def test_example_cannot_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text(
                'BEEGATE_API_KEY=fixture-only\n'
                'BEEGATE_BASE_URL=https://api.example.invalid/v1\n'
                'BEEGATE_MODEL=your-glm-model-id\n')
            with self.assertRaisesRegex(ValueError, 'real HTTP'):
                load_settings(root, {})

    def test_environment_overrides_file_and_secret_is_generated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('BEEGATE_API_KEY=file-fixture\n')
            settings = {'BEEGATE_API_KEY': 'environment-fixture',
                        'BEEGATE_BASE_URL': 'http://localhost:9999/v1',
                        'BEEGATE_MODEL': 'fixture-model'}
            first = load_settings(root, settings)
            second = load_settings(root, settings)
            self.assertEqual(first['BEEGATE_API_KEY'], 'environment-fixture')
            self.assertEqual(first['BEEGATE_MODEL'], 'fixture-model')
            self.assertEqual(len(first['SEARXNG_SECRET']), 64)
            self.assertNotEqual(first['SEARXNG_SECRET'], second['SEARXNG_SECRET'])
            self.assertNotIn('SEARXNG_SECRET', settings)

    def test_embedded_credentials_are_rejected_without_echoing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as error:
                load_settings(Path(directory), {
                    'BEEGATE_API_KEY': 'fixture-only',
                    'BEEGATE_BASE_URL': 'https://user:private-fixture@example.org/v1',
                    'BEEGATE_MODEL': 'fixture-model'})
            self.assertNotIn('private-fixture', str(error.exception))

    def test_explicit_search_secret_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(Path(directory), {
                'BEEGATE_API_KEY': 'fixture-only',
                'BEEGATE_BASE_URL': 'http://localhost:9999/v1',
                'BEEGATE_MODEL': 'fixture-model',
                'SEARXNG_SECRET': 'test-fixture-not-a-real-secret'})
            self.assertEqual(settings['SEARXNG_SECRET'], 'test-fixture-not-a-real-secret')


if __name__ == '__main__':
    unittest.main()
