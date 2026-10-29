"""Tests for te_agent_migrate.scope. All data is synthetic."""
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape

from te_agent_migrate import scope
from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.inventory import load_inventory

SUMMARY_HEADER = [
    'AgentName', 'AgentId', 'ExpectedCurrentPrimaryGroup', 'State', 'Hostname', 'PrivateIPs',
    'AccountGroupCount', 'AccountGroups', 'UniqueTests', 'TestOwnerGroups', 'Login State', 'notes',
]
TESTS_HEADER = ['AgentName', 'AgentId', 'AccountGroupContext', 'AID', 'TestId', 'TestName', 'TestType']
SOURCE = 'Group S'
SOURCE_AID = '900'


class Inline(str):
    """Mark a value that must be written as an inline string."""


def _column_letter(index):
    letters = ''
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _sheet_xml(rows, shared):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for row_index, row in enumerate(rows, start=1):
        parts.append('<row r="%d">' % row_index)
        for col_index, value in enumerate(row):
            ref = '%s%d' % (_column_letter(col_index), row_index)
            if value is None:
                continue
            if isinstance(value, Inline):
                parts.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, escape(value)))
            elif isinstance(value, str):
                if value not in shared:
                    shared.append(value)
                parts.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, shared.index(value)))
            else:
                parts.append('<c r="%s"><v>%s</v></c>' % (ref, value))
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return ''.join(parts)


def make_xlsx(path, summary_rows, test_rows=None):
    """Write a small workbook. The Summary sheet is the second part, to test the rels lookup."""
    shared = []
    summary_xml = _sheet_xml([SUMMARY_HEADER] + summary_rows, shared)
    tests_xml = _sheet_xml([TESTS_HEADER] + (test_rows or []), shared)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">%s</sst>'
        % ''.join('<si><t>%s</t></si>' % escape(text) for text in shared)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        '<sheet name="Summary" sheetId="1" r:id="rId7"/>'
        '<sheet name="Agent_Tests" sheetId="2" r:id="rId3"/>'
        '</sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId3" Type="x/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId7" Type="x/worksheet" Target="/xl/worksheets/sheet2.xml"/>'
        '<Relationship Id="rId9" Type="x/sharedStrings" Target="sharedStrings.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', workbook)
        archive.writestr('xl/_rels/workbook.xml.rels', rels)
        archive.writestr('xl/sharedStrings.xml', shared_xml)
        archive.writestr('xl/worksheets/sheet1.xml', tests_xml)
        archive.writestr('xl/worksheets/sheet2.xml', summary_xml)


def summary_row(name, agent_id, state, ips, groups, login, tests=1, owners='Div_A', host=None,
                source=SOURCE, notes=None):
    return [
        name, agent_id, source, state, host if host is not None else 'host-' + name[-2:],
        ips, len(groups.split(';')) if groups else 0, groups, tests, owners, login, notes,
    ]


G_S = 'Group S [900]'
G_A = 'Div_A [100]'
G_B = 'Div_B [200]'
G_X = 'BU_X [300]'
G_Y = 'Ent_Y [400]'


def standard_rows(with_lonely=True):
    rows = [
        summary_row('AAAA - HOST01', 5001, 'online', '192.0.2.11', '; '.join([G_S, G_A, G_Y]), 'good', 3),
        summary_row('AAAA - HOST02', Inline('5002'), Inline('online'), Inline('192.0.2.12'),
                    Inline('; '.join([G_S, G_X, G_A])), Inline('good'), 4),
        summary_row('BBBB 2 - HOST03', 5003.0, 'online', '192.0.2.13', '; '.join([G_S, G_X, G_Y]), 'good'),
        summary_row('CCCC - HOST04', 5004, 'online', '192.0.2.14', '; '.join([G_S, G_A, G_B]), 'good'),
        summary_row('DDDD - HOST05', 5005, 'offline', '192.0.2.15', '; '.join([G_S, G_B]), 'offline'),
        summary_row('EEEE - HOST06', 5006, 'online', '192.0.2.16', '; '.join([G_S, G_B]), 'cant login'),
        summary_row('FFFF - HOST07', 5007, 'online', '192.0.2.17', '; '.join([G_S, G_Y]), 'good'),
        summary_row('GGGG - HOST08', 5008, ' Online ', '192.0.2.18 ; 192.0.2.19', '; '.join([G_B, G_S, G_A]), 'GOOD'),
    ]
    if with_lonely:
        rows.append(summary_row('LONELY', 5009, 'online', '192.0.2.20', '; '.join([G_S, G_A]), 'good'))
    rows.append([None, 5010, SOURCE, 'online', 'blank-name-row'])
    return rows


