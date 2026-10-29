"""Anonymized LAB PLAN for the sandbox, built from the scope workbook.

The plan copies the SHAPE of the real production scope and never its names:

* every account group gets an alias ``SBX-G01``, ``SBX-G02`` ... (order of first appearance);
* every device gets an alias ``SBX-D001``, ``SBX-D002`` ... (workbook row order);
* names, ids, IP addresses and host names are NOT in the plan.

The real -> alias map can be written to one private file (``lab_alias_map.json``). It is never printed.

Functions with no file access (pure, easy to test):

* ``build_lab_plan``   - build the plan from scope rows, device plans and test counters;
* ``scale_counts``     - scale a test-type mix down to a limit, same ratio;
* ``choose_archetypes``- choose the devices for a lab with a limited number of VMs;
* ``split_waves``      - split all READY devices into waves.

No network call is made. Python 3.6 compatible.
"""
import json
import os
from collections import Counter, OrderedDict

from te_agent_migrate import scope
from te_agent_migrate.errors import ConfigurationError

SCHEMA = 1
GROUP_ALIAS = "SBX-G%02d"
DEVICE_ALIAS = "SBX-D%03d"
DEFAULT_VMS = 12
DEFAULT_MAX_TESTS_PER_GROUP = 25
DEFAULT_PLAN_FILE = os.path.join("work", "sandbox", "lab_plan.json")
ALIAS_MAP_FILE = "lab_alias_map.json"

# Test types that the seed script can build (payload shapes known from seed_sandbox.py).
SEEDABLE_TYPES = (
    "http-server", "agent-to-server", "agent-to-agent", "dns-server", "page-load", "web-transactions",
)

LAB_ONLINE = "online"
LAB_OFFLINE = "offline"  # a device that is in the plan but is never started in the lab
TARGET_TOKEN = "@T"  # the target group inside a share shape


# ----------------------------------------------------------------------------------------- small helpers
def _counter_dict(counter):
    """Counter -> dict with sorted keys and positive counts only."""
    return OrderedDict((key, int(counter[key])) for key in sorted(counter) if counter[key] > 0)


def shape_of(target, shares):
    """Share SHAPE of a device: sorted group aliases, the target alias replaced by ``@T``.

    Returns None when the device has no target.
    """
    if not target:
        return None
    tokens = sorted(set(TARGET_TOKEN if alias == target else alias for alias in shares))
    return "+".join(tokens)


def scale_counts(counts, limit, allowed=None):
    """Scale a mix ``type -> count`` down to at most ``limit`` tests. Same ratio, deterministic.

    Rules: a mix that fits is not changed. Else every type that is in the mix gets one test
    (the biggest types first if ``limit`` is smaller than the number of types), then the rest is
    shared by the divisor method (D'Hondt). A type never gets more tests than the real count.
    ``allowed``: only these types are kept (others are dropped before scaling).
    """
    items = dict((key, int(value)) for key, value in dict(counts or {}).items()
                 if int(value) > 0 and (allowed is None or key in allowed))
    total = sum(items.values())
    limit = int(limit)
    if limit <= 0 or total == 0:
        return OrderedDict()
    if total <= limit:
        return OrderedDict((key, items[key]) for key in sorted(items))
    order = sorted(items, key=lambda key: (-items[key], key))[:limit]
    given = dict((key, 1) for key in order)
    left = limit - len(order)
    while left > 0:
        best = None
        best_ratio = -1.0
        for key in order:
            if given[key] >= items[key]:
                continue
            ratio = items[key] / float(given[key] + 1)
            if ratio > best_ratio:
                best, best_ratio = key, ratio
        if best is None:
            break
        given[best] += 1
        left -= 1
    return OrderedDict((key, given[key]) for key in sorted(given))


