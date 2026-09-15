import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bundle_html', str(ROOT / 'bundle_html.py'))
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class GuideToolsTests(unittest.TestCase):
    def test_bundle_generic_and_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'screen shot.png'
            data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=')
            image.write_bytes(data)
            page = root / 'guide.html'
            page.write_text('<html><img data-image="sample" alt="src=example"><a data-link="docs" href="old">Docs</a><script>var s="keep";</script></html>')
            url = 'https://internal.example/repo?a=1&b="two"'
            self.assertEqual(bundle.main([str(page), '-o', str(page), '--image', 'sample=' + str(image), '--link', 'docs=' + url]), 0)
            result = page.read_text()
            self.assertIn('data:image/png;base64,' + base64.b64encode(data).decode(), result)
            self.assertIn('alt="src=example"', result)
            self.assertIn('&amp;b=&quot;two&quot;', result)
            self.assertIn('<script>var s="keep";</script>', result)
            self.assertEqual(bundle.main([str(page), '-o', str(page), '--link', 'docs=README.html']), 0)
            self.assertIn('href="README.html"', page.read_text())

    def test_invalid_inputs_preserve_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page, output, bad = root / 'in.html', root / 'out.html', root / 'bad.png'
            page.write_text('<img data-image="shot"><a data-link="readme" href="README.md">Read</a>')
            output.write_text('previous output')
            bad.write_bytes(b'not an image')
            for options in [ ['--image', 'unknown=' + str(bad)], ['--image', 'shot=' + str(bad)],
                             ['--link', 'readme=javascript:alert(1)'],
                             ['--readme-url', 'a', '--link', 'readme=b'] ]:
                self.assertEqual(bundle.main([str(page), '-o', str(output)] + options), 1)
                self.assertEqual(output.read_text(), 'previous output')

    def test_intro_all_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'INTRO.html'
            self.assertEqual(bundle.main([str(ROOT / 'INTRO.html'), '-o', str(output),
                '--github-url', 'https://internal.example/converter',
                '--readme-url', 'https://internal.example/converter/blob/main/README.md',
                '--profiler-url', 'https://internal.example/profiler']), 0)
            tags = bundle.Slots(output.read_text()).tags
            self.assertEqual({t[4] for t in tags if t[2] == 'data-image'},
                             {'tarmac', 'cachegrind', 'settings', 'overview', 'sources'})
            self.assertNotIn('https://github.com/', output.read_text())

    @unittest.skipUnless(os.name == 'posix', 'Bash wrapper requires Unix')
    def test_farm_arguments_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / 'args.json'
            launcher = root / 'ud'
            launcher.write_text('#!' + sys.executable + '\nimport sys,json,os\njson.dump(sys.argv[1:],open(os.environ["CAPTURE"],"w"))\nsys.exit(23)\n')
            launcher.chmod(0o755)
            env = os.environ.copy()
            env.update(PATH=str(root) + os.pathsep + env.get('PATH', ''), CAPTURE=str(capture),
                       TARMAC_PYTHON='/python path/python3', TARMAC_SCRIPT='/script path/convert.py')
            args = ['/trace path', '--elf', '/elf path/fw.elf', '--workers', '16', '--pattern', 'tarmac*.log']
            result = subprocess.run(['bash', str(ROOT / 'run_compute_farm.sh')] + args, env=env)
            self.assertEqual(result.returncode, 23)
            self.assertEqual(json.loads(capture.read_text()), [env['TARMAC_PYTHON'], env['TARMAC_SCRIPT']] + args)


if __name__ == '__main__':
    unittest.main()
