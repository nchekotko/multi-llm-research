import json
from pathlib import Path

import pytest

from application.contracts import TaskSpec
from application.demo import demo_config
from application.engine import Engine
from application.storage import Store


@pytest.fixture
def task():
    data = json.loads((Path(__file__).parent.parent / "examples/task.json").read_text(encoding="utf-8"))
    return TaskSpec.model_validate(data)


@pytest.fixture
def engine(tmp_path):
    return Engine(Store(tmp_path / "state"), demo_config())
