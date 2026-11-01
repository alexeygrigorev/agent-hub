"""Codex engine refused to start on Tabo (intake 61bba72f smoke): the codex
CLI replaces .codex/config.toml on launch and writes CRLF on Windows, the
line-anchored trust-table matches did not match a trailing \\r, and an
earlier provisioning run re-appended tables that were already present. TOML
forbids duplicate table headers, so the engine died at startup with
``duplicate key`` (exit 1, stderr only — the launcher captured neither).
collapse_duplicate_toml_tables heals files that already carry duplicates and
normalizes CRLF so the next engine rewrite cannot reintroduce them.

Table headers are built from ``Path.resolve()`` exactly as the code does, so
the expectations hold on every platform.
"""
import json
import unittest
from pathlib import Path

from agent_hub_host import provisioning as p


def project_table(directory):
    header = '[projects.' + json.dumps(str(Path(directory).resolve())) + ']'
    return header + '\ntrust_level = "trusted"\n', header


def hook_table(hooks_file):
    key = str(Path(hooks_file).resolve()) + ':stop:0:0'
    header = '[hooks.state.' + json.dumps(key) + ']'
    return header + '\ntrusted_hash = "sha256:abc"\n', header


class CollapseDuplicateTomlTablesTests(unittest.TestCase):
    def test_crlf_entry_is_recognized_and_not_appended_twice(self):
        block, header = project_table('probe-dir')
        content = 'approval_policy = "never"\r\n\r\n' + block.replace('\n', '\r\n')
        out = p.ensure_toml_trusted_projects(content, ['probe-dir'])
        self.assertEqual(out.count(header), 1)
        self.assertNotIn('\r\n', out)
        # Trust is still upgraded in place on the surviving table.
        self.assertEqual(out.count('trust_level = "trusted"'), 1)

    def test_existing_duplicate_tables_collapse_to_the_first(self):
        block, header = project_table('probe-dir')
        hook, _ = hook_table('probe-hooks.json')
        content = 'x = "y"\n\n' + block + '\n' + hook + '\n' + block
        out = p.ensure_toml_trusted_projects(content, ['probe-dir'])
        self.assertEqual(out.count(header), 1)
        self.assertIn('x = "y"', out)
        self.assertEqual(out.count(hook), 1)

    def test_distinct_tables_are_all_kept(self):
        first, first_header = project_table('probe-dir')
        second, second_header = project_table('probe-dir-other')
        out = p.ensure_toml_trusted_projects(first + '\n' + second, ['probe-dir'])
        self.assertIn(second_header, out)
        self.assertEqual(out.count('trust_level = "trusted"'), 2)

    def test_duplicate_hook_tables_collapse(self):
        hook, header = hook_table('probe-hooks.json')
        out = p.ensure_toml_trusted_hooks(hook + '\n' + hook,
                                          Path('probe-hooks.json'))
        self.assertEqual(out.count(header), 1)

    def test_missing_trust_is_still_added_after_collapse(self):
        block, header = project_table('probe-dir')
        untrusted = header + '\nowner = "me"\n'
        out = p.ensure_toml_trusted_projects(untrusted + '\n' + block,
                                             ['probe-dir'])
        self.assertEqual(out.count(header), 1)
        # The kept first table gains the trust key and keeps its sibling.
        kept = out.split(header, 1)[1].split('\n[', 1)[0]
        self.assertIn('trust_level = "trusted"', kept)
        self.assertIn('owner = "me"', out)

    def test_content_without_tables_is_untouched(self):
        content = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        self.assertEqual(
            p.collapse_duplicate_toml_tables(content, '[projects.'), content)


if __name__ == '__main__':
    unittest.main()
