"""Summarise a JUnit report written by ``pytest --junitxml`` for a CI run.

Prints how many tests passed, failed and were skipped. On GitHub Actions the same line becomes a
notice on the run, and each failure becomes an error annotation with the end of its traceback, so
the outcome can be read without opening the log.

``--require-all-ran`` makes the report itself fail when no test ran or any was skipped. A job that
exists to test against a service must not pass because the service was not reachable and every
test skipped itself.

    python tools/ci/junit_summary.py report.xml --title "Neo4j" --require-all-ran
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

#: GitHub shows at most ten annotations of one level for a step.
_MAX_FAILURES_SHOWN = 9
_TRACEBACK_LINES = 25
_TRACEBACK_CHARS = 3000


@dataclass
class Report:
    passed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    #: (test id, short message, traceback)
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + len(self.failures) + sum(self.skipped.values())

    def line(self) -> str:
        parts = [f"{self.passed} passed"]
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        parts.append(f"{sum(self.skipped.values())} skipped")
        return ", ".join(parts)


def read(path: str) -> Report:
    report = Report()
    # The file was written by pytest a moment ago in the same job; it is not untrusted input.
    for case in ET.parse(path).getroot().iter("testcase"):  # noqa: S314
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        skipped = case.find("skipped")
        if problem is not None:
            report.failures.append((test_id, problem.get("message", ""), problem.text or ""))
        elif skipped is not None:
            reason = skipped.get("message", "") or "no reason given"
            report.skipped[reason] = report.skipped.get(reason, 0) + 1
        else:
            report.passed += 1
    return report


def _escape(text: str, *, in_property: bool = False) -> str:
    """Encode text for a workflow command, as the Actions runner expects."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if in_property:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def annotate(level: str, title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={_escape(title, in_property=True)}::{_escape(message)}")


def _tail(traceback: str) -> str:
    lines = traceback.strip().splitlines()[-_TRACEBACK_LINES:]
    return "\n".join(lines)[-_TRACEBACK_CHARS:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("report", help="JUnit XML file written by pytest --junitxml")
    parser.add_argument("--title", default="Tests", help="name of this run in the annotations")
    parser.add_argument(
        "--require-all-ran",
        action="store_true",
        help="fail when no test ran or any test was skipped",
    )
    arguments = parser.parse_args(argv)

    try:
        report = read(arguments.report)
    except (OSError, ET.ParseError) as error:
        print(f"{arguments.title}: no usable report at {arguments.report}: {error}")
        annotate("error", arguments.title, f"no test report was written ({error})")
        return 1

    print(f"{arguments.title}: {report.line()}")
    for reason, count in sorted(report.skipped.items()):
        print(f"  skipped {count}: {reason}")
    for test_id, message, _ in report.failures:
        print(f"  FAILED {test_id}: {message.splitlines()[0] if message else ''}")
    annotate("notice", arguments.title, report.line())

    for test_id, message, traceback in report.failures[:_MAX_FAILURES_SHOWN]:
        annotate("error", f"{arguments.title}: {test_id}", f"{message}\n\n{_tail(traceback)}")
    hidden = report.failures[_MAX_FAILURES_SHOWN:]
    if hidden:
        names = "\n".join(test_id for test_id, _, _ in hidden)
        annotate("error", f"{arguments.title}: {len(hidden)} more failures", names)

    if arguments.require_all_ran:
        skipped = sum(report.skipped.values())
        if report.total == 0 or skipped:
            reasons = "; ".join(f"{count} x {reason}" for reason, count in sorted(report.skipped.items()))
            problem = (
                "no test was collected"
                if report.total == 0
                else f"{skipped} of {report.total} tests were skipped ({reasons})"
            )
            print(f"{arguments.title}: every test has to run here, but {problem}")
            annotate("error", arguments.title, f"every test has to run in this job, but {problem}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
