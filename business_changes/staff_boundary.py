"""Explicit staff-route transaction ownership with replies after commit.

The caller chooses its exact business route and current authorization policy.
No request dispatcher or SQL is intercepted. Existing source services and
staff audits run on the same guarded connection; redirects wait for commit.
"""
from dataclasses import dataclass
import uuid

from .commands import TransactionView


@dataclass(frozen=True)
class DeferredReply:
    method: str
    args: tuple
    kwargs: dict

    @property
    def rejected(self):
        return self.method=='error_page' or (self.method=='flash_redirect' and
            self.kwargs.get('kind',self.args[2] if len(self.args)>2 else 'success')=='error')


class RejectedReply(Exception):
    def __init__(self,reply):
        self.reply=reply
        super().__init__('staff_route_rejected')


def run_staff_transaction(handler,database,*,authorize,operation,reply_handler=None):
    state=database._request_state()
    if state and (state.get('business_connection') is not None or
                  state.get('connection') is not None and state['connection'].in_transaction):
        raise RuntimeError('staff_route_requires_its_own_outer_transaction')
    names=('flash_redirect','redirect','error_page')
    originals={name:getattr(handler,name) for name in names}
    instance_values={name:handler.__dict__[name] for name in names if name in handler.__dict__}
    user=handler.user
    try:
        try:
            with database.transaction() as connection:
                view=TransactionView(connection,'bc_staff_'+uuid.uuid4().hex)
                with database.business_scope(view):
                    handler.user={**user,**authorize(view)}
                    for name in names:
                        def defer(*args,_method=name,**kwargs):return DeferredReply(_method,args,kwargs)
                        setattr(handler,name,defer)
                    reply=operation()
                    if not isinstance(reply,DeferredReply):raise RuntimeError('staff_route_reply_must_wait_for_commit')
                    if reply.rejected:raise RejectedReply(reply)
        except RejectedReply as exc:
            reply=exc.reply
    finally:
        handler.user=user
        for name in names:
            if name in instance_values:setattr(handler,name,instance_values[name])
            else:handler.__dict__.pop(name,None)
    if reply_handler is not None:return reply_handler(reply)
    return originals[reply.method](*reply.args,**reply.kwargs)
