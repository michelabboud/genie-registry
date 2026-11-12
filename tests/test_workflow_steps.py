"""Exercises the pull-request job's shell steps from the real workflow file
against a local bare repository and a stub `gh`: branch creation, no-op
reruns, the force-with-lease race, foreign commits, skipped-vendor carry
forward, the empty-commit guard and an ls-remote failure. Needs bash and git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "model-watch.yml"
SIM = Path(__file__).resolve().parent / "workflow_steps_sim.sh"


def step_script(name: str) -> str:
    """The `run: |` body of the step called `name`, dedented."""
    lines = WORKFLOW.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


class WorkflowSteps(unittest.TestCase):
    def test_pull_request_job_shell_steps(self):
        if shutil.which("bash") is None or shutil.which("git") is None:
            self.fail("bash and git are required for this test")
        with tempfile.TemporaryDirectory(prefix="model-watch-sim-") as tmp:
            steps = Path(tmp) / "steps"
            work = Path(tmp) / "work"
            steps.mkdir()
            work.mkdir()
            (steps / "branch.sh").write_text(step_script("Find the watch branch and its pull request"))
            (steps / "open.sh").write_text(step_script("Open or update the pull request"))
            env = {
                **os.environ,
                "SIM_REPO": str(REPO),
                "SIM_STEPS": str(steps),
                "SIM_WORK": str(work),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            result = subprocess.run(
                ["bash", str(SIM)], env=env, capture_output=True, text=True, timeout=120
            )
        report = [line for line in result.stdout.splitlines() if line.startswith(("PASS", "FAIL", "failures="))]
        self.assertEqual(result.returncode, 0, "\n".join(report) + "\n" + result.stderr[-2000:])
        self.assertEqual(sum(line.startswith("PASS") for line in report), 7, "\n".join(report))


if __name__ == "__main__":
    unittest.main()