def entry(prefix, name, aid, status='OK'):
    return scope.PrefixEntry(prefix, name, aid, status)


class ScopeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def workbook(self, rows=None, test_rows=None, name='scope.xlsx'):
        path = self.path(name)
        make_xlsx(path, standard_rows() if rows is None else rows, test_rows)
        return path

    def run_main(self, xlsx, out=None):
        stream = io.StringIO()
        code = scope.main(['--xlsx', xlsx, '--out', out or self.path('out')], stream=stream)
        return code, stream.getvalue()


class ReaderTests(ScopeTestCase):
    def test_reads_rows_with_agent_name_only(self):
        rows = scope.read_scope_workbook(self.workbook())
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0].name, 'AAAA - HOST01')
        self.assertEqual(rows[0].source_name, SOURCE)
        self.assertEqual(rows[0].ips, ['192.0.2.11'])
        self.assertEqual(rows[0].groups, [('Group S', '900'), ('Div_A', '100'), ('Ent_Y', '400')])
        self.assertEqual(rows[0].test_count, 3)
        self.assertEqual(rows[0].test_owner_groups, ['Div_A'])
        self.assertEqual(rows[0].login_state, 'good')

    def test_numbers_are_rendered_without_decimal_part(self):
        rows = scope.read_scope_workbook(self.workbook())
        self.assertEqual(rows[0].agent_id, '5001')
        self.assertEqual(rows[1].agent_id, '5002')  # inline string
        self.assertEqual(rows[2].agent_id, '5003')  # numeric cell written as 5003.0

    def test_inline_strings_are_read(self):
        rows = scope.read_scope_workbook(self.workbook())
        self.assertEqual(rows[1].state, 'online')
        self.assertEqual(rows[1].groups[1], ('BU_X', '300'))

    def test_two_ips_are_split(self):
        rows = scope.read_scope_workbook(self.workbook())
        self.assertEqual(rows[7].ips, ['192.0.2.18', '192.0.2.19'])

    def test_render_number(self):
        self.assertEqual(scope.render_number('5001'), '5001')
        self.assertEqual(scope.render_number('5001.0'), '5001')
        self.assertEqual(scope.render_number('1.2345678E7'), '12345678')
        self.assertEqual(scope.render_number('3000100.0'), '3000100')
        self.assertEqual(scope.render_number('0.5'), '0.5')
        self.assertEqual(scope.render_number(''), '')
        self.assertEqual(scope.render_number('abc'), 'abc')

    def test_parse_group_list(self):
        groups, bad = scope.parse_group_list('Div_A [1]; Name With Space [22];bad entry; Div_A [1]; ')
        self.assertEqual(groups, [('Div_A', '1'), ('Name With Space', '22')])
        self.assertEqual(bad, 1)

    def test_missing_sheet_and_missing_column(self):
        path = self.workbook()
        with self.assertRaises(ConfigurationError):
            scope.read_sheet(path, 'No such sheet')
        bad = self.path('bad.xlsx')
        shared = []
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('xl/workbook.xml',
                             '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                             '<sheets><sheet name="Summary" r:id="rId1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels',
                             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             '<Relationship Id="rId1" Target="worksheets/s.xml"/></Relationships>')
            archive.writestr('xl/worksheets/s.xml', _sheet_xml([['AgentName', 'State']], shared))
        with self.assertRaises(ConfigurationError) as ctx:
            scope.read_scope_workbook(bad)
        self.assertIn('AccountGroups', str(ctx.exception))

    def test_not_a_zip_file(self):
        path = self.path('plain.xlsx')
        with open(path, 'w') as handle:
            handle.write('not a workbook')
        with self.assertRaises(ConfigurationError):
            scope.read_scope_workbook(path)

    def test_doctype_is_refused(self):
        path = self.path('doctype.xlsx')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('xl/workbook.xml', '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><workbook/>')
        with self.assertRaises(ConfigurationError):
            scope.read_scope_workbook(path)

    def test_read_scope_tests(self):
        test_rows = [
            ['AAAA - HOST01', 5001, 'Div_A', 100, 1, 'Test one', 'http-server'],
            ['AAAA - HOST02', 5002, 'Div_A', 100, 1, 'Test one', 'http-server'],
            ['AAAA - HOST01', 5001, 'Div_A', 100, 2, 'Test two', 'agent-to-server'],
            ['AAAA - HOST01', 5001, 'Div_B', 200, 3, 'Test three', 'http-server'],
        ]
        data = scope.read_scope_tests(self.workbook(test_rows=test_rows))
        self.assertEqual(data['agents']['AAAA - HOST01']['http-server'], 2)
        self.assertEqual(data['agents']['AAAA - HOST02']['http-server'], 1)
        self.assertEqual(data['groups']['100']['http-server'], 2)
        self.assertEqual(data['group_test_ids']['100'], set(['1', '2']))
        self.assertEqual(data['group_test_ids']['200'], set(['3']))
        self.assertEqual(data['group_unique_types']['100']['http-server'], 1)
        self.assertEqual(data['group_unique_types']['100']['agent-to-server'], 1)


