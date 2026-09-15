"""
Integrity checks on the bench's static assets.

A stylesheet with unbalanced braces still loads; it just silently swallows the rules after
the break, so the page renders subtly wrong and no test notices. That happened once while
this file was being assembled, which is why these checks exist.
"""
from pathlib import Path

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase


class StaticAssetTests(TestCase):
    """ The page is only as good as the two files it pulls in. """

    def asset(self, name):
        path = finders.find(f'pathway_review/{name}')
        self.assertIsNotNone(path, f'{name} is not discoverable by the staticfiles finders')
        return Path(path).read_text(encoding='utf-8')

    def test_stylesheet_braces_balance(self):
        css = self.asset('bench.css')
        self.assertEqual(
            css.count('{'), css.count('}'),
            'bench.css has unbalanced braces; rules after the break are silently dropped',
        )

    def test_stylesheet_has_no_orphaned_declarations(self):
        """A declaration sitting outside any rule means a rule's opening line was lost."""
        depth, orphans = 0, []
        for number, line in enumerate(self.asset('bench.css').split('\n'), 1):
            stripped = line.strip()
            if stripped and not stripped.startswith(('/*', '*', '}', '@')) and depth == 0:
                if '{' not in stripped:
                    orphans.append(number)
            depth += line.count('{') - line.count('}')
        self.assertEqual(orphans, [], f'orphaned declarations at lines {orphans}')

    def test_script_is_present_and_parses_as_one_iife(self):
        js = self.asset('bench.js')
        self.assertEqual(js.count('('), js.count(')'), 'bench.js has unbalanced parentheses')
        self.assertEqual(js.count('{'), js.count('}'), 'bench.js has unbalanced braces')

    def test_template_pulls_in_both_assets(self):
        # rendered with a request, because {% csrf_token %} emits nothing without one
        html = render_to_string(
            'pathway_review/bench.html', {'reviewer_name': 'Someone'},
            request=RequestFactory().get('/pathway-review/'),
        )
        self.assertIn('pathway_review/bench.css', html)
        self.assertIn('pathway_review/bench.js', html)
        self.assertIn('csrfmiddlewaretoken', html)
