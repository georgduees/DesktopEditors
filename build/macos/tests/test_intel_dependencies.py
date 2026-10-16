import ast
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


MACOS_DIR = Path(__file__).resolve().parents[1]
SOURCES = MACOS_DIR.parents[1] / "core" / "Common" / "3dParty"
BUILDERS = ("boost", "cef", "icu", "openssl", "v8")


class IntelDependenciesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        for relative in ("build_3rdparty_common.py",) + tuple(
            f"{builder}/nc-build.py" for builder in BUILDERS
        ):
            source = SOURCES / relative
            destination = cls.root / "Common" / "3dParty" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        subprocess.run(
            ["git", "apply", str(MACOS_DIR / "intel-dependencies.patch")],
            cwd=cls.root,
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def load_function(self, relative, name, **namespace):
        source = ast.parse(
            (self.root / "Common" / "3dParty" / relative).read_text()
        )
        function = next(
            node for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        module = ast.Module(body=[function], type_ignores=[])
        exec(compile(module, relative, "exec"), namespace)
        return namespace[name]

    def macos_context(self, arm64=False):
        return SimpleNamespace(
            is_macos=lambda: True,
            is_apple_silicon=lambda: arm64,
            is_arm64=lambda: arm64,
            is_linux=lambda: False,
            is_windows=lambda: False,
            target_arch=lambda: "arm64" if arm64 else "x64",
            create_install_dir=Mock(),
            create_install_dir_ok_marker=Mock(),
            fix_terminal_encoding=Mock(),
            run_command=Mock(),
            abort_op=Mock(side_effect=AssertionError("Unexpected platform rejection")),
            install_dir=Path("/test/install"),
            work_dir=Path("/test/work"),
        )

    def test_patched_scripts_parse(self):
        for source in self.root.rglob("*.py"):
            with self.subTest(source=source):
                ast.parse(source.read_text())

    def test_macos_detection_is_not_architecture_specific(self):
        for platform, expected in (("darwin", True), ("linux", False), ("win32", False)):
            with self.subTest(platform=platform):
                detect = self.load_function(
                    "build_3rdparty_common.py", "is_macos",
                    sys=SimpleNamespace(platform=platform),
                )
                self.assertEqual(detect(), expected)

    def test_cef_uses_matching_macos_distribution(self):
        for arm64, expected in ((False, "macosx64"), (True, "macosarm64")):
            with self.subTest(arm64=arm64):
                detect = self.load_function(
                    "cef/nc-build.py", "cef_platform",
                    nc=self.macos_context(arm64),
                    os=SimpleNamespace(environ={}),
                )
                self.assertEqual(detect(), expected)

    def test_boost_uses_matching_native_architecture(self):
        for arm64, architecture in ((False, "x86"), (True, "arm")):
            with self.subTest(arm64=arm64):
                nc = self.macos_context(arm64)
                build = self.load_function(
                    "boost/nc-build.py", "build_and_install",
                    nc=nc, modules_needed=["system"],
                )
                build()
                commands = [call.args[0] for call in nc.run_command.call_args_list]
                self.assertEqual(commands[0][0], "./bootstrap.sh")
                self.assertIn("toolset=clang", commands[1])
                self.assertIn(f"architecture={architecture}", commands[1])
                self.assertIn("address-model=64", commands[1])

    def test_icu_and_openssl_accept_both_macos_architectures(self):
        for builder in ("icu", "openssl"):
            for arm64 in (False, True):
                with self.subTest(builder=builder, arm64=arm64):
                    nc = self.macos_context(arm64)
                    fix_names = Mock()
                    build = self.load_function(
                        f"{builder}/nc-build.py", "build_and_install",
                        nc=nc,
                        os=SimpleNamespace(cpu_count=lambda: 2),
                        fix_macos_install_names=fix_names,
                    )
                    build()
                    self.assertEqual(nc.run_command.call_count, 3)
                    nc.create_install_dir_ok_marker.assert_called_once()
                    if builder == "icu":
                        fix_names.assert_called_once_with(nc.install_dir)

    def test_v8_uses_matching_macos_target(self):
        for arm64, cpu in ((False, "x64"), (True, "arm64")):
            with self.subTest(arm64=arm64):
                arguments = self.load_function(
                    "v8/nc-build.py", "get_gn_args_file_content",
                    nc=self.macos_context(arm64),
                    get_cpu=lambda: cpu,
                    Path=Path,
                    shutil=SimpleNamespace(which=lambda name: "/usr/bin/clang"),
                )()
                self.assertIn('target_os="mac"', arguments)
                self.assertIn(f'target_cpu="{cpu}"', arguments)
                self.assertIn(f'v8_target_cpu="{cpu}"', arguments)


if __name__ == "__main__":
    unittest.main()