# ----------------------------------------------------------------------------------------- the plan
class LabPlan(object):
    """The anonymized plan. ``groups`` and ``devices`` are dicts keyed by alias (ordered).

    ``alias_map`` (real names) exists only when the plan was built from a workbook. It is never part of
    ``to_dict()``.
    """

    def __init__(self, source, groups, devices, max_tests_per_group=DEFAULT_MAX_TESTS_PER_GROUP,
                 alias_map=None):
        self.source = source
        self.groups = groups
        self.devices = devices
        self.max_tests_per_group = max_tests_per_group
        self.alias_map = alias_map

    def ready_devices(self):
        return [alias for alias, device in self.devices.items() if device["status"] == scope.STATUS_READY]

    def to_dict(self, lab=None):
        data = OrderedDict()
        data["schema"] = SCHEMA
        data["source"] = self.source
        data["max_tests_per_group"] = self.max_tests_per_group
        data["groups"] = self.groups
        data["devices"] = self.devices
        if lab is not None:
            data["lab"] = lab
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ConfigurationError("Lab plan: unknown file format (schema %s expected)." % SCHEMA)
        groups = data.get("groups")
        devices = data.get("devices")
        if not isinstance(groups, dict) or not isinstance(devices, dict) or not data.get("source"):
            raise ConfigurationError("Lab plan: the keys source, groups and devices are required.")
        for alias in list(groups) + list(devices):
            if not str(alias).startswith("SBX-"):
                raise ConfigurationError("Lab plan: alias %r does not start with SBX-." % alias)
        return cls(str(data["source"]), OrderedDict(groups), OrderedDict(devices),
                   int(data.get("max_tests_per_group") or DEFAULT_MAX_TESTS_PER_GROUP))


def _alias_order(plans, tests):
    order = []
    seen = set()

    def add(aid):
        if aid and aid not in seen:
            seen.add(aid)
            order.append(aid)

    for plan in plans:
        add(plan.source_aid)
        add(plan.target_aid)
        for aid in plan.share_aids:
            add(aid)
    for aid in tests["groups"]:
        add(aid)
    return order


def build_lab_plan(rows, plans, tests, max_tests_per_group=DEFAULT_MAX_TESTS_PER_GROUP):
    """Build the ``LabPlan``.

    ``rows``: ScopeRow list; ``plans``: DevicePlan list (same order); ``tests``: result of
    ``scope.read_scope_tests``.
    """
    rows = list(rows)
    plans = list(plans)
    aid_order = _alias_order(plans, tests)
    group_alias = OrderedDict((aid, GROUP_ALIAS % (index + 1)) for index, aid in enumerate(aid_order))
    group_names = {}
    for row in rows:
        for name, aid in row.groups:
            group_names.setdefault(aid, name)
        if row.source_aid and row.source_name:
            group_names.setdefault(row.source_aid, row.source_name)
    sources = Counter(plan.source_aid for plan in plans if plan.source_aid)
    source_aid = sorted(sources, key=lambda aid: (-sources[aid], group_alias[aid]))[0] if sources else ""

    devices = OrderedDict()
    device_names = OrderedDict()
    for index, plan in enumerate(plans):
        alias = DEVICE_ALIAS % (index + 1)
        device_names[alias] = plan.name
        anomaly = bool(plan.target_aid) and scope.WARNING_TARGET_NOT_IN_GROUPS in plan.warnings
        shares = [aid for aid in plan.share_aids if not (anomaly and aid == plan.target_aid)]
        share_aliases = [group_alias[aid] for aid in shares]
        target = group_alias[plan.target_aid] if plan.target_aid else None
        device = OrderedDict()
        device["status"] = plan.status
        device["lab_state"] = LAB_ONLINE if plan.status == scope.STATUS_READY else LAB_OFFLINE
        device["source"] = group_alias[plan.source_aid] if plan.source_aid else None
        device["target"] = target
        device["shares"] = share_aliases
        device["shape"] = shape_of(target, share_aliases)
        device["anomaly"] = anomaly
        device["tests_by_type"] = _counter_dict(tests["agents"].get(plan.name, Counter()))
        devices[alias] = device

    groups = OrderedDict()
    for aid, alias in group_alias.items():
        mix = tests["group_unique_types"].get(aid, Counter())
        group = OrderedDict()
        group["roles"] = []
        group["devices"] = 0
        group["ready_devices"] = 0
        group["tests"] = int(sum(mix.values()))
        group["tests_by_type"] = _counter_dict(mix)
        group["scaled"] = scale_counts(group["tests_by_type"], max_tests_per_group, SEEDABLE_TYPES)
        group["not_seedable"] = OrderedDict(
            (key, value) for key, value in group["tests_by_type"].items() if key not in SEEDABLE_TYPES)
        groups[alias] = group
    source = group_alias[source_aid] if source_aid else ""
    for alias, device in devices.items():
        ready = device["status"] == scope.STATUS_READY
        for share in set(device["shares"]):
            groups[share]["devices"] += 1
            if ready:
                groups[share]["ready_devices"] += 1
    target_set = set(d["target"] for d in devices.values() if d["target"])
    shared_set = set(share for d in devices.values() for share in d["shares"])
    for alias, group in groups.items():
        roles = []
        if alias == source:
            roles.append("source")
        if alias in target_set:
            roles.append("target")
        if alias in shared_set and alias != source and alias not in target_set:
            roles.append("shared")
        if group["tests"] > 0:
            roles.append("test-owner")
        group["roles"] = roles
    alias_map = OrderedDict()
    alias_map["groups"] = OrderedDict(
        (alias, OrderedDict([("name", group_names.get(aid, "")), ("aid", aid)]))
        for aid, alias in group_alias.items())
    alias_map["devices"] = device_names
    return LabPlan(source, groups, devices, max_tests_per_group, alias_map)


