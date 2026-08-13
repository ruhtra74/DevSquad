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
import threading
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
        capture = spec.capture and not spec.interactive
        captured: list[str] = []

        def _pump():
            """Relit stdout ligne à ligne : écrit le log en direct ET accumule la sortie."""
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    log_file.write(line)
                    log_file.flush()
                    captured.append(line)
            except (ValueError, OSError):
                pass

        try:
            if spec.interactive:
                proc = subprocess.Popen(cmd, cwd=spec.cwd, env=env)
                pump = None
            elif capture:
                proc = subprocess.Popen(
                    cmd, cwd=spec.cwd, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                pump = threading.Thread(target=_pump, daemon=True)
                pump.start()
            else:
                proc = subprocess.Popen(
                    cmd, cwd=spec.cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT
                )
                pump = None
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
                if pump is not None:
                    pump.join(timeout=5)
                return RunResult(
                    exit_code=-1,
                    success=False,
                    error=f"timeout après {spec.timeout_seconds}s",
                    log_path=log_path,
                    output="".join(captured),
                )
            if pump is not None:
                pump.join(timeout=5)
            return RunResult(
                exit_code=proc.returncode,
                success=proc.returncode == 0,
                log_path=log_path,
                output="".join(captured),
            )
        finally:
            log_file.close()
