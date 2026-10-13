"""No model calls: supplemental SVG entry selection and honest gallery state."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from bench.entrypoints import svg_entrypoint
from make_gallery import media_text, project_row


class SvgSupplementTests(unittest.TestCase):
    def test_single_svg_selection_does_not_require_html(self):
        self.assertEqual(svg_entrypoint(['drawing.svg']), 'drawing.svg')
        self.assertEqual(svg_entrypoint(['index.svg', 'other.svg']), 'index.svg')
        self.assertIsNone(svg_entrypoint(['a.svg', 'b.svg']))
        self.assertIsNone(svg_entrypoint(['index.html', '../drawing.svg']))

    def test_length_without_delivery_is_not_a_work_or_score(self):
        run = {'id': '1' * 32, 'model': 'synthetic-fixture', 'tool': 'opencode',
               'task_id': 'crocodile', 'task_name': 'Synthetic fixture',
               'purpose': 'benchmark', 'prompt_version': 'standalone-svg-v1',
               'status': 'completed', 'metrics': {}, 'reviews': [],
               'evaluation': {'status': 'completed', 'evidence': [],
                              'generation_termination_reason': 'length'},
               'checks': [{'name': 'entrypoint', 'status': 'fail'}]}
        descriptor = {'status': 'no_entrypoint', 'entrypoint': None, 'entry_sha256': None,
                      'missing': [], 'policy': 'opaque-srcdoc-v1', 'package': None}
        for version in ['standalone-svg-v1', 'standalone-svg-no-thinking-v2']:
            with self.subTest(version=version):
                row = project_row({**run, 'prompt_version': version},
                                  {'evidence': {}, 'thumbnails': {}, 'assets': {},
                                   'originals': {run['id']: descriptor}})
                self.assertEqual(row['termination'], 'length')
                self.assertEqual(row['entry_status'], 'fail')
                self.assertFalse(row['registered_media'])
                self.assertIsNone(row['image'])
                self.assertNotIn('scores', row)
                self.assertIn('SVG 补测 · length 截断', media_text(row))
                self.assertIn('未交付，没有可展示 SVG/HTML', media_text(row))


if __name__ == '__main__':
    unittest.main()
