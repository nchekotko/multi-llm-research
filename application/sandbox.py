from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .security import PolicyDenied, ensure_no_secrets


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    status: str


class DockerSandbox:
    """One offline Linux container per invocation. Never falls back to host execution."""

    def __init__(self, image: str, enabled: bool = False):
        self.image, self.enabled = image, enabled

    def execute(
        self, code: str, input_data: object, *, allowed: bool, timeout: int = 10, cancelled=lambda: False
    ) -> SandboxResult:
        if not self.enabled or not allowed:
            raise PolicyDenied("python_sandbox is disabled or outside the granted tool policy")
        if not 1 <= timeout <= 60 or len(code.encode()) > 100000:
            raise PolicyDenied("sandbox code size or timeout exceeds limits")
        ensure_no_secrets({"code": code, "input": input_data})
        docker = shutil.which("docker")
        if docker is None:
            raise SandboxUnavailable("Docker executable not found")
        # Exact local image digest is resolved first; Docker cannot pull a task-controlled image.
        try:
            inspected = subprocess.run(
                [docker, "image", "inspect", "--format", "{{.Id}}", self.image],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise SandboxUnavailable("Linux Docker daemon or prebuilt sandbox image is unavailable") from exc
        image_id = inspected.stdout.strip()
        if not image_id.startswith("sha256:"):
            raise SandboxUnavailable("sandbox image digest could not be resolved")
        from .contracts import new_id

        name = "multi-llm-" + new_id()
        command = [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=32",
            "--memory=128m",
            "--memory-swap=128m",
            "--cpus=0.5",
            "--ulimit",
            "fsize=1048576:1048576",
            "--ulimit",
            "nofile=64:64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,uid=65532,gid=65532",
            "-i",
            image_id,
        ]
        started = time.monotonic()
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        payload = json.dumps({"code": code, "input": input_data}, ensure_ascii=False).encode()
        if len(payload) > 200000:
            process.kill()
            process.communicate(timeout=5)
            subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=5, check=False)
            raise PolicyDenied("sandbox input exceeds 200000 bytes")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = threading.Event()

        def drain(stream, channel):
            with stream:
                while chunk := stream.read(4096):
                    buffer = buffers[channel]
                    buffer.extend(chunk[: max(0, 65536 - len(buffer))])
                    if len(buffer) >= 65536:
                        overflow.set()
                        return

        readers = [
            threading.Thread(target=drain, args=(stream, channel), daemon=True)
            for stream, channel in [(process.stdout, "stdout"), (process.stderr, "stderr")]
        ]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write(payload)
            process.stdin.close()
            while process.poll() is None:
                is_cancelled = cancelled()
                if is_cancelled or overflow.is_set() or time.monotonic() - started > timeout:
                    status = (
                        "cancelled"
                        if is_cancelled
                        else ("output_exceeded" if overflow.is_set() else "timed_out")
                    )
                    subprocess.run([docker, "kill", name], capture_output=True, timeout=5, check=False)
                    process.kill()
                    process.wait(timeout=5)
                    return SandboxResult(-1, "", "", status)
                time.sleep(0.02)
            for reader in readers:
                reader.join(timeout=2)
            if overflow.is_set():
                return SandboxResult(-1, "", "", "output_exceeded")
            stdout, stderr = [buffers[c].decode("utf-8", errors="replace") for c in ("stdout", "stderr")]
            ensure_no_secrets({"stdout": stdout, "stderr": stderr})
            return SandboxResult(
                process.returncode,
                stdout[:65536],
                stderr[:65536],
                "completed" if process.returncode == 0 else "failed",
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=5, check=False)
            for reader in readers:
                reader.join(timeout=2)


def read_code(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")
