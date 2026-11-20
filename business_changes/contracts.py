"""Immutable change scopes supplied by an authorized domain command.

These contracts do not infer ownership or interpret business evidence. A domain
command must validate source and all affected subjects on its own connection.
"""
from dataclasses import dataclass
import re


class BusinessPermissionError(PermissionError):
    """A rechecked business authority changed; distinct from filesystem errors."""


def validate_key(value, label, *, dotted=False):
    pattern=r'[a-z][a-z0-9_.]{0,127}' if dotted else r'[a-z][a-z0-9_]{0,63}'
    if not isinstance(value,str) or not re.fullmatch(pattern,value):
        raise ValueError('invalid '+label)


@dataclass(frozen=True, order=True)
class Subject:
    kind: str
    identifier: str

    def __post_init__(self):
        validate_key(self.kind,'subject kind')
        if (not isinstance(self.identifier,str) or not self.identifier.strip()
                or self.identifier!=self.identifier.strip() or len(self.identifier)>200
                or any(ord(x)<32 for x in self.identifier)):
            raise ValueError('invalid subject identifier')


@dataclass(frozen=True)
class Change:
    event_type: str
    source: Subject
    source_version: int
    affected_subjects: tuple[Subject,...]

    def __post_init__(self):
        validate_key(self.event_type,'event type',dotted=True)
        if not isinstance(self.source,Subject):
            raise ValueError('invalid source')
        if type(self.source_version) is not int or self.source_version<1:
            raise ValueError('invalid source version')
        subjects=tuple(self.affected_subjects)
        if any(not isinstance(x,Subject) for x in subjects):
            raise ValueError('invalid affected subject')
        object.__setattr__(self,'affected_subjects',tuple(dict.fromkeys(subjects)))
