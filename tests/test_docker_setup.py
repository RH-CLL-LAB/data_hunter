"""The Docker setup stays an environment image around the mounted checkout."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml=None


class DockerSetupTests(unittest.TestCase):
    def test_image_installs_dependencies_and_nothing_else(self):
        dockerfile=(ROOT/'Dockerfile').read_text()
        self.assertIn('FROM python:3.12-slim',dockerfile);self.assertIn('requirements.txt',dockerfile);self.assertIn('nodejs',dockerfile)
        self.assertNotIn('COPY . ',dockerfile);self.assertNotIn('OPENAI_API_KEY',dockerfile)
        ignore=[l.strip() for l in (ROOT/'.dockerignore').read_text().splitlines() if l.strip() and not l.startswith('#')]
        self.assertEqual(ignore[:2],['*','!requirements.txt'])

    @unittest.skipUnless(yaml,'PyYAML not installed')
    def test_services_mount_the_checkout_and_isolate_the_network(self):
        compose=yaml.safe_load((ROOT/'compose.yaml').read_text());services=compose['services']
        # The MedScrape checkout also ships the pipeline services; the Data Hunter release does not.
        self.assertTrue({'build-test','site','download-agent'}<=set(services));self.assertTrue(set(services)<={'build-test','site','pipeline','pipeline-local-model','download-agent'})
        for name,service in services.items():
            self.assertTrue(any(v.startswith('.:/app') for v in service['volumes']),name)
            self.assertEqual(service['working_dir'],'/app',name)
        self.assertEqual(services['build-test']['network_mode'],'none')
        if 'pipeline' in services:self.assertEqual(services['pipeline']['network_mode'],'none');self.assertEqual(services['pipeline-local-model']['network_mode'],'host')
        self.assertIn('.:/app:ro',services['site']['volumes']);self.assertEqual(services['site']['ports'],['127.0.0.1:8767:8767'])
        self.assertNotIn('network_mode',services['download-agent'])
        for name in ['pipeline','pipeline-local-model','download-agent']:
            if name in services:self.assertTrue(services[name].get('profiles'),name)


if __name__=='__main__':unittest.main()
