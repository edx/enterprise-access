"""
Shared test utilities for customer_billing tests.
"""
import ast
import inspect
import re
import sys
import typing

import stripe
from typing_extensions import is_typeddict


def _nested_params_types(hint, module):
    """
    Yield the stripe ``*Params`` TypedDicts that a param's type hint refers to, through
    ``NotRequired``/``List``/``Union``. Forward refs are resolved by name in the params module
    rather than evaluated, since some only exist for type checkers (e.g. ``StripeContext``).
    """
    if isinstance(hint, typing.ForwardRef):
        for name in re.findall(r'[A-Za-z_]\w*', hint.__forward_arg__):
            candidate = getattr(module, name, None)
            if is_typeddict(candidate):
                yield candidate
        return
    if is_typeddict(hint):
        yield hint
        return
    for arg in typing.get_args(hint):
        yield from _nested_params_types(arg, module)


def unsupported_stripe_params(params, params_type, path=''):
    """
    Return the dotted paths of keys in ``params`` that the stripe ``params_type`` TypedDict
    doesn't declare, recursing into nested params. Free-form dicts like ``metadata`` are not checked.
    """
    module = sys.modules[params_type.__module__]
    declared = params_type.__required_keys__ | params_type.__optional_keys__
    unsupported = []
    for key, value in params.items():
        key_path = f'{path}{key}'
        if key not in declared:
            unsupported.append(key_path)
            continue
        nested_types = list(_nested_params_types(params_type.__annotations__[key], module))
        if not nested_types:
            continue
        items = value if isinstance(value, list) else [value]
        for index, item in enumerate(items):
            if isinstance(item, dict):
                item_path = f'{key_path}[{index}].' if isinstance(value, list) else f'{key_path}.'
                unsupported += min(
                    (unsupported_stripe_params(item, nested_type, item_path) for nested_type in nested_types),
                    key=len,
                )
    return unsupported


