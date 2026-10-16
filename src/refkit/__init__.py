"""RefKit: reference models of hardware blocks, written like Verilog in Python, run cycle by cycle, translated to Verilog and
proven against RTL with SymbiYosys.

    from refkit import Module, Reg, seq, comb       # the language (core.py)
    from refkit.verilog import translate, prove     # Verilog translation and the formal proof (verilog.py)
    from refkit import guide_text                   # the guide a model is taught from (guide.md)
"""

from importlib import resources as _resources

from .core import *  # noqa: F401,F403
from .core import __all__ as _core_all
from .core import _meta_of, _resolve  # noqa: F401  (used by refkit.verilog and by tools that run completed references)


def guide_text() -> str:
    """The whole guide (core and cookbook), as a model is shown it."""
    return (_resources.files(__package__) / "guide.md").read_text(encoding="utf-8")


def guide_path() -> str:
    """Where guide.md is on disk."""
    return str(_resources.files(__package__) / "guide.md")


__all__ = [*_core_all, "guide_text", "guide_path"]
