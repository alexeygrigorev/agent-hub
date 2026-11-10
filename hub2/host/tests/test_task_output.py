"""Exact-attempt headless stdout capture, without live processes or credentials."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import task_output as output
from agent_hub_host import native_launch


class TaskOutputTest(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[3] / '.local' / 'tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task, self.native = 'hub2-session-1', '11111111-1111-4111-8111-111111111111'
        self.spec = {'workspace': str(self.root), 'operation_id': 'attempt-1', 'epoch': 2}
        self.expected = dict(self.spec, tag='task-' + self.task)
        self.binding = output.prepare_output(self.task, self.spec, 'local-codex', 'codex', 'synthetic prompt')
        self.directory = self.root.joinpath(*self.binding['parts'])
        self.record = dict(session_id=self.native, tag=self.expected['tag'], workspace=str(self.root),
                           created_at_ms=int(time.time()*1000)-1000, task_output=self.binding)
        self.start = dict(schema_version=1, parent_session=dict(id=self.native, tag=self.expected['tag'], workspace=str(self.root)),
                          cwd=str(self.root), output_dir=str(self.directory), started_ms=int(time.time()*1000),
                          profile='local-codex', engine='codex', prompt_sha256=self.binding['prompt_sha256'])
        self.save()

    def save(self):
        (self.root / '.local' / ('launch-' + self.task + '.json')).write_text(json.dumps(self.record))
        (self.directory / 'START.json').write_text(json.dumps(self.start))
        (self.directory / 'stdout.log').write_text('actual child output\n')
        (self.directory / 'stderr.log').write_text('must not be returned')

    def capture(self):
        return output.capture_task_output(self.task, self.expected, self.native)

    def test_reads_stdout_while_task_is_still_running_without_result_file(self):
        self.assertFalse((self.directory/'RESULT.json').exists())
        self.assertEqual(self.capture(), 'actual child output\n')

    def test_partial_task_start_is_empty_and_old_nonprofile_launch_uses_pty(self):
        (self.directory/'START.json').unlink()
        self.assertEqual(self.capture(), '')
        self.record.pop('task_output');self.save()
        self.assertIsNone(self.capture())

    def test_foreign_parent_tag_workspace_or_native_never_returns_output(self):
        original=copy.deepcopy(self.start)
        for field, value in [('id','foreign'),('tag','other'),('workspace',str(self.root.parent))]:
            with self.subTest(field=field):
                self.start=copy.deepcopy(original);self.start['parent_session'][field]=value;self.save()
                with self.assertRaisesRegex(RuntimeError, 'task_output_start_conflict'):self.capture()

    def test_wrong_profile_prompt_time_cwd_and_output_directory_are_refused(self):
        original=copy.deepcopy(self.start)
        for field,value in [('profile','foreign'),('engine','foreign'),('prompt_sha256','foreign'),
                            ('started_ms',1),('started_ms',int(time.time()*1000)+60000),
                            ('cwd',str(self.root.parent)),('output_dir',str(self.root.parent))]:
            with self.subTest(field=field,value=value):
                self.start=dict(original,**{field:value});self.save()
                with self.assertRaisesRegex(RuntimeError, 'task_output_start_conflict'):self.capture()

    def test_previous_attempt_epoch_cannot_capture(self):
        for field,value in [('operation_id','attempt-old'),('epoch',1)]:
            with self.subTest(field=field):
                expected=dict(self.expected,**{field:value})
                with self.assertRaisesRegex(RuntimeError, 'task_output_fence_conflict'):
                    output.capture_task_output(self.task,expected,self.native)

    def test_serialized_path_traversal_is_refused(self):
        self.binding['parts']=['..','foreign'];self.save()
        with self.assertRaisesRegex(RuntimeError, 'task_output_fence_conflict'):self.capture()

    def test_symlinked_manifest_or_stdout_is_never_followed(self):
        for name in ['START.json','stdout.log']:
            with self.subTest(name=name):
                self.save();path=self.directory/name;path.unlink();path.symlink_to(self.root/'foreign')
                (self.root/'foreign').write_text('private sentinel')
                with self.assertRaises(OSError):self.capture()
                path.unlink()

    def test_symlinked_directory_and_launch_evidence_are_never_followed(self):
        path=self.root/'.local'/('launch-'+self.task+'.json');data=path.read_text();path.unlink()
        target=self.root/'foreign.json';target.write_text(data);path.symlink_to(target)
        with self.assertRaises(OSError):self.capture()
        path.unlink();path.write_text(data)
        old=self.directory.with_name('old');self.directory.rename(old);self.directory.symlink_to(old,target_is_directory=True)
        with self.assertRaises(OSError):self.capture()

    def test_oversized_start_is_refused_and_stdout_tail_is_bounded(self):
        (self.directory/'START.json').write_text('x'*131073)
        with self.assertRaisesRegex(RuntimeError,'task_output_record_too_large'):self.capture()
        self.save();(self.directory/'stdout.log').write_text('x'*100000+'TAIL')
        self.assertEqual(len(self.capture().encode()),16384)
        self.assertTrue(self.capture().endswith('TAIL'))

    def test_ack_payload_including_escaped_controls_is_bounded(self):
        for data in ['x'*20000,'\x00'*20000,'😀'*20000]:
            bounded=output.bounded_capture(data)
            self.assertLessEqual(len(json.dumps({'output':bounded},ensure_ascii=False).encode()),8000)

    def test_profile_launch_passes_exact_output_directory_and_records_binding(self):
        config={'routes':{'codex':{'provider':'codex','profile':'local-codex'}}}
        launcher=native_launch.NativeLauncher(config)
        spec=dict(self.spec,operation_id='attempt-next',engine='codex',model='gpt-6.1-sol',timeout_seconds=90)
        command=launcher.profile_command(self.task,spec,config['routes']['codex'],'synthetic prompt')
        self.assertEqual(command[command.index('--output-dir')+1],str(self.root.joinpath(*spec['_task_output']['parts'])))
        self.assertNotEqual(spec['_task_output']['parts'],self.binding['parts'])

    def test_unsupported_platform_keeps_existing_launch_path(self):
        with mock.patch.object(os,'supports_dir_fd',set()):
            self.assertIsNone(output.prepare_output(self.task,self.spec,'local','codex','prompt'))
