"""Every control of the generated site explains itself: a title from HINTS, or a ⓘ toggle with its explanation."""
import importlib.util
from pathlib import Path
import unittest

CODE_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hints', CODE_ROOT / 'Scripts/dataset_browser/hints.py')
hints = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hints)


class HintTests(unittest.TestCase):
    def test_controls_get_the_title_their_attribute_names(self):
        html = hints.hinted('<button type="button" class="agent-save" data-save-agent>Download…</button><select data-sort aria-label="Sort"><option>a</option></select>')
        self.assertIn('<button title="Open the download options', html)
        self.assertIn('<select title="Order the table', html)

    def test_value_keys_tell_radios_apart_and_keys_match_whole_attributes(self):
        html = hints.hinted('<input type="radio" name="agent-download-scope" value="full"><input type="radio" name="agent-download-scope" value="minimal">'
                            '<button data-dataset-action="copy" data-held-action>Copy files</button><button data-dataset-action="remove">Plan removal…</button>')
        self.assertIn('<input title="Resolve and acquire each complete provider release', html)
        self.assertIn('<input title="Acquire the reviewed minimal useful package', html)
        self.assertIn('<button title="Save a task that makes a versioned working copy', html)
        self.assertIn('<button title="Save a removal plan only', html)
        self.assertIsNone(hints.hint_for('data-exportable data-sorting'), 'a longer attribute never matches a shorter key')

    def test_existing_titles_unknown_controls_and_plain_text_inputs_are_left_alone(self):
        source = '<button data-export title="mine">x</button><button data-unknown>y</button><input name="dataset_name" maxlength="160"><a href="x.html">link</a>'
        self.assertEqual(hints.hinted(source), source)

    def test_titles_are_escaped_and_the_help_toggle_matches_the_filter_panel(self):
        hints.HINTS['data-test-quote'] = 'Says "hi" & <bye>'
        try:
            self.assertEqual(hints.hinted('<button data-test-quote>q</button>'), '<button title="Says &quot;hi&quot; &amp; &lt;bye&gt;" data-test-quote>q</button>')
        finally:
            del hints.HINTS['data-test-quote']
        button = hints.help_button('help-x', 'the thing', 'What it <does>')
        self.assertIn('data-help="help-x" aria-label="Explain the thing" aria-controls="help-x" aria-expanded="false"', button)
        self.assertIn('<div class="filterhelptext helpnote" id="help-x" hidden>What it &lt;does&gt;</div>', button)

    def test_missing_hints_reports_buttons_selects_and_summaries_without_a_title(self):
        html = '<button data-export title="t">a</button><button class="infobutton" aria-label="Explain x">ⓘ</button><button data-nothing>b</button><summary>Plain heading</summary><summary class="plainbutton">Menu</summary><select name="z"></select>'
        self.assertEqual([m[:20] for m in hints.missing_hints(html)], ['<button data-nothing', '<summary class="plai', '<select name="z">'])

    def test_every_page_blurb_has_a_navigation_entry_name(self):
        for name in ('Home', 'Tutorial', 'Research studies', 'Search datasets', 'Dataset explorer', 'Activity', 'Priorities & access', 'Licensing', 'Status & actions'):
            self.assertIn(name, hints.PAGE_BLURBS)


if __name__ == '__main__':
    unittest.main()
