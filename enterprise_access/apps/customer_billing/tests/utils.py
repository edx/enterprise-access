"""
Shared test utilities for customer_billing tests.
"""


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