class PrefixMapTests(ScopeTestCase):
    def derived(self):
        return dict((e.prefix, e) for e in scope.derive_prefix_map(scope.read_scope_workbook(self.workbook())))

    def test_prefix_is_text_before_separator(self):
        self.assertEqual(scope.prefix_of('BBBB 2 - HOST03'), 'BBBB 2')
        self.assertEqual(scope.prefix_of('AAAA - HOST - X'), 'AAAA')
        self.assertEqual(scope.prefix_of('LONELY'), '')

    def test_order_follows_first_appearance(self):
        rows = scope.read_scope_workbook(self.workbook())
        prefixes = [e.prefix for e in scope.derive_prefix_map(rows)]
        self.assertEqual(prefixes, ['AAAA', 'BBBB 2', 'CCCC', 'DDDD', 'EEEE', 'FFFF', 'GGGG'])

    def test_intersection_removes_source_and_is_ok(self):
        entry_a = self.derived()['AAAA']
        self.assertEqual((entry_a.group_name, entry_a.aid, entry_a.status), ('Div_A', '100', 'OK'))

    def test_div_is_preferred_then_bu_then_others(self):
        derived = self.derived()
        self.assertEqual((derived['BBBB 2'].aid, derived['BBBB 2'].status), ('300', 'OK'))  # BU_ over Ent_
        rows = [summary_row('HHHH - HOST09', 1, 'online', '192.0.2.30', '; '.join([G_S, G_Y, G_X, G_A]), 'good')]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(rows)))
        self.assertEqual((result[0].aid, result[0].status), ('100', 'OK'))  # Div_ over BU_ and Ent_

    def test_several_candidates_give_review_with_fallback(self):
        result = self.derived()['CCCC']
        self.assertEqual(result.status, 'REVIEW')
        self.assertEqual((result.group_name, result.aid), ('Div_A', '100'))

    def test_review_fallback_is_most_common_first_group(self):
        rows = [
            summary_row('KKKK - HOST01', 1, 'online', '192.0.2.31', '; '.join([G_S, G_B, G_A]), 'good'),
            summary_row('KKKK - HOST02', 2, 'online', '192.0.2.32', '; '.join([G_A, G_S, G_B]), 'good'),
            summary_row('KKKK - HOST03', 3, 'online', '192.0.2.33', '; '.join([G_A, G_B, G_S]), 'good'),
        ]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(rows)))[0]
        self.assertEqual((result.aid, result.status), ('100', 'REVIEW'))

    def test_no_common_group_gives_review(self):
        rows = [
            summary_row('MMMM - HOST01', 1, 'online', '192.0.2.31', '; '.join([G_S, G_A]), 'good'),
            summary_row('MMMM - HOST02', 2, 'online', '192.0.2.32', '; '.join([G_S, G_B]), 'good'),
        ]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(rows)))[0]
        self.assertEqual(result.status, 'REVIEW')

    def test_only_source_group_gives_empty_review(self):
        rows = [summary_row('NNNN - HOST01', 1, 'online', '192.0.2.31', G_S, 'good')]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(rows)))[0]
        self.assertEqual((result.group_name, result.aid, result.status), ('', '', 'REVIEW'))

    def test_anomaly_device_does_not_hide_division_group(self):
        # HOST03 is not in Div_A. It must not turn the whole prefix into REVIEW.
        rows = [
            summary_row('PPPP - HOST01', 1, 'online', '192.0.2.41', '; '.join([G_S, G_A, G_Y]), 'good'),
            summary_row('PPPP - HOST02', 2, 'online', '192.0.2.42', '; '.join([G_S, G_A, G_Y]), 'good'),
            summary_row('PPPP - HOST03', 3, 'online', '192.0.2.43', '; '.join([G_S, G_Y]), 'good'),
        ]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(rows)))[0]
        self.assertEqual((result.aid, result.status), ('100', 'OK'))
        plans = scope.build_plan(scope.read_scope_workbook(self.workbook(rows)), [result])
        self.assertEqual([p.target_aid for p in plans], ['100', '100', '100'])
        self.assertEqual([bool(p.warnings) for p in plans], [False, False, True])
        self.assertEqual(plans[2].share_aids[0], '100')  # the target is always shared

    def test_majority_division_group_is_ok_but_a_tie_is_review(self):
        majority = [
            summary_row('QQQQ - HOST01', 1, 'online', '192.0.2.51', '; '.join([G_S, G_A, G_B]), 'good'),
            summary_row('QQQQ - HOST02', 2, 'online', '192.0.2.52', '; '.join([G_S, G_A, G_B]), 'good'),
            summary_row('QQQQ - HOST03', 3, 'online', '192.0.2.53', '; '.join([G_S, G_A]), 'good'),
        ]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(majority)))[0]
        self.assertEqual((result.aid, result.status), ('100', 'OK'))
        tie = [
            summary_row('RRRR - HOST01', 1, 'online', '192.0.2.61', '; '.join([G_S, G_A]), 'good'),
            summary_row('RRRR - HOST02', 2, 'online', '192.0.2.62', '; '.join([G_S, G_B]), 'good'),
        ]
        result = scope.derive_prefix_map(scope.read_scope_workbook(self.workbook(tie, name='tie.xlsx')))[0]
        self.assertEqual(result.status, 'REVIEW')

    def test_name_without_separator_uses_text_before_first_hyphen(self):
        self.assertEqual(scope.prefix_of('SSSS-HOST-01 UC'), 'SSSS')
        self.assertEqual(scope.prefix_of('SSSS - HOST-01'), 'SSSS')
        self.assertEqual(scope.prefix_of('NOHYPHEN'), '')

    def test_write_and_load_round_trip(self):
        entries = [entry('AAAA', 'Div_A', '100'), entry('BBBB 2', 'BU_X', '300', 'REVIEW')]
        path = self.path('prefix_map.csv')
        self.assertTrue(scope.write_prefix_map(path, entries))
        self.assertEqual(scope.load_prefix_map(path), entries)

    def test_write_never_overwrites(self):
        path = self.path('prefix_map.csv')
        with open(path, 'w') as handle:
            handle.write('operator edit\n')
        self.assertFalse(scope.write_prefix_map(path, [entry('AAAA', 'Div_A', '100')]))
        with open(path) as handle:
            self.assertEqual(handle.read(), 'operator edit\n')

    def test_load_checks_values(self):
        path = self.path('prefix_map.csv')
        cases = [
            'prefix,group_name,aid,status\nAAAA,Div_A,10x,OK\n',
            'prefix,group_name,aid,status\nAAAA,Div_A,100,MAYBE\n',
            'prefix,group_name,aid,status\nAAAA,Div_A,100,OK\naaaa,Div_B,200,OK\n',
            'prefix,aid\nAAAA,100\n',
        ]
        for text in cases:
            with open(path, 'w') as handle:
                handle.write(text)
            with self.assertRaises(ConfigurationError):
                scope.load_prefix_map(path)
        with open(path, 'w') as handle:
            handle.write('prefix,group_name,aid,status\n# note,,,\nAAAA,Div_A,100,ok\n')
        self.assertEqual(scope.load_prefix_map(path), [entry('AAAA', 'Div_A', '100')])


