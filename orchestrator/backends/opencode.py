"""Adaptateur OpenCode : pilote `opencode run` en conservant l'interactivité.

- Mode interactif : `opencode run --interactive` attaché au terminal de
  l'utilisateur — les agents peuvent poser des questions via l'outil
  `question` et l'utilisateur répond en direct. La sortie n'est pas capturée,
  c'est le contrat documentaire (fichiers .md) qui fait foi.
- Mode non-interactif (parallélisme futur) : `opencode run --format json`
  avec sortie capturée dans un log.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import Backend, RunResult, RunSpec


class OpenCodeBackend(Backend):
    name = "opencode"

    def __init__(self, binary: str = "opencode"):
        self.binary = binary

    def run(self, spec: RunSpec, log_path: str) -> RunResult:
        Path(spec.cwd).mkdir(parents=True, exist_ok=True)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [self.binary, "run"]
        if spec.interactive:
            cmd.append("--interactive")
        else:
            cmd += ["--format", "json"]
        if spec.auto_approve:
            cmd.append("--auto")
        cmd.append(spec.prompt)

        log_file = open(log_path, "w")
        env = {**os.environ, "PWD": spec.cwd}
        try:
            if spec.interactive:
                proc = subprocess.Popen(cmd, cwd=spec.cwd, env=env)
            else:
                proc = subprocess.Popen(
                    cmd, cwd=spec.cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT
                )
            try:
                if spec.timeout_seconds:
                    proc.wait(timeout=spec.timeout_seconds)
                else:
                    proc.wait()
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return RunResult(
                    exit_code=-1,
                    success=False,
                    error=f"timeout après {spec.timeout_seconds}s",
                    log_path=log_path,
                )
            return RunResult(
                exit_code=proc.returncode,
                success=proc.returncode == 0,
                log_path=log_path,
            )
        finally:
            log_file.close()