def load_lab_plan(xlsx_path, prefix_map_path=None, max_tests_per_group=DEFAULT_MAX_TESTS_PER_GROUP):
    """Read the workbook and build the plan. Without a prefix map file the map is derived from the rows."""
    rows = scope.read_scope_workbook(xlsx_path)
    if not rows:
        raise ConfigurationError("Sheet %r has no agent rows." % scope.SUMMARY_SHEET)
    if prefix_map_path:
        entries = scope.load_prefix_map(prefix_map_path)
    else:
        entries = scope.derive_prefix_map(rows)
    plans = scope.build_plan(rows, entries)
    tests = scope.read_scope_tests(xlsx_path)
    return build_lab_plan(rows, plans, tests, max_tests_per_group)


# ----------------------------------------------------------------------------------------- archetypes
def _total_tests(device):
    return sum(device["tests_by_type"].values())


def _pick(candidates, keyfunc):
    """The candidate with the biggest key. On a tie the first (candidates are sorted) wins."""
    best = None
    best_key = None
    for alias in candidates:
        key = keyfunc(alias)
        if best is None or key > best_key:
            best, best_key = alias, key
    return best


def choose_archetypes(plan, vms=DEFAULT_VMS):
    """Choose the devices of a lab with ``vms`` VMs. Deterministic: no random choice.

    Phases (a phase stops when the VMs are all used):

    0. the anomaly (target not in the share list), if a READY device has it;
    1. every target group, the smallest groups first;
    2. every share shape that is still missing;
    3. every test type that is still missing (the device with most tests of that type);
    4. the devices that add most groups that the lab does not see yet.

    Return an OrderedDict: ``devices`` (sorted aliases), ``reasons``, ``coverage``, ``not_covered``.
    """
    vms = max(int(vms), 0)
    devices = plan.devices
    ready = sorted(plan.ready_devices())
    chosen = []
    reasons = OrderedDict()
    state = {"targets": set(), "shapes": set(), "types": set(), "groups": set()}

    def types_of(alias):
        return set(devices[alias]["tests_by_type"])

    def groups_of(alias):
        return set(devices[alias]["shares"])

    def take(alias, reason):
        chosen.append(alias)
        reasons[alias] = reason
        device = devices[alias]
        if device["target"]:
            state["targets"].add(device["target"])
        if device["shape"]:
            state["shapes"].add(device["shape"])
        state["types"].update(types_of(alias))
        state["groups"].update(groups_of(alias))

    def new_shape(alias):
        shape = devices[alias]["shape"]
        return 1 if shape and shape not in state["shapes"] else 0

    def gain(alias):
        return (new_shape(alias), len(types_of(alias) - state["types"]),
                len(groups_of(alias) - state["groups"]), _total_tests(devices[alias]))

    def free(candidates):
        return [alias for alias in candidates if alias not in chosen]

    # phase 0: the anomaly
    if vms:
        anomalies = [alias for alias in ready if devices[alias]["anomaly"]]
        if anomalies:
            take(_pick(anomalies, gain), "anomaly")
    # phase 1: every target group, the smallest first
    by_target = OrderedDict()
    for alias in ready:
        target = devices[alias]["target"]
        if target:
            by_target.setdefault(target, []).append(alias)
    for target in sorted(by_target, key=lambda name: (len(by_target[name]), name)):
        if len(chosen) >= vms:
            break
        if target in state["targets"]:
            continue
        take(_pick(free(by_target[target]), gain), "target")
    # phase 2: share shapes
    by_shape = OrderedDict()
    for alias in ready:
        shape = devices[alias]["shape"]
        if shape:
            by_shape.setdefault(shape, []).append(alias)
    for shape in sorted(by_shape, key=lambda name: (len(by_shape[name]), name)):
        if len(chosen) >= vms:
            break
        if shape in state["shapes"]:
            continue
        take(_pick(free(by_shape[shape]), gain), "shape")
    # phase 3: test types, the rarest type first
    by_type = OrderedDict()
    for alias in ready:
        for test_type in devices[alias]["tests_by_type"]:
            by_type.setdefault(test_type, []).append(alias)
    for test_type in sorted(by_type, key=lambda name: (len(by_type[name]), name)):
        if len(chosen) >= vms:
            break
        if test_type in state["types"]:
            continue
        candidates = free(by_type[test_type])
        if candidates:
            take(_pick(candidates, lambda a, t=test_type: (devices[a]["tests_by_type"][t],) + gain(a)),
                 "type")
    # phase 4: fill the free VMs with devices that add most groups
    while len(chosen) < vms:
        candidates = free(ready)
        if not candidates:
            break
        take(_pick(candidates, lambda a: (len(groups_of(a) - state["groups"]),
                                          len(types_of(a) - state["types"]), _total_tests(devices[a]))),
             "groups")
    chosen_sorted = sorted(chosen)
    reasons = OrderedDict((alias, reasons[alias]) for alias in chosen_sorted)
    result = OrderedDict()
    result["vms"] = vms
    result["devices"] = chosen_sorted
    result["reasons"] = reasons
    coverage, not_covered = coverage_of(plan, chosen_sorted)
    result["coverage"] = coverage
    result["not_covered"] = not_covered
    return result


