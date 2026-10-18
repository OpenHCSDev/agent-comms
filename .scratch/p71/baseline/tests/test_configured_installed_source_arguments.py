"""Private CLI declarations reach the existing installer-origin validator."""
from contextlib import redirect_stderr
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_comms.field_codec import FieldCodec
from publish_retained_summary import (
    ArchivePackageDirectUrl, InstalledSource, PackageArchiveInfo,
    PackageVcsInfo, VcsPackageDirectUrl,
)


class ConfiguredInstalledSourceArguments(unittest.TestCase):
    def source(self, origin):
        return InstalledSource('agent_comms', 'declared-git-head', '/installed/agent_comms',
                               1, 1, True, origin, 'original-inventory')

    def test_file_wheel_and_remaining_command_reach_original_validator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / 'original.whl'
            wheel.write_bytes(b'original reviewed archive')
            sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
            source = self.source(ArchivePackageDirectUrl(wheel.as_uri(), PackageArchiveInfo()))
            declaration = root / 'source.json'
            declaration.write_text(json.dumps(FieldCodec.encode(source)))
            decoded, artifacts, arguments = InstalledSource.command_arguments([
                'output', 'native', 'original-python', '--condition-application',
                'task-memory', 'checkpoint', '--installed-source', str(declaration),
                '--archive', str(wheel), sha256,
            ])
            self.assertEqual(decoded, source)
            self.assertEqual(arguments, ['output', 'native', 'original-python',
                                        '--condition-application', 'task-memory', 'checkpoint'])
            self.assertEqual(decoded.require_package('agent_comms', Path(source.location),
                             FieldCodec.encode(source.direct_url), artifacts), source.head)
            wheel.write_bytes(b'changed archive')
            with self.assertRaisesRegex(RuntimeError, 'Reviewed artifact changed'):
                decoded.require_package('agent_comms', Path(source.location),
                                        FieldCodec.encode(source.direct_url), artifacts)

    def test_imported_location_and_origin_remain_required(self):
        source = self.source(VcsPackageDirectUrl('git+https://original/repo',
                            PackageVcsInfo('git', 'declared-git-head', 'declared-git-head')))
        with self.assertRaisesRegex(RuntimeError, 'another imported package'):
            source.require_package('agent_comms', Path('/other/package'),
                                   FieldCodec.encode(source.direct_url), ())
        with self.assertRaisesRegex(RuntimeError, 'another installer origin'):
            source.require_package('agent_comms', Path(source.location),
                                   {'url': 'another-origin'}, ())
        with self.assertRaisesRegex(RuntimeError, 'source bytes are not verified'):
            replace(source, byte_equal=False).require_package('agent_comms',
                Path(source.location), FieldCodec.encode(source.direct_url), ())

    def test_vcs_origin_uses_same_source_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.source(VcsPackageDirectUrl('git+https://original/repo',
                                PackageVcsInfo('git', 'declared-git-head', 'declared-git-head')))
            declaration = Path(temporary) / 'source.json'
            declaration.write_text(json.dumps(FieldCodec.encode(source)))
            decoded, artifacts, arguments = InstalledSource.command_arguments([
                '--installed-source', str(declaration), 'output', 'native', 'original-python'])
            self.assertEqual(arguments, ['output', 'native', 'original-python'])
            self.assertEqual(artifacts, ())
            self.assertEqual(decoded.require_package('agent_comms', Path(source.location),
                             FieldCodec.encode(source.direct_url), artifacts), source.head)

    def test_missing_declaration_refuses_at_cli_boundary(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            InstalledSource.command_arguments(['output', 'native', 'original-python'])
        self.assertEqual(failure.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