def _dotted_name(node):
    """Return ``['stripe', 'checkout', 'Session', 'create']`` for ``stripe.checkout.Session.create``."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return [node.id, *reversed(parts)] if isinstance(node, ast.Name) else None


def _literal_shape(node):
    """Keep the keys of dict/list literals so nested params can be checked; any other value becomes None."""
    if isinstance(node, ast.Dict) and all(isinstance(key, ast.Constant) for key in node.keys):
        return {key.value: _literal_shape(value) for key, value in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal_shape(element) for element in node.elts]
    return None


def _params_type(func, path):
    """
    Return the ``*Params`` TypedDict a stripe method's ``**params`` is annotated with, looked up in
    ``stripe.params`` (or ``stripe.params.checkout`` etc. for namespaced resources).
    """
    namespace = getattr(stripe, 'params', None)  # stripe<12 has no stripe.params
    for part in path[1:-2]:
        namespace = getattr(namespace, part, None)
    for parameter in inspect.signature(func).parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            for hint in typing.get_args(parameter.annotation):
                name = hint.__forward_arg__ if isinstance(hint, typing.ForwardRef) else getattr(hint, '__name__', '')
                candidate = getattr(namespace, name, None)
                if is_typeddict(candidate):
                    return candidate
    return None


def _missing_stripe_references(tree, filename):
    """
    Report every ``stripe.X...`` reference (calls, ``isinstance`` checks, ``except`` clauses, annotations)
    the installed SDK no longer has, e.g. ``stripe.Reversal`` after stripe 16 renamed it. An ``except``
    clause only evaluates its class when an exception is raised, so a rename there fails late.
    """
    missing = set()
    for node in ast.walk(tree):
        path = _dotted_name(node) if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) else None
        if not path or path[0] != 'stripe':
            continue
        obj = stripe
        for depth, part in enumerate(path[1:], start=2):
            if not hasattr(obj, part):
                missing.add((node.lineno, '.'.join(path[:depth])))
                break
            obj = getattr(obj, part)
    return [f'{filename}:{line} {name}: does not exist in stripe=={stripe.VERSION}' for line, name in sorted(missing)]


def stripe_call_problems(source, filename='<source>'):
    """
    Check every ``stripe.X`` reference and ``stripe.<Resource>.<method>(...)`` call in ``source`` against
    the installed stripe SDK.

    Reports references to names the SDK no longer has, calls its signature rejects (e.g. a path param
    passed by keyword once it became positional-only), and literal params (including nested dict
    keys) its ``*Params`` TypedDict no longer declares. Values passed via ``**kwargs`` can't be seen
    statically; cover those with a runtime test that checks the captured kwargs instead.
    """
    tree = ast.parse(source)
    problems = _missing_stripe_references(tree, filename)
    for node in ast.walk(tree):
        path = _dotted_name(node.func) if isinstance(node, ast.Call) else None
        if not path or path[0] != 'stripe' or len(path) < 3:
            continue
        where = f'{filename}:{node.lineno} {".".join(path)}'
        owner, func = None, stripe
        try:
            for part in path[1:]:
                owner, func = func, getattr(func, part)
        except AttributeError:
            continue  # reported by _missing_stripe_references
        # Methods callable on the class or an instance (e.g. Coupon.delete) route class-style calls to a
        # ``_cls_<name>`` classmethod, which is what ``stripe.X.method(...)`` runs, so check that one.
        variant = getattr(inspect.getattr_static(owner, path[-1], None), 'class_method_name', None)
        if variant:
            func = getattr(owner, variant)
        if not inspect.isroutine(func):
            continue
        keywords = {keyword.arg: _literal_shape(keyword.value) for keyword in node.keywords if keyword.arg}
        signature = inspect.signature(func)
        # Checked explicitly: on some CPython 3.12 patch releases (e.g. 3.12.7) Signature.bind() lets a
        # positional-only name passed by keyword fall into **params instead of failing like the real call.
        positional_only = [
            name for name, parameter in signature.parameters.items() if parameter.kind is parameter.POSITIONAL_ONLY
        ]
        by_keyword = [name for name in positional_only if name in keywords]
        if by_keyword:
            problems.append(f'{where}: passes positional-only {", ".join(by_keyword)} by keyword')
            continue
        if not any(isinstance(arg, ast.Starred) for arg in node.args) and all(k.arg for k in node.keywords):
            try:
                signature.bind(*node.args, **keywords)
            except TypeError as exc:
                problems.append(f'{where}: {exc}')
                continue
        params_type = _params_type(func, path)
        if params_type:
            explicit = set(signature.parameters)
            params = {key: value for key, value in keywords.items() if key not in explicit}
            problems += [
                f'{where}: stripe=={stripe.VERSION} does not accept param {param}'
                for param in unsupported_stripe_params(params, params_type)
            ]
    return problems


class AttrDict(dict):
    """
    Minimal helper that allows both attribute (obj.foo) and item (obj['foo']) access.
    Recursively converts nested dicts to AttrDicts, but leaves non-dict values as-is.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k, v in list(self.items()):
            if isinstance(v, dict) and not isinstance(v, AttrDict):
                self[k] = AttrDict.wrap(v)

    def __getattr__(self, name):
        try:
            value = self[name]
        except KeyError as e:
            raise AttributeError(name) from e
        return value

    def __setattr__(self, name, value):
        self[name] = value

    def to_dict(self):
        """Recursively convert AttrDict to a plain dict."""
        def _convert(v):
            if isinstance(v, AttrDict):
                return v.to_dict()
            if isinstance(v, list):
                return [_convert(item) for item in v]
            return v
        return {k: _convert(v) for k, v in self.items()}

    @staticmethod
    def wrap(value):
        """Recursively wrap dicts and lists in AttrDict."""
        if isinstance(value, dict) and not isinstance(value, AttrDict):
            return AttrDict({k: AttrDict.wrap(v) for k, v in value.items()})
        if isinstance(value, list):
            return [AttrDict.wrap(item) for item in value]
        return value
