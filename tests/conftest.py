"""Put src/ on the path for direct `pytest tests/` runs.

The documented invocation is `PYTHONPATH=src pytest`, but the verify
harness runs bare `pytest tests/`; without this conftest every module
import fails on `No module named 'cupola'` and the gate reads red for
an environment reason instead of a code reason.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))
