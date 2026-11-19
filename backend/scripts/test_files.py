"""Which files are tests: shared by the Tester's runner (run_tests.py).

check_changes.py (the Coder) has the same lists; keep the two in step.
"""
import fnmatch

# Folders and file names that hold tests, in the common languages' layouts.
TEST_DIRS = {"test", "tests", "__tests__", "__mocks__", "spec", "specs", "testing", "testdata",
             "test_data", "fixtures", "__snapshots__", "e2e", "cypress", "playwright"}
TEST_NAMES = ["test_*.py", "*_test.py", "tests.py", "conftest.py", "*.test.*", "*.spec.*", "*_test.go",
              "*Test.java", "*Tests.java", "*Test.kt", "*Tests.kt", "*Test.cs", "*Tests.cs", "*_spec.rb",
              "*_test.rb", "*Test.php", "*_test.exs", "*.snap", "test_*.gd"]


def is_test_file(path: str) -> bool:
    """A test file (not test setup such as package.json)."""
    parts = path.replace("\\", "/").split("/")
    if any(p.lower() in TEST_DIRS for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(parts[-1], pat) for pat in TEST_NAMES)
