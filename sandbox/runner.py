"""Runs inside the disposable container; never import this module on the host."""
import contextlib
import json
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class CappedStream:
    def __init__(self, stream):
        self.stream, self.used = stream, 0

    def write(self, value):
        size = len(value.encode("utf-8"))
        if self.used+size > 32768:
            raise RuntimeError("output limit exceeded")
        self.used += size
        return self.stream.write(value)

    def flush(self):
        self.stream.flush()


payload = json.loads(sys.stdin.buffer.read(200000))
namespace = {"__name__": "__main__", "task_input": payload["input"]}
with contextlib.redirect_stdout(CappedStream(sys.stdout)), contextlib.redirect_stderr(CappedStream(sys.stderr)):
    exec(compile(payload["code"], "<task>", "exec"), namespace)
