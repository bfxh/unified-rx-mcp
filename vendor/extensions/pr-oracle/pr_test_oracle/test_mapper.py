"""Static analysis to map changed source files to candidate test files."""

import os
from pathlib import Path

from simple_logger.logger import get_logger

from pr_test_oracle.models import TestMapping

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

# Test infrastructure files that should not be treated as test cases
_EXCLUDED_FILES = frozenset(
    {
        "__init__.py",
        "conftest.py",
        "setup.py",
        "conftest.js",
        "jest.config.js",
        "jest.config.ts",
        "vitest.config.ts",
        "karma.conf.js",
    }
)

# Config files whose changes may affect all tests
_CONFIG_FILES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "conftest.py",
        ".env",
        "requirements.txt",
        "requirements-dev.txt",
        "Makefile",
    }
)

# Recognized source file extensions eligible for candidate test mapping
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".java",
        ".rb",
        ".rs",
        ".cs",
        ".php",
        ".sh",
        ".bash",
    }
)


class TestMapper:
    """Maps changed source files to candidate test files using static analysis."""

    def __init__(self, repo_path: str, test_patterns: list[str] | None = None) -> None:
        """Initialize with repository path and test file glob patterns.

        Args:
            repo_path: Path to the repository root.
            test_patterns: Glob patterns for test files. Defaults to common Python patterns.
        """
        self._repo = Path(repo_path)
        self._patterns = test_patterns or ["tests/**/*.py", "test_*.py"]
        self._test_files: list[str] | None = None  # Lazy-loaded cache

    def discover_test_files(self) -> list[str]:
        """Find all test files in the repository matching the configured patterns.

        Returns:
            List of test file paths relative to repo root.
        """
        if self._test_files is not None:
            return self._test_files

        test_files: set[str] = set()
        for pattern in self._patterns:
            for path in self._repo.glob(pattern):
                if path.is_file() and path.name not in _EXCLUDED_FILES:
                    try:
                        test_files.add(str(path.relative_to(self._repo)))
                    except ValueError:
                        # Skip files outside repo root
                        continue

        self._test_files = sorted(test_files)
        logger.info("Discovered %d test files in %s", len(self._test_files), self._repo)
        return self._test_files

    def map_changed_files(self, changed_files: list[str]) -> list[TestMapping]:
        """Map changed source files to candidate test files.

        Uses multiple strategies:
        1. Naming convention (src/auth.py -> tests/test_auth.py)
        2. Directory structure mapping (src/module/foo.py -> tests/module/test_foo.py)
        3. Config file detection (pyproject.toml, setup.cfg, etc. -> broad test impact)

        Args:
            changed_files: List of changed file paths from the PR.

        Returns:
            List of TestMapping objects.
        """
        test_files = self.discover_test_files()
        mappings: list[TestMapping] = []

        for changed_file in changed_files:
            path = Path(changed_file)

            # 1. Config files affect all tests (regardless of extension)
            if path.name in _CONFIG_FILES:
                mappings.append(
                    TestMapping(
                        source_file=changed_file,
                        candidate_tests=test_files,
                        mapping_reason="Config file change affects all tests",
                    )
                )
                continue

            # 2. Test files map to themselves (regardless of extension)
            if _is_test_file(path):
                mappings.append(
                    TestMapping(
                        source_file=changed_file,
                        candidate_tests=[changed_file]
                        if changed_file in test_files
                        else [],
                        mapping_reason="Changed file is itself a test",
                    )
                )
                continue

            # 3. Non-source files (README.md, images, etc.) — no mapping
            if path.suffix not in _SOURCE_EXTENSIONS:
                mappings.append(
                    TestMapping(
                        source_file=changed_file,
                        candidate_tests=[],
                        mapping_reason="Non-source file; AI will determine relevant tests from diff",
                    )
                )
                continue

            # 4. Source files — find candidate tests
            candidates = self._find_candidates(path, test_files)
            reason = "Naming convention and directory structure mapping"
            if not candidates:
                reason = "No direct mapping found; AI will determine relevant tests from diff"

            mappings.append(
                TestMapping(
                    source_file=changed_file,
                    candidate_tests=candidates,
                    mapping_reason=reason,
                )
            )

        logger.info(
            "Mapped %d changed files to test candidates (%d with direct matches)",
            len(mappings),
            sum(1 for m in mappings if m.candidate_tests),
        )
        return mappings

    def _find_candidates(self, source_path: Path, test_files: list[str]) -> list[str]:
        """Find candidate test files for a source file using multiple strategies.

        Args:
            source_path: Path to the changed source file.
            test_files: List of all discovered test file paths.

        Returns:
            List of matching test file paths.
        """
        candidates: set[str] = set()
        stem = source_path.stem  # e.g., "auth" from "auth.py"

        for test_file in test_files:
            test_path = Path(test_file)
            test_stem = test_path.stem  # e.g., "test_auth" from "test_auth.py"

            # Strategy 1: Direct naming convention
            # auth.py -> test_auth.py or auth_test.py
            if test_stem in {f"test_{stem}", f"{stem}_test"}:
                candidates.add(test_file)
                continue

            # Strategy 2: Module name appears in test file name as a word boundary
            # e.g., github_client.py -> test_github_client.py
            # but auth.py should NOT match test_oauth.py
            if f"_{stem}" in test_stem or test_stem.startswith(stem):
                candidates.add(test_file)
                continue

            # Strategy 3: Directory structure mapping
            # src/pr_test_oracle/auth.py -> tests/test_auth.py
            # src/module/sub/foo.py -> tests/sub/test_foo.py
            source_parts = source_path.parts
            test_parts = test_path.parts

            # Check if the test is in a parallel directory structure
            # Strip common prefixes like "src", package name
            source_module_parts = _strip_source_prefix(source_parts)
            test_module_parts = _strip_test_prefix(test_parts)

            if (
                source_module_parts
                and test_module_parts
                and source_module_parts[:-1] == test_module_parts[:-1]
                and test_module_parts[-1] == f"test_{source_module_parts[-1]}"
            ):
                candidates.add(test_file)

        return sorted(candidates)

    def get_test_file_contents(self, test_files: list[str]) -> dict[str, str]:
        """Read contents of test files for AI context.

        Args:
            test_files: List of test file paths relative to repo root.

        Returns:
            Dict mapping file path to file contents. Files that can't be read are skipped.
        """
        contents: dict[str, str] = {}
        for test_file in test_files:
            full_path = self._repo / test_file
            if full_path.is_file():
                try:
                    contents[test_file] = full_path.read_text(encoding="utf-8")
                except OSError:
                    logger.warning("Failed to read test file: %s", test_file)
        return contents


