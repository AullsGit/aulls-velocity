import os
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYN = os.path.join(ROOT, "bench", "data", "synthetic")


def pytest_collection_modifyitems(config, items):
    if not os.path.isdir(SYN):
        skip = pytest.mark.skip(reason="material em bench/data ausente")
        for it in items:
            if "bench" in it.keywords:
                it.add_marker(skip)


os.environ.setdefault("VELOCITY_NO_TEXT", "1")  # OCR só quando pedido (carrega modelos); os testes não precisam
