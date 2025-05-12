"""
Workflow Steps do work on objects, not on dicts.  The BaseInputOutput
class below can be subclassed by attrs classes, so that every input/output
class utilized by workflow steps can be counted on to have a from_dict()
classmethod to generate an object from a dict, and a to_dict() method
to turn an instance of an input/output class back into a dict (in other words
to structure and unstructure, or to deserialize and then serialize).
"""
import uuid
from datetime import datetime
from logging import getLogger

import attrs
import cattrs

LOGGER = getLogger(__name__)

CONVERTER = cattrs.Converter()


@CONVERTER.register_structure_hook
def uuid_structure_hook(val: str, _) -> uuid.UUID:
    """
    cattrs has a good number of built-in hooks to structure/unstructure
    data, but UUIDs are not one of them. This hook function ensures
    that any field declared as a uuid type that is structured via the
    default converter object (i.e. via inheritance from the ``BaseInputOutput``
    class below) ends up as an actual UUID.
    """
    if not val:
        return None
    return uuid.UUID(val)


@CONVERTER.register_unstructure_hook
def uuid_unstructure_hook(val: uuid.UUID) -> str:
    """
    cattrs has a good number of built-in hooks to structure/unstructure
    data, but UUIDs are not one of them. This hook function ensures
    that any field declared as a uuid type that is *un*structured via the
    default converter object (i.e. via inheritance from the ``BaseInputOutput``
    class below) ends up as a string representation of the UUID value
    stored in the field.
    """
    if not val:
        return None
    return str(val)


@CONVERTER.register_structure_hook
def datetime_structure_hook(val: datetime, _) -> datetime:
    return val


@CONVERTER.register_unstructure_hook
def datetime_unstructure_hook(val: datetime) -> str:
    if not val:
        return None
    return val.isoformat()


@attrs.define
class BaseInputOutput:
    """
    Base class that other attrs-defined workflow input and output classes
    should inherit from.
    """
    @classmethod
    def from_dict(cls, data_dict):
        try:
            return CONVERTER.structure(data_dict, cls)
        except Exception as exc:
            LOGGER.exception('Exception structuring %s: %s', data_dict, exc)
            raise

    def to_dict(self):
        try:
            return CONVERTER.unstructure(self)
        except Exception as exc:
            LOGGER.exception('Exception un-structuring %s: %s', self, exc)
            raise 
