"""Explicitly run the integrated holdout and require all ten scenarios to execute."""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = Path(r"C:/scratch/2026-10-23_search-agent-issues/holdout.xml")


def main() -> int:
    env = os.environ.copy()
    env["AINEWS_RUN_ACCEPTANCE"] = "1"
    env.setdefault("AINEWS_TEST_ROOT", os.getcwd())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(HERE),
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={REPORT}",
        ],
        env=env,
        check=False,
    )
    if result.returncode:
        return result.returncode
    suite = ET.parse(REPORT).getroot()
    cases = list(suite.iter("testcase"))
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    failures = sum(
        1 for case in cases if case.find("failure") is not None or case.find("error") is not None
    )
    if len(cases) != 10 or skipped or failures:
        print(
            f"Holdout must execute 10 scenarios with zero skips; got {len(cases)} cases, "
            f"{skipped} skipped, {failures} failed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