def coverage_of(plan, chosen):
    """Return (coverage, not_covered) for a list of chosen device aliases. Pure."""
    devices = plan.devices
    ready = plan.ready_devices()
    chosen = list(chosen)

    def collect(aliases, key):
        values = set()
        for alias in aliases:
            value = devices[alias][key]
            if isinstance(value, (list, dict)):
                values.update(value)
            elif value:
                values.add(value)
        return values

    targets_all = collect(ready, "target")
    shapes_all = collect(ready, "shape")
    types_all = collect(ready, "tests_by_type")
    targets_in = collect(chosen, "target")
    shapes_in = collect(chosen, "shape")
    types_in = collect(chosen, "tests_by_type")
    groups_seen = collect(chosen, "shares")
    owners = set(alias for alias, group in plan.groups.items() if group["tests"] > 0)
    anomaly_ready = [alias for alias in ready if devices[alias]["anomaly"]]
    anomaly_in = [alias for alias in chosen if devices[alias]["anomaly"]]
    coverage = OrderedDict()
    coverage["target_groups"] = [len(targets_in), len(targets_all)]
    coverage["shapes"] = [len(shapes_in), len(shapes_all)]
    coverage["test_types"] = [len(types_in), len(types_all)]
    coverage["groups_seen"] = [len(groups_seen), len(plan.groups)]
    coverage["test_owner_groups_seen"] = [len(owners & groups_seen), len(owners)]
    coverage["anomaly"] = ("covered" if anomaly_in else ("not covered" if anomaly_ready else "none in READY"))
    not_covered = OrderedDict()
    not_covered["target_groups"] = sorted(targets_all - targets_in)
    not_covered["shapes"] = sorted(shapes_all - shapes_in)
    not_covered["test_types"] = sorted(types_all - types_in)
    not_covered["groups"] = sorted(set(plan.groups) - groups_seen)
    not_covered["test_owner_groups"] = sorted(owners - groups_seen)
    not_covered["anomaly"] = bool(anomaly_ready) and not anomaly_in
    not_covered["ready_devices_not_in_lab"] = len(set(ready) - set(chosen))
    not_covered["devices_never_started"] = len(
        [alias for alias, device in devices.items() if device["status"] != scope.STATUS_READY])
    return coverage, not_covered


# ----------------------------------------------------------------------------------------- waves
def waves_needed(plan, vms=DEFAULT_VMS):
    """Number of waves that cover every READY device (wave 1 = the archetype set)."""
    vms = max(int(vms), 1)
    first = choose_archetypes(plan, vms)["devices"]
    rest = len(set(plan.ready_devices()) - set(first))
    return 1 + (rest + vms - 1) // vms


