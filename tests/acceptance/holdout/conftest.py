"""Register the flat holdout helper module by file path for reliable collection."""

import importlib.util
import sys
from pathlib import Path

_helper_path = Path(__file__).with_name("acceptance_helpers.py")
_spec = importlib.util.spec_from_file_location("acceptance_helpers", _helper_path)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

checkout = _module.checkout
demo = _module.demo
server_factory = _module.server_factory