def _is_test_file(path: Path) -> bool:
    """Check if a file path looks like a test file."""
    name = path.name
    stem = path.stem
    return (
        # Python
        name.startswith("test_")
        or name.endswith("_test.py")
        # JS/TS
        or name.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx"))
        or name.endswith((".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx"))
        # Go
        or name.endswith("_test.go")
        # Ruby
        or name.endswith("_spec.rb")
        # Java/C#
        or stem.endswith("Test")
        or stem.endswith("Tests")
    )


def _strip_source_prefix(parts: tuple[str, ...]) -> list[str]:
    """Strip common source prefixes (src, package name) from path parts."""
    parts_list = list(parts)
    # Remove 'src' prefix and package name
    if parts_list and parts_list[0] == "src":
        parts_list = parts_list[1:]
        # Remove package name (first directory after src)
        if len(parts_list) > 1:
            parts_list = parts_list[1:]
    # Remove .py extension from last element
    if parts_list and parts_list[-1].endswith(".py"):
        parts_list[-1] = parts_list[-1][:-3]
    return parts_list


def _strip_test_prefix(parts: tuple[str, ...]) -> list[str]:
    """Strip common test prefixes (tests, test) from path parts."""
    parts_list = list(parts)
    if parts_list and parts_list[0] in ("tests", "test"):
        parts_list = parts_list[1:]
    # Remove .py extension from last element
    if parts_list and parts_list[-1].endswith(".py"):
        parts_list[-1] = parts_list[-1][:-3]
    return parts_list
