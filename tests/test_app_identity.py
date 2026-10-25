"""The app opts into the shell-owned, registry-backed home-screen identity."""
import base64
import json
from pathlib import Path
import re
import unittest
import zlib
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
HOOK = '/wizard-os-home/core/app-identity.js'

class IdentityHook(unittest.TestCase):
    def test_deferred_same_origin_identity_hook_in_head(self):
        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.hooks = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'script' and attrs.get('src') == HOOK:
                    self.hooks.append(attrs)
        html = (ROOT / 'web/index.html').read_text()
        parser = Scripts()
        parser.feed(html.split('</head>')[0])
        self.assertEqual(len(parser.hooks), 1)
        self.assertIn('defer', parser.hooks[0])

    def test_inline_recipe_contains_exact_current_page(self):
        recipe = ROOT / 'runtipi/docker-compose.yml'
        text = recipe.read_text()
        match = re.search(r"base64.b64decode\('([A-Za-z0-9+/=]+)'\)", text)
        if match is None:
            self.skipTest('Image-based app; no inline source bundle')
        payload = base64.b64decode(match[1])
        if 'zlib.decompress' in text:
            payload = zlib.decompress(payload)
        files = json.loads(payload)
        self.assertEqual(files['web/index.html'], (ROOT / 'web/index.html').read_text())

if __name__ == '__main__':
    unittest.main()