def split_waves(plan, vms=DEFAULT_VMS, waves=None):
    """Split all READY devices into waves of at most ``vms`` devices. Wave 1 = the archetype set.

    ``waves``: wanted number of waves. It is raised to the least number that fits and lowered to
    the number that has no empty wave. The rest is split in balanced slices, ordered by target and
    alias, so a wave holds devices of the same target groups.
    Return an OrderedDict: ``requested``, ``used``, ``waves`` (list of sorted alias lists).
    """
    vms = max(int(vms), 1)
    first = choose_archetypes(plan, vms)["devices"]
    devices = plan.devices
    rest = sorted(set(plan.ready_devices()) - set(first),
                  key=lambda alias: (devices[alias]["target"] or "", alias))
    minimum = 1 + (len(rest) + vms - 1) // vms
    wanted = int(waves) if waves else 1
    used = max(wanted, minimum) if rest else 1
    used = min(used, 1 + len(rest))
    result = OrderedDict()
    result["requested"] = wanted
    result["used"] = used
    out = [list(first)]
    if used > 1:
        count = used - 1
        base, extra = divmod(len(rest), count)
        position = 0
        for index in range(count):
            size = base + (1 if index < extra else 0)
            out.append(sorted(rest[position:position + size]))
            position += size
    result["waves"] = out
    return result


def build_lab(plan, vms=DEFAULT_VMS, waves=None):
    """The ``lab`` section of the plan file: archetype choice, coverage and (when asked) waves."""
    lab = choose_archetypes(plan, vms)
    lab["waves_needed"] = waves_needed(plan, vms)
    if waves:
        lab["wave_split"] = split_waves(plan, vms, waves)
    return lab


# ----------------------------------------------------------------------------------------- summary text
def _ratio(pair):
    return "%d of %d" % (pair[0], pair[1])


def summary_lines(plan, lab):
    """Counts only. Alias names are anonymous and are listed where it helps."""
    statuses = Counter(device["status"] for device in plan.devices.values())
    cover = lab["coverage"]
    miss = lab["not_covered"]
    lines = ["LAB PLAN SUMMARY " + "*" * 53]
    lines.append("  account groups in plan ..... %d" % len(plan.groups))
    lines.append("  devices in plan ............ %d (%s)" % (
        len(plan.devices), ", ".join("%s %d" % (key, statuses[key]) for key in sorted(statuses))))
    lines.append("  groups with tests .......... %d" % len([g for g in plan.groups.values() if g["tests"]]))
    lines.append("  VMs in the lab ............. %d" % lab["vms"])
    lines.append("  devices chosen ............. %d" % len(lab["devices"]))
    lines.append("  target groups covered ...... %s" % _ratio(cover["target_groups"]))
    lines.append("  share shapes covered ....... %s" % _ratio(cover["shapes"]))
    lines.append("  test types covered ......... %s" % _ratio(cover["test_types"]))
    lines.append("  groups seen by the lab ..... %s" % _ratio(cover["groups_seen"]))
    lines.append("  test-owner groups seen ..... %s" % _ratio(cover["test_owner_groups_seen"]))
    lines.append("  anomaly .................... %s" % cover["anomaly"])
    lines.append("NOT COVERED:")
    lines.append("  target groups .............. %d" % len(miss["target_groups"]))
    lines.append("  share shapes ............... %d" % len(miss["shapes"]))
    lines.append("  test types ................. %d" % len(miss["test_types"]))
    lines.append("  groups without a lab device  %d" % len(miss["groups"]))
    lines.append("  READY devices not in lab ... %d" % miss["ready_devices_not_in_lab"])
    lines.append("  devices never started ...... %d (offline, login problem or review)"
                 % miss["devices_never_started"])
    lines.append("  waves for all READY devices  %d (wave 1 = the archetype set)" % lab["waves_needed"])
    split = lab.get("wave_split")
    if split:
        lines.append("  waves requested/used ....... %d / %d (sizes: %s)" % (
            split["requested"], split["used"], ", ".join(str(len(w)) for w in split["waves"])))
    lines.append("")
    lines.append("Waves: the operator re-registers the SAME VMs for each wave. Before the next wave run")
    lines.append("--cleanup --apply, register the VMs again, then seed with --wave N.")
    return lines


# ----------------------------------------------------------------------------------------- files
def _private_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, 0o700)


def write_private_json(path, data):
    """Write JSON to ``path`` (file mode 0600; a new directory gets mode 0700)."""
    path = str(path)
    _private_dir(os.path.dirname(path))
    text = json.dumps(data, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path


def read_lab_plan_file(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (IOError, OSError, ValueError) as exc:
        raise ConfigurationError("Lab plan: cannot read %s: %s" % (path, exc))
    return LabPlan.from_dict(data)
