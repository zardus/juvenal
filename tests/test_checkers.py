"""Unit tests for checkers — script execution."""

from juvenal.checkers import run_script


class TestRunScript:
    def test_success(self, tmp_path):
        result = run_script("echo hello", str(tmp_path))
        assert result.exit_code == 0
        assert "hello" in result.output

    def test_failure(self, tmp_path):
        result = run_script("exit 1", str(tmp_path))
        assert result.exit_code == 1

    def test_stderr_captured(self, tmp_path):
        result = run_script("echo err >&2", str(tmp_path))
        assert "err" in result.output

    def test_combined_stdout_stderr(self, tmp_path):
        result = run_script("echo out && echo err >&2", str(tmp_path))
        assert "out" in result.output
        assert "err" in result.output

    def test_timeout(self, tmp_path):
        result = run_script("sleep 60", str(tmp_path), timeout=1)
        assert result.exit_code == 1
        assert "timed out" in result.output

    def test_env_vars(self, tmp_path):
        result = run_script("echo $MY_TEST_VAR", str(tmp_path), env={"MY_TEST_VAR": "hello123"})
        assert result.exit_code == 0
        assert "hello123" in result.output

    def test_working_dir(self, tmp_path):
        (tmp_path / "marker.txt").write_text("found")
        result = run_script("cat marker.txt", str(tmp_path))
        assert result.exit_code == 0
        assert "found" in result.output

    def test_nonzero_exit_code(self, tmp_path):
        result = run_script("exit 42", str(tmp_path))
        assert result.exit_code == 42