class PlanTests(ScopeTestCase):
    def plans(self, prefix_map=None, rows=None):
        data = scope.read_scope_workbook(self.workbook(rows))
        if prefix_map is None:
            prefix_map = scope.derive_prefix_map(data)
        plans = scope.build_plan(data, prefix_map)
        return dict((p.name, p) for p in plans)

    def test_statuses(self):
        plans = self.plans()
        self.assertEqual(plans['AAAA - HOST01'].status, 'READY')
        self.assertEqual(plans['AAAA - HOST02'].status, 'READY')
        self.assertEqual(plans['BBBB 2 - HOST03'].status, 'READY')
        self.assertEqual(plans['CCCC - HOST04'].status, 'NEEDS_REVIEW')
        self.assertEqual(plans['DDDD - HOST05'].status, 'EXCLUDED_OFFLINE')
        self.assertEqual(plans['EEEE - HOST06'].status, 'EXCLUDED_LOGIN')
        self.assertEqual(plans['FFFF - HOST07'].status, 'READY')
        self.assertEqual(plans['LONELY'].status, 'NEEDS_REVIEW')

    def test_state_and_login_compare_ignores_case_and_spaces(self):
        plan = self.plans(prefix_map=[entry('GGGG', 'Div_A', '100')])['GGGG - HOST08']
        self.assertEqual(plan.state, 'Online')
        self.assertEqual(plan.login_state, 'GOOD')
        self.assertEqual(plan.status, 'READY')

    def test_plan_values(self):
        plan = self.plans()['AAAA - HOST01']
        self.assertEqual(plan.ip, '192.0.2.11')
        self.assertEqual(plan.agent_id, '5001')
        self.assertEqual(plan.source_aid, '900')
        self.assertEqual(plan.target_aid, '100')
        self.assertEqual(plan.target_name, 'Div_A')
        self.assertEqual(plan.warnings, [])

    def test_first_ip_is_used(self):
        self.assertEqual(self.plans()['GGGG - HOST08'].ip, '192.0.2.18')

    def test_review_reason_and_missing_prefix(self):
        plans = self.plans()
        self.assertIn('REVIEW', plans['CCCC - HOST04'].reason)
        self.assertIn('no prefix', plans['LONELY'].reason)
        only_a = self.plans(prefix_map=[entry('AAAA', 'Div_A', '100')])
        self.assertEqual(only_a['BBBB 2 - HOST03'].status, 'NEEDS_REVIEW')
        self.assertIn('not in the prefix map', only_a['BBBB 2 - HOST03'].reason)

    def test_offline_wins_over_review(self):
        plans = self.plans(prefix_map=[])
        self.assertEqual(plans['DDDD - HOST05'].status, 'EXCLUDED_OFFLINE')
        self.assertEqual(plans['EEEE - HOST06'].status, 'EXCLUDED_LOGIN')

    def test_target_not_in_group_list_adds_target_and_warns(self):
        mapping = [entry('FFFF', 'Div_B', '200')]
        plan = self.plans(prefix_map=mapping)['FFFF - HOST07']
        self.assertEqual(plan.status, 'READY')
        self.assertEqual(plan.share_aids, ['200', '900', '400'])
        self.assertEqual(plan.warnings, [scope.WARNING_TARGET_NOT_IN_GROUPS])

    def test_share_order_target_first_then_list_order_without_duplicates(self):
        plan = self.plans(prefix_map=[entry('GGGG', 'Div_A', '100')])['GGGG - HOST08']
        self.assertEqual(plan.share_aids, ['100', '200', '900'])
        self.assertEqual(plan.warnings, [])
        plan = self.plans(prefix_map=[entry('GGGG', 'Div_B', '200')])['GGGG - HOST08']
        self.assertEqual(plan.share_aids, ['200', '900', '100'])

    def test_target_found_by_group_name_when_aid_is_empty(self):
        plan = self.plans(prefix_map=[entry('GGGG', 'div_a', '')])['GGGG - HOST08']
        self.assertEqual((plan.status, plan.target_aid), ('READY', '100'))
        plan = self.plans(prefix_map=[entry('GGGG', 'Unknown group', '')])['GGGG - HOST08']
        self.assertEqual(plan.status, 'NEEDS_REVIEW')

    def test_target_equal_to_source_needs_review(self):
        plan = self.plans(prefix_map=[entry('GGGG', 'Group S', '900')])['GGGG - HOST08']
        self.assertEqual(plan.status, 'NEEDS_REVIEW')

    def test_operator_can_fix_review_entry(self):
        data = scope.read_scope_workbook(self.workbook())
        mapping = scope.derive_prefix_map(data)
        mapping = [entry(e.prefix, 'Div_B', '200') if e.prefix == 'CCCC' else e for e in mapping]
        plans = dict((p.name, p) for p in scope.build_plan(data, mapping))
        self.assertEqual(plans['CCCC - HOST04'].status, 'READY')
        self.assertEqual(plans['CCCC - HOST04'].share_aids, ['200', '900', '100'])

    def test_duplicate_ip_needs_review(self):
        rows = [
            summary_row('AAAA - HOST01', 1, 'online', '192.0.2.11', '; '.join([G_S, G_A]), 'good'),
            summary_row('AAAA - HOST02', 2, 'online', '192.0.2.11', '; '.join([G_S, G_A]), 'good'),
        ]
        plans = self.plans(rows=rows)
        self.assertEqual(plans['AAAA - HOST01'].status, 'READY')
        self.assertEqual(plans['AAAA - HOST02'].status, 'NEEDS_REVIEW')

    def test_duplicate_agent_id_is_dropped_with_warning(self):
        rows = [
            summary_row('AAAA - HOST01', 7, 'online', '192.0.2.11', '; '.join([G_S, G_A]), 'good'),
            summary_row('AAAA - HOST02', 7, 'online', '192.0.2.12', '; '.join([G_S, G_A]), 'good'),
        ]
        plans = self.plans(rows=rows)
        self.assertEqual(plans['AAAA - HOST01'].agent_id, '7')
        self.assertEqual(plans['AAAA - HOST02'].agent_id, '')
        self.assertEqual(plans['AAAA - HOST02'].status, 'READY')
        self.assertEqual(len(plans['AAAA - HOST02'].warnings), 1)

    def test_no_valid_ip_needs_review(self):
        rows = [summary_row('AAAA - HOST01', 1, 'online', 'not-an-ip', '; '.join([G_S, G_A]), 'good')]
        self.assertEqual(self.plans(rows=rows)['AAAA - HOST01'].status, 'NEEDS_REVIEW')


