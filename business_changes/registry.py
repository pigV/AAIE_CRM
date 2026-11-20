"""Deterministic, side-effect-free delivery planning for explicit scopes.

Planning is not execution: no state is changed and no handler is called here.
Persistent delivery, retries, and domain integrations are separate work.
"""
from dataclasses import dataclass
from .contracts import Change, Subject, validate_key


@dataclass(frozen=True)
class Handler:
    name: str
    version: str
    event_types: frozenset[str]
    subject_kinds: frozenset[str]
    after: frozenset[str]=frozenset()

    def __post_init__(self):
        validate_key(self.name,'handler name')
        if not isinstance(self.version,str) or not self.version.strip() or len(self.version)>64:
            raise ValueError('invalid handler version')
        for field in ('event_types','subject_kinds','after'):
            object.__setattr__(self,field,frozenset(getattr(self,field)))
        if not self.event_types or not self.subject_kinds:
            raise ValueError('empty handler scope')
        for value in self.event_types:validate_key(value,'event type',dotted=True)
        for value in self.subject_kinds:validate_key(value,'subject kind')
        for value in self.after:validate_key(value,'dependency')


@dataclass(frozen=True)
class DeliveryTarget:
    handler_name: str
    handler_version: str
    subject: Subject


class Registry:
    def __init__(self, handlers):
        self._handlers={}
        for handler in handlers:
            if not isinstance(handler,Handler):raise ValueError('invalid handler')
            if handler.name in self._handlers:raise ValueError('duplicate handler')
            self._handlers[handler.name]=handler
        known=set(self._handlers)
        if any(h.after-known for h in self._handlers.values()):
            raise ValueError('unknown dependency')
        remaining=set(known);ordered=[]
        while remaining:
            ready=sorted(name for name in remaining if not self._handlers[name].after.intersection(remaining))
            if not ready:raise ValueError('handler dependency cycle')
            ordered.extend(ready);remaining.difference_update(ready)
        self._order=tuple(ordered)

    def plan(self, change: Change) -> tuple[DeliveryTarget,...]:
        if not isinstance(change,Change):raise ValueError('invalid change')
        subjects=sorted(set((change.source,*change.affected_subjects)))
        return tuple(DeliveryTarget(handler.name,handler.version,subject)
                     for name in self._order
                     for handler in (self._handlers[name],)
                     if change.event_type in handler.event_types
                     for subject in subjects if subject.kind in handler.subject_kinds)

    def handler(self,name):
        return self._handlers[name]
