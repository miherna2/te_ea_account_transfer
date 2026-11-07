"""Checks that change nothing, run before the first agent is reset.

A reset cannot be undone, so every cheap thing that can fail later is tried first:
the agent UI answers on port 443 from this computer, and (in apply mode) the UI login works.
"""
import socket

from te_agent_migrate.errors import AuthenticationError
from te_agent_migrate.flow.cluster import _member_ui_ip
from te_agent_migrate.flow.standalone import _agent_ip
from te_agent_migrate.output import mask_secrets

UI_PORT = 443
CONNECT_TIMEOUT = 5


class PrecheckResult(object):
    """What the checks found. Each list holds 'name (ip)' texts; ``ok`` is true when all lists are empty."""

    def __init__(self):
        self.checked = 0
        self.unreachable = []
        self.bad_login = []
        self.other = []

    @property
    def ok(self):
        return not (self.unreachable or self.bad_login or self.other)


def ui_addresses(item):
    """(label, ip) of every agent UI the flow of this device will open."""
    if item.inventory.is_cluster:
        out = []
        for member in item.members:
            ip = _member_ui_ip(item, member)
            if ip:
                out.append(('%s / %s' % (item.name, member.get('name') or member.get('memberId') or '?'), ip))
        return out
    ip = _agent_ip(item)
    return [(item.name, ip)] if ip else []


def _reachable(ip, port=UI_PORT, timeout=CONNECT_TIMEOUT):
    try:
        connection = socket.create_connection((ip, port), timeout=timeout)
    except (OSError, ValueError):
        return False
    connection.close()
    return True


def check_devices(items, ui_factory=None, reachable=_reachable):
    """Check every ready device. ``ui_factory`` None = only the connection; else the UI login too.

    Returns a PrecheckResult. Nothing is written to any device.
    """
    result = PrecheckResult()
    for item in items:
        if item.status != 'ready':
            continue
        for label, ip in ui_addresses(item):
            result.checked += 1
            text = '%s (%s)' % (label, ip)
            if not reachable(ip):
                result.unreachable.append(text)
                continue
            if ui_factory is None:
                continue
            try:
                with ui_factory(ip) as ui:
                    ui.authenticate()
            except AuthenticationError:
                result.bad_login.append(text)
            except Exception as exc:  # reachable but the UI answered badly: report it, do not guess
                result.other.append('%s: %s' % (text, mask_secrets(str(exc)) or exc.__class__.__name__))
    return result
