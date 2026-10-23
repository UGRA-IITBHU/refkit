"""Completed references of several blocks run together, wired as a plan wires the blocks: the reference of the whole design as
its parts describe it. Simulated against the reference of the whole module (written from the description of the whole), it
shows whether a split into blocks is right before any RTL of the blocks exists: a block that registers a value the whole needs
in the same cycle, or two blocks that each add a register, show as a difference here.

    from refkit.compose import run_composed
    outs = run_composed({"a": source_a, "b": source_b},
                        [("top.x", "a.x"), ("a.y", "b.y_in"), ("b.z[3:0]", "top.z")], cycles, outputs={"z": 4})

An endpoint is `top.<port>` or `<part>.<port>`, optionally with a bit `[i]` or a range `[hi:lo]`. Each cycle the parts' outputs
are settled first (a part's outputs may depend on its inputs in the same cycle, so the parts are evaluated again until no output
changes: a combinational loop through the parts that never settles is an error), then every part takes the clock edge with the
inputs it settled on. Each part keeps its own reset (port, polarity, style) from its own `__refkit__`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .core import RefKitError, ResetSpec, _meta_of, _resolve, _step, mask

__all__ = ["run_composed", "Endpoint"]

_ENDPOINT = re.compile(r"^\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*(?:\[\s*(\d+)\s*(?::\s*(\d+)\s*)?\])?\s*$")


class Endpoint:
    """`owner.port[hi:lo]`: owner is `top` or a part's name; hi/lo are None for the whole port."""

    __slots__ = ("owner", "port", "hi", "lo")

    def __init__(self, text: str) -> None:
        m = _ENDPOINT.match(text)
        if m is None:
            raise RefKitError(f"cannot read the endpoint {text!r} (expected owner.port, owner.port[i] or owner.port[hi:lo])")
        self.owner, self.port = m.group(1), m.group(2)
        hi = None if m.group(3) is None else int(m.group(3))
        lo = hi if m.group(4) is None else int(m.group(4))
        if hi is not None and lo is not None and hi < lo:
            hi, lo = lo, hi
        self.hi, self.lo = hi, lo

    def take(self, value: int) -> int:
        """The selected bits of a value of the whole port."""
        return value if self.hi is None else (value >> self.lo) & ((1 << (self.hi - self.lo + 1)) - 1)

    def __repr__(self) -> str:
        return f"{self.owner}.{self.port}" + ("" if self.hi is None else f"[{self.hi}:{self.lo}]")


class _Part:
    def __init__(self, name: str, source: str) -> None:
        meta = _meta_of(source)
        if meta is None:
            raise RefKitError(f"part {name}: not a completed RefKit reference (no __refkit__)")
        if meta.get("clock") is not None:
            raise RefKitError(f"part {name}: a reference run step by step (its clock an input) cannot be composed")
        self.name = name
        self.machine = _resolve(meta["module"])
        self.inputs: dict[str, int] = dict(meta.get("inputs") or {})
        self.widths: dict[str, int] = dict(meta.get("widths") or {})
        self.machine._in_widths = dict(self.inputs)  # type: ignore[attr-defined]
        self.reset = ResetSpec(meta["reset"], int(meta["active"]), meta["style"]) if meta.get("reset") else None
        if self.reset is not None and self.reset.port not in self.inputs:
            self.inputs[self.reset.port] = 1
        self.state: dict[str, Any] = self.machine.reset_values()


def run_composed(
    parts: Mapping[str, str], connections: Sequence[tuple[str, str]], cycles: Sequence[Mapping[str, int]], *,
    outputs: Mapping[str, int],
) -> list[dict[str, int | None]]:
    """Run the parts (name -> completed reference source) wired by `connections` ((driver, sink) endpoints) over `cycles`
    (the top's inputs per cycle); returns the top's outputs (`outputs`: name -> width) per cycle. An input of a part that
    nothing drives reads 0; an output of the top that a part leaves undefined (None) in a cycle is None when one driver gives
    it whole."""
    ps = {name: _Part(name, src) for name, src in parts.items()}
    sinks: dict[tuple[str, str], list[tuple[Endpoint, Endpoint]]] = {}
    for driver, sink in connections:
        d, s = Endpoint(driver), Endpoint(sink)
        for e in (d, s):
            if e.owner != "top" and e.owner not in ps:
                raise RefKitError(f"connection {driver} -> {sink}: no part named {e.owner}")
        sinks.setdefault((s.owner, s.port), []).append((d, s))

    def gather(owner: str, port: str, top_in: Mapping[str, int], outs: Mapping[str, Mapping[str, int | None]], width: int) -> int | None:
        value, undefined = 0, False
        for d, s in sinks.get((owner, port), []):
            raw = top_in.get(d.port, 0) if d.owner == "top" else outs[d.owner].get(d.port)
            if raw is None:
                undefined = True
                raw = 0
            got = d.take(int(raw))
            if s.hi is None:
                value = got
            else:
                n = s.hi - s.lo + 1
                value = (value & ~(((1 << n) - 1) << s.lo)) | ((got & ((1 << n) - 1)) << s.lo)
        if undefined and len(sinks.get((owner, port), [])) == 1:
            return None
        return mask(value, width)

    outs: dict[str, dict[str, int | None]] = {name: {o: 0 for o in p.widths} for name, p in ps.items()}
    result: list[dict[str, int | None]] = []
    for number, x in enumerate(cycles):
        nxt: dict[str, dict[str, Any]] = {}
        for _ in range(len(ps) + 2):
            changed = False
            for name, p in ps.items():
                xi = {}
                for port, w in p.inputs.items():
                    v = gather(name, port, x, outs, w)
                    xi[port] = 0 if v is None else v
                nxt[name], o = _step(p.machine, p.state, xi, p.reset, p.widths)
                if o != outs[name]:
                    outs[name], changed = dict(o), True
            if not changed:
                break
        else:
            raise RefKitError(f"cycle {number}: the outputs of the parts never settle (a combinational loop through the parts)")
        result.append({port: gather("top", port, x, outs, w) for port, w in outputs.items()})
        for name, p in ps.items():
            p.state = nxt[name]
    return result
