"""Synthetic discovery_output fixture for the te_report tests.

Everything here is invented; no real discovery data is used. The helper is
named test_report_*.py so it is shipped with the report tests (it holds no
test cases itself).
"""

import io
import json
import os

SECRET_MARKER = "SYNTHETIC-SECRET-VALUE"


def write_json(path, data):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data))


def _agent(agent_id, name, state, ips, test_ids=None, **extra):
    item = {
        "agentId": str(agent_id),
        "agentName": name,
        "agentType": "enterprise",
        "agentState": state,
        "ipAddresses": ips,
    }
    if test_ids is not None:
        item["testIds"] = test_ids
    item.update(extra)
    return item


def _cluster(agent_id, name, members, test_ids=None):
    item = {
        "agentId": str(agent_id),
        "agentName": name,
        "agentType": "enterprise-cluster",
        "clusterMembers": members,
    }
    if test_ids is not None:
        item["testIds"] = test_ids
    return item


def _member(member_id, name, ip, state):
    return {"memberId": member_id, "name": name, "ipAddresses": [ip], "agentState": state}


def _link():
    return {"self": {"href": "https://example.invalid/x"}}


def build_discovery(root, full=True):
    """Write a synthetic discovery_output tree into ``root``.

    Group 100 ("Source") owns tests/dashboards; group 200 ("Other") only sees
    the shared agent 1. With ``full=False`` only the bare minimum is written.
    """
    write_json(
        os.path.join(root, "account_groups.json"),
        {
            "accountGroups": [
                {"aid": "100", "accountGroupName": "Source"},
                {"aid": "200", "accountGroupName": "Other"},
            ],
            "_links": _link(),
        },
    )
    g100 = os.path.join(root, "aid_100")
    g200 = os.path.join(root, "aid_200")

    agents_page1 = {
        "agents": [
            _agent(1, "agent-a", "online", ["10.0.0.1"], [5001, 5002], agentToken="tok-" + SECRET_MARKER),
            _agent(2, "agent-b", "online", ["10.0.0.2"], [5002, 5003]),
            _agent(3, "agent-off", "offline", ["10.0.0.3"]),
        ],
        "_links": _link(),
    }
    agents_page2 = {
        "agents": [
            _cluster(
                4, "cluster-c",
                [_member("cm1", "node1", "10.0.1.1", "online"), _member("cm2", "node2", "10.0.1.2", "online")],
                [5004],
            ),
            _cluster(
                5, "cluster-bad",
                [_member("cb1", "nodeb1", "10.0.2.1", "online"), _member("cb2", "nodeb2", "10.0.2.2", "offline")],
            ),
            _agent(6, "agent-disabled", "disabled", ["10.0.0.6"]),
        ]
    }
    write_json(os.path.join(g100, "agents.json"), [agents_page1, agents_page2])
    write_json(
        os.path.join(g200, "agents.json"),
        [{"agents": [_agent(1, "agent-a", "online", ["10.0.0.1"], [])]}],
    )
    if not full:
        return root

    write_json(
        os.path.join(g100, "tests.json"),
        [
            {
                "tests": [
                    {"testId": "5001", "testName": "test-one", "type": "http-server", "enabled": True},
                    {"testId": "5002", "testName": "test-two", "type": "agent-to-server", "enabled": True},
                    {
                        "testId": "5003", "testName": "a2a-test", "type": "agent-to-agent",
                        "enabled": True, "targetAgentId": "1",
                    },
                    {"testId": "5004", "testName": "cluster-test", "type": "http-server"},
                    {
                        "testId": "5005", "testName": "script-test", "type": "web-transactions",
                        "transactionScript": "var pw = '%s';" % SECRET_MARKER,
                        "credentials": [{"password": SECRET_MARKER}],
                    },
                ]
            }
        ],
    )
    write_json(
        os.path.join(g100, "alert_rules.json"),
        [{"alertRules": [
            {"ruleId": "r1", "ruleName": "Loss", "tests": [{"testId": "5001"}]},
            {"ruleId": "r2", "ruleName": "no details"},
        ]}],
    )
    write_json(
        os.path.join(g100, "agent_notif_rules.json"),
        [{"agentAlertRules": [{"ruleId": "n1", "ruleName": "agent down", "agents": [{"agentId": "2"}]}]}],
    )
    write_json(
        os.path.join(g100, "suppression.json"),
        [{"alertSuppressionWindows": [{"alertSuppressionWindowId": "s1", "name": "maint", "isEnabled": True}]}],
    )
    write_json(
        os.path.join(g100, "suppression_s1.json"),
        {
            "alertSuppressionWindowId": "s1", "name": "maint", "isEnabled": True,
            "agents": [{"agentId": "2"}], "status": "active",
        },
    )
    tags = [
        {"id": "tag-agent-1", "objectType": "v-agent", "type": "static", "key": "site", "value": "hq", "legacyId": 501},
        {"id": "tag-test-1", "objectType": "test", "type": "static", "key": "app", "value": "web", "legacyId": 601},
        {"id": "tag-dash-1", "objectType": "dashboard", "type": "static", "key": "team", "value": "ops", "legacyId": 701},
    ]
    write_json(os.path.join(g100, "tags.json"), [{"tags": tags}])
    assigned = []
    assignments = {
        "tag-agent-1": [{"id": "1", "type": "v-agent"}, {"id": "2", "type": "v-agent"}],
        "tag-test-1": [{"id": "5001", "type": "test"}, {"id": "5002", "type": "test"}, {"id": "9", "type": "endpoint-test"}],
        "tag-dash-1": [{"id": "d1", "type": "dashboard"}],
    }
    for tag in tags:
        copy = dict(tag)
        copy["assignments"] = assignments[tag["id"]]
        assigned.append(copy)
    write_json(os.path.join(g100, "tags_assignments.json"), [{"tags": assigned}])

    write_json(
        os.path.join(g100, "dashboards.json"),
        [[
            {"dashboardId": "d1", "title": "Dash One", "isBuiltIn": False, "globalFilterId": "gf1"},
            {"dashboardId": "d2", "title": "Dash Two", "isBuiltIn": False, "globalFilterId": "gf2"},
        ]],
    )
    write_json(
        os.path.join(g100, "dashboard_d1.json"),
        {
            "dashboardId": "d1", "title": "Dash One", "globalFilterId": "gf1",
            "widgets": [
                {"id": "w1", "title": "agent-id widget", "type": "Time Series: Line",
                 "filters": {"AGENT": ["1"], "TEST": ["5001"]}},
                {"id": "w2", "title": "agent tag widget", "type": "Table",
                 "filters": {"AGENT_TAG_ID": ["tag-agent-1"]}},
                {"id": "w3", "title": "status owned", "type": "Agent Status",
                 "agents": "Enterprise Agents", "show": "Owned Agents",
                 "dataSource": "CLOUD_AND_ENTERPRISE_AGENTS", "filters": {}},
                {"id": "w4", "title": "tests only", "type": "Table",
                 "filters": {"TEST_TAG_ID": ["tag-test-1"], "TEST": ["5001"], "ENDPOINT_TEST_TAG_ID": ["x"]}},
                {"id": "w5", "title": "legacy label widget", "type": "Table",
                 "filters": {"AGENT_LABEL": ["501"]}},
                {"id": "w6", "title": "nested target", "type": "Number",
                 "numberCards": [{"id": "c1", "filters": {"TARGET_AGENT": ["2"]}}],
                 "filters": {}},
                {"id": "w7", "title": "status all", "type": "Agent Status",
                 "agents": "Enterprise Agents", "show": "All Assigned Agents", "filters": {}},
            ],
        },
    )
    write_json(
        os.path.join(g100, "dashboard_filters.json"),
        [{"dashboardFilters": [
            {"id": "gf1", "name": "GF target", "context": [
                {"dataSourceId": "VIRTUAL_AGENT", "filters": [
                    {"filterId": "SOURCE_AND_TARGET_AGENT", "values": ["2"], "metricIds": []},
                    {"filterId": "TEST_TAG", "values": ["tag-test-1"], "metricIds": []},
                ]},
            ]},
            {"id": "gf2", "name": "GF agent tag", "context": [
                {"dataSourceId": "VIRTUAL_AGENT", "filters": [
                    {"filterId": "AGENT_TAG", "values": ["tag-agent-1"], "metricIds": []},
                ]},
                {"dataSourceId": "ENDPOINT_AGENTS", "filters": [
                    {"filterId": "ENDPOINT_AGENT_TAG_ID", "values": ["zzz"], "metricIds": []},
                ]},
            ]},
        ]}],
    )
    write_json(
        os.path.join(root, "agent_details", "1_aid100.json"),
        {"agentId": "1", "accountGroups": [
            {"aid": "100", "accountGroupName": "Source"}, {"aid": "200", "accountGroupName": "Other"}]},
    )
    write_json(
        os.path.join(root, "agent_details", "2_aid100.json"),
        {"agentId": "2", "accountGroups": [
            {"aid": "100", "accountGroupName": "Source"}, {"aid": "300", "accountGroupName": "Elsewhere"}]},
    )
    return root


def write_selection(path, lines):
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path