class OutputTests(ScopeTestCase):
    def ready_plans(self):
        data = scope.read_scope_workbook(self.workbook())
        return scope.build_plan(data, scope.derive_prefix_map(data))

    def test_inventory_round_trip_through_loader(self):
        plans = self.ready_plans()
        path = self.path('inventory.csv')
        count = scope.write_inventory(path, plans)
        agents = load_inventory(path)
        ready = [p for p in plans if p.status == 'READY']
        self.assertEqual(count, len(ready))
        self.assertEqual(len(agents), len(ready))
        by_name = dict((a.hostname, a) for a in agents)
        self.assertNotIn('CCCC - HOST04', by_name)
        self.assertNotIn('DDDD - HOST05', by_name)
        first = by_name['AAAA - HOST01']
        self.assertEqual(first.kind, 'agent')
        self.assertEqual(first.ip_address, '192.0.2.11')
        self.assertEqual(first.agent_id, '5001')
        self.assertEqual(first.target_aid, '100')
        self.assertEqual(first.share_aids, ['100', '900', '400'])
        self.assertIn(first.target_aid, first.share_aids)

    def test_inventory_header_is_v3(self):
        path = self.path('inventory.csv')
        scope.write_inventory(path, self.ready_plans())
        with open(path) as handle:
            self.assertEqual(handle.readline().strip(), 'type,name,ip_address,agent_id,target_aid,share_aids')

    def test_report_lists_all_devices(self):
        plans = self.ready_plans()
        path = self.path('scope_report.csv')
        scope.write_scope_report(path, plans)
        with open(path, newline='') as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), len(plans) + 1)
        self.assertTrue(lines[0].startswith('name,ip_address,agent_id,prefix,status,reason'))
        self.assertTrue(any('EXCLUDED_OFFLINE' in line for line in lines))

    def test_archive_existing_uses_numbered_suffix(self):
        path = self.path('inventory.csv')
        for text in ('one', 'two', 'three'):
            with open(path, 'w') as handle:
                handle.write(text)
            scope.archive_existing(path)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(self.path('inventory.old.csv')))
        self.assertTrue(os.path.exists(self.path('inventory.old.2.csv')))
        self.assertTrue(os.path.exists(self.path('inventory.old.3.csv')))
        self.assertIsNone(scope.archive_existing(path))


