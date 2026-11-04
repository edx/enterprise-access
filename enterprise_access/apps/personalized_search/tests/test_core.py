"""Tests that keep the core portable (the import guard) and for its stream encoding."""
import ast
from pathlib import Path
from unittest import TestCase

from enterprise_access.apps.personalized_search.core import stream

CORE = Path(__file__).resolve().parent.parent / 'core'
CORE_PACKAGE = 'enterprise_access.apps.personalized_search.core'
FORBIDDEN_ROOTS = {'django', 'rest_framework', 'requests', 'openai', 'algoliasearch', 'edx_rbac', 'edx_toggles',
                   'edx_django_utils', 'edx_rest_framework_extensions', 'crum', 'waffle', 'model_utils'}


def imported_modules(path: Path | None = None, source: str | None = None) -> set[str]:
    """Every module a file (or source text) imports."""
    out = set()
    for node in ast.walk(ast.parse(source if source is not None else path.read_text())):
        if isinstance(node, ast.Import):
            out |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


class CoreImportGuardTests(TestCase):
    """The core imports the standard library and itself, nothing else (KD-10)."""

    def test_core_imports_nothing_outside_itself(self):
        offenders = []
        for path in sorted(CORE.glob('*.py')):
            for module in imported_modules(path):
                root = module.split('.')[0]
                if root in FORBIDDEN_ROOTS or (root == 'enterprise_access' and not module.startswith(CORE_PACKAGE)):
                    offenders.append(f'{path.name}: {module}')
        self.assertEqual(offenders, [])

    def test_guard_sees_a_violation(self):
        source = 'import json\nfrom django.conf import settings\nfrom enterprise_access.apps.api import utils\n'
        self.assertEqual(imported_modules(source=source), {'json', 'django.conf', 'enterprise_access.apps.api'})


class StreamTests(TestCase):
    """SSE encoding, the closing wrapper and the parts kept for replay."""

    def test_encode_part_is_compact(self):
        self.assertEqual(stream.encode_part({'type': 'finish'}), 'data: {"type":"finish"}\n\n')

    def test_sse_frames_end_with_done_and_close_on_stop(self):
        closed = []

        def parts():
            try:
                yield {'type': 'start', 'messageId': 'm'}
                yield {'type': 'finish'}
            finally:
                closed.append('gen')

        frames = list(stream.sse_frames(parts(), lambda: closed.append('release')))
        self.assertEqual(frames[-1], stream.DONE_FRAME)
        self.assertEqual(closed, ['gen', 'release'])
        closed.clear()
        frames = stream.sse_frames(parts(), lambda: closed.append('release'))
        next(frames)
        frames.close()  # the client stopped
        self.assertEqual(closed, ['gen', 'release'])

    def test_stored_parts(self):
        parts = [{'type': 'start', 'messageId': 'm'}, {'type': 'data-status', 'data': {}, 'transient': True},
                 {'type': 'text-delta', 'id': 'reply', 'delta': 'a'},
                 {'type': 'text-delta', 'id': 'reply', 'delta': 'b'},
                 {'type': 'data-timing', 'id': 'timing', 'data': {}}, {'type': 'finish'}]
        self.assertEqual(stream.stored_parts(parts), [{'type': 'start', 'messageId': 'm'},
                                                      {'type': 'text-delta', 'id': 'reply', 'delta': 'ab'}])