class CliTests(ScopeTestCase):
    def test_exit_3_when_review_needed_and_files_written(self):
        code, text = self.run_main(self.workbook())
        self.assertEqual(code, scope.EXIT_NEEDS_REVIEW)
        out = self.path('out')
        for name in ('prefix_map.csv', 'inventory.csv', 'scope_report.csv'):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertIn('READY', text)
        self.assertIn('CCCC', text)
        self.assertIn('(no prefix)', text)
        self.assertEqual(len(load_inventory(os.path.join(out, 'inventory.csv'))), 4)

    def test_exit_0_when_nothing_needs_review(self):
        xlsx = self.workbook(rows=standard_rows(with_lonely=False))
        out = self.path('out')
        code, _text = self.run_main(xlsx, out)
        self.assertEqual(code, 3)  # CCCC and GGGG are ambiguous
        mapping = scope.load_prefix_map(os.path.join(out, 'prefix_map.csv'))
        fixed = [entry(e.prefix, 'Div_B', '200') if e.prefix in ('CCCC', 'GGGG') else e for e in mapping]
        os.remove(os.path.join(out, 'prefix_map.csv'))
        scope.write_prefix_map(os.path.join(out, 'prefix_map.csv'), fixed)
        code, text = self.run_main(xlsx, out)
        self.assertEqual(code, scope.EXIT_OK)
        self.assertIn('kept, not changed', text)
        self.assertEqual(len(load_inventory(os.path.join(out, 'inventory.csv'))), 6)

    def test_second_run_keeps_operator_prefix_map(self):
        xlsx = self.workbook()
        out = self.path('out')
        self.run_main(xlsx, out)
        map_path = os.path.join(out, 'prefix_map.csv')
        with open(map_path, 'a') as handle:
            handle.write('# operator note,,,\n')
        with open(map_path) as handle:
            before = handle.read()
        self.run_main(xlsx, out)
        with open(map_path) as handle:
            self.assertEqual(handle.read(), before)

    def test_old_inventory_is_moved(self):
        out = self.path('out')
        os.makedirs(out)
        with open(os.path.join(out, 'inventory.csv'), 'w') as handle:
            handle.write('hostname,ip_address\nOLD1,192.0.2.99\n')
        xlsx = self.workbook()
        self.run_main(xlsx, out)
        with open(os.path.join(out, 'inventory.old.csv')) as handle:
            self.assertIn('OLD1', handle.read())
        self.run_main(xlsx, out)
        self.assertTrue(os.path.isfile(os.path.join(out, 'inventory.old.2.csv')))
        self.assertTrue(load_inventory(os.path.join(out, 'inventory.csv')))

    def test_exit_1_on_read_errors(self):
        code, text = self.run_main(self.path('missing.xlsx'))
        self.assertEqual(code, scope.EXIT_READ_ERROR)
        self.assertIn('ERROR', text)
        plain = self.path('plain.xlsx')
        with open(plain, 'w') as handle:
            handle.write('x')
        self.assertEqual(self.run_main(plain)[0], scope.EXIT_READ_ERROR)

    def test_exit_1_on_bad_prefix_map(self):
        out = self.path('out')
        os.makedirs(out)
        with open(os.path.join(out, 'prefix_map.csv'), 'w') as handle:
            handle.write('prefix,group_name,aid,status\nAAAA,Div_A,100,BROKEN\n')
        self.assertEqual(self.run_main(self.workbook(), out)[0], scope.EXIT_READ_ERROR)

    def test_summary_has_no_host_names_or_ips(self):
        _code, text = self.run_main(self.workbook())
        for rows in (scope.read_scope_workbook(self.workbook()),):
            for row in rows:
                self.assertNotIn(row.name, text)
                self.assertNotIn(row.hostname, text)
                for ip in row.ips:
                    self.assertNotIn(ip, text)
        self.assertNotIn('192.0.2.', text)
        self.assertNotIn('HOST0', text)
        data = scope.read_scope_workbook(self.workbook())
        plans = scope.build_plan(data, scope.derive_prefix_map(data))
        joined = '\n'.join(scope.format_summary(plans))
        self.assertNotIn('192.0.2.', joined)
        self.assertNotIn('host-', joined)

    def test_summary_counts(self):
        data = scope.read_scope_workbook(self.workbook())
        plans = scope.build_plan(data, scope.derive_prefix_map(data))
        counts = scope.count_by_status(plans)
        self.assertEqual(counts['READY'], 4)
        self.assertEqual(counts['EXCLUDED_OFFLINE'], 1)
        self.assertEqual(counts['EXCLUDED_LOGIN'], 1)
        self.assertEqual(counts['NEEDS_REVIEW'], 3)
        self.assertEqual(sum(counts.values()), len(plans))


class ExampleFileTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def test_example_files_load(self):
        agents = load_inventory(os.path.join(self.ROOT, 'examples', 'inventory.example.csv'))
        self.assertEqual(len(agents), 3)
        for agent in agents:
            self.assertIn(agent.target_aid, agent.share_aids)
        entries = scope.load_prefix_map(os.path.join(self.ROOT, 'examples', 'prefix_map.example.csv'))
        self.assertEqual([e.status for e in entries], ['OK', 'OK', 'REVIEW'])


if __name__ == '__main__':
    unittest.main()
