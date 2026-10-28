"""RefKit: a small framework for writing the reference model of a CLOCKED block.

A reference written as one free-form Python function over a list of cycles gets the *timing* of the hardware wrong as often as
the behaviour: an output one cycle early or late, a reset that acts at the wrong moment, a counter that does not wrap, a state
machine that is Mealy when the description says Moore. RefKit takes the timing out of the model's hands. The model describes the
hardware as a **machine**: the registers it holds (with widths and reset values), a pure function that gives their next values,
and a pure function that gives the outputs. RefKit runs it with the clocking and reset semantics the harness expects, so the
only thing the model can get wrong is what the description actually says.

    from refkit import Machine, Reg, State

    class Ref(Machine):
        regs = {"count": Reg(4)}                     # 4 bits, wraps by itself, reset value 0
        def next_state(self, s, x):                  # s: registers now, x: inputs this cycle
            return {"count": s.count + 1} if x.en else {}
        def outputs(self, s, x):                     # what the output ports read in this cycle
            return {"q": s.count}

Because the machine is data (registers, a transition function, an output function) two readings of one description can be compared
exactly, by exploring every reachable state, instead of by a stimulus that may not reach the difference (`compare`).

The harness calls the reference as ``ref_<name>(cycles)``; `run` is what that function calls (the backend writes the call, with the
reset port and the output widths of the block).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "Machine", "FSM", "Reg", "State", "run", "compare", "RefKitError", "ExampleMismatch",
    "mask", "bit", "bits", "concat", "ones", "popcount", "rotl", "rotr", "signed", "rising", "falling",
]


class RefKitError(Exception):
    """The reference is malformed (an undeclared register, a missing output, a bad state name...). Reported, never ignored."""


class ExampleMismatch(RefKitError):
    """The machine does not reproduce an example the description gives cycle by cycle."""


# ─── small helpers the descriptions need ────────────────────────────────────────────────────────────────────────────────

def mask(value: int, width: int) -> int:
    """`value` as an unsigned number of `width` bits (wraps, and a negative number becomes its two's complement)."""
    return int(value) & ((1 << width) - 1)


def ones(width: int) -> int:
    return (1 << width) - 1


def bit(value: int, index: int) -> int:
    """Bit `index` of `value`; bit 0 is the least significant."""
    return (int(value) >> index) & 1


def bits(value: int, high: int, low: int) -> int:
    """`value[high:low]`, both ends included, as Verilog writes it."""
    return (int(value) >> low) & ((1 << (high - low + 1)) - 1)


def concat(*parts: tuple[int, int]) -> int:
    """Verilog's `{a, b}`: `concat((a, wa), (b, wb))`, the first part ends up in the most significant bits."""
    out = 0
    for value, width in parts:
        out = (out << width) | mask(value, width)
    return out


def popcount(value: int) -> int:
    return bin(int(value)).count("1")


def rotl(value: int, amount: int, width: int) -> int:
    amount %= width
    value = mask(value, width)
    return mask((value << amount) | (value >> (width - amount)), width)


def rotr(value: int, amount: int, width: int) -> int:
    return rotl(value, width - (amount % width), width)


def signed(value: int, width: int) -> int:
    """The two's-complement meaning of a `width`-bit pattern (a negative number when its top bit is set)."""
    value = mask(value, width)
    return value - (1 << width) if value >> (width - 1) else value


def rising(previous: int, current: int) -> int:
    """1 when a one-bit signal went from 0 to 1."""
    return int(not previous and bool(current))


def falling(previous: int, current: int) -> int:
    return int(bool(previous) and not current)


# ─── registers ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Reg:
    """A register of `width` bits. Its next value is wrapped to the width by RefKit.

    `width` may be left out: `Reg()` is a register with the width of the output port of the same name when there is one (an
    `output reg`), and otherwise 32 bits, a number that does not wrap in practice (a count with no width in the description).
    `reset` is its value after reset; `reset=None` is a register the reset does not touch (Verilog: assigned in an always block
    that does not test the reset). `init` is its value at power-up, before any reset (Verilog `reg [3:0] r = 4'd5;` or an
    `initial` block); it defaults to the reset value, or 0 for a register with no reset."""

    width: int | None = None
    reset: int | None = 0
    init: int | None = None

    @property
    def power_up(self) -> int:
        return self.init if self.init is not None else (self.reset if self.reset is not None else 0)

    def __post_init__(self) -> None:
        if self.width is not None and self.width < 1:
            raise RefKitError(f"Reg width must be at least 1, got {self.width}")


#: The width of a `Reg()` declared without one that is not an output register: Verilog's `integer`.
NUMBER_WIDTH = 32


def _reg_width(owner: Any, name: str, decl: Reg) -> int:
    """A register's width: the declared one; for `Reg()`, the width of the output port of that name, else NUMBER_WIDTH.

    `owner` is the machine (an instance, whose `_out_widths` the run sets) or its class (no port widths known: NUMBER_WIDTH)."""
    if decl.width is not None:
        return decl.width
    return (getattr(owner, "_out_widths", None) or {}).get(name, NUMBER_WIDTH)


class _class_or_instance:
    """A method that receives the instance when called on one and the class otherwise (`reset_values` needs the port widths a
    run gives the instance, and is also called on a child module's class)."""

    def __init__(self, function: Callable[..., Any]) -> None:
        self.function = function

    def __get__(self, obj: Any, cls: type) -> Callable[..., Any]:
        owner = obj if obj is not None else cls
        return lambda *args, **kwargs: self.function(owner, *args, **kwargs)


@dataclass(frozen=True)
class State:
    """A register that holds one of a list of named states (a state machine's state). `reset` defaults to the first name."""

    names: tuple[str, ...]
    reset: str | None = None

    def __init__(self, *names: str, reset: str | None = None) -> None:
        if not names:
            raise RefKitError("State needs at least one name")
        object.__setattr__(self, "names", tuple(names))
        object.__setattr__(self, "reset", names[0] if reset is None else reset)
        if self.reset not in self.names:
            raise RefKitError(f"the reset state {self.reset!r} is not one of {list(self.names)}")


class _View(Mapping[str, Any]):
    """Read-only view of a dict, with attribute access. A name that is a Python keyword (`in`) is `x["in"]` or `x.in_`."""

    __slots__ = ("_d",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_d", dict(data))

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __getattr__(self, name: str) -> Any:
        d = object.__getattribute__(self, "_d")
        if name in d:
            return d[name]
        if name.endswith("_") and name[:-1] in d:
            return d[name[:-1]]
        raise AttributeError(f"no input or register called {name!r}; have: {sorted(d)}")

    def __setattr__(self, name: str, value: Any) -> None:
        raise RefKitError("s and x are read-only: return the new register values from next_state instead")

    def __repr__(self) -> str:
        return f"{self._d}"


# ─── the machine ────────────────────────────────────────────────────────────────────────────────────────────────────────

class Machine:
    """Subclass it, declare `regs`, write `next_state` and `outputs`.

    ``regs``       every register of the hardware: ``{"name": Reg(width, reset=0)}`` or ``{"state": State("A", "B")}``.
    ``next_state`` ``(self, s, x) -> dict``: the new value of each register that CHANGES at this clock edge (leave out the ones
                   that hold). `s` is the registers as they are now, `x` the inputs of this cycle.
    ``outputs``    ``(self, s, x) -> dict``: the value of every output port during this cycle, from the registers `s` and, for a
                   Mealy output, the inputs `x`. ``None`` for an output means the description says its value does not matter then.
    ``moore``      optional: True when no output depends on the inputs (RefKit then checks that), False when some does.
    ``examples``   optional: cycle-by-cycle values the DESCRIPTION itself gives, as a list of ``(inputs, outputs)`` pairs of
                   per-cycle dicts; RefKit refuses a machine that does not reproduce them.
    """

    regs: dict[str, Reg | State] = {}
    moore: bool | None = None
    examples: Sequence[tuple[Sequence[Mapping[str, int]], Sequence[Mapping[str, int | None]]]] = ()

    def next_state(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, Any]:
        return {}

    def outputs(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, int | None]:
        raise NotImplementedError("a Machine must define outputs(self, s, x)")

    # -- used by RefKit (private names, so that a user's block may be called `outputs` or `next_state`) -----------------

    def _engine_outputs(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, int | None]:
        return self.outputs(s, x)

    def _engine_next(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, Any]:
        return self.next_state(s, x)

    @_class_or_instance
    def reset_values(cls) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, decl in cls.regs.items():
            if isinstance(decl, Reg):
                out[name] = mask(decl.power_up, _reg_width(cls, name, decl))
            elif isinstance(decl, State):
                out[name] = decl.reset
            else:
                raise RefKitError(f"regs[{name!r}] must be Reg(...) or State(...), got {decl!r}")
        return out

    def _apply(self, s: dict[str, Any], change: Mapping[str, Any] | None) -> dict[str, Any]:
        if change is None:
            return dict(s)
        if not isinstance(change, Mapping):
            raise RefKitError(f"next_state must return a dict, got {type(change).__name__}")
        new = dict(s)
        for name, value in change.items():
            decl = self.regs.get(name)
            if decl is None:
                raise RefKitError(f"next_state set {name!r}, which is not declared in regs (declared: {sorted(self.regs)})")
            if isinstance(decl, Reg):
                new[name] = mask(value, _reg_width(self, name, decl))
            else:
                if value not in decl.names:
                    raise RefKitError(f"next_state set {name!r} to {value!r}, which is not one of {list(decl.names)}")
                new[name] = value
        return new


class FSM(Machine):
    """A state machine given as a table, the way a state diagram is drawn.

    ``states``       the state names, the reset state first (or set ``reset_state``).
    ``transitions``  ``{state: [(condition, next_state), ...]}``: the first condition that holds fires; ``condition`` is a
                     function of the inputs ``lambda x: x.w == 1`` or ``True`` for always; no condition holds: the state stays.
    ``output``       ``{state: {port: value}}`` for outputs that depend only on the state (Moore), or a function
                     ``(state, x) -> {port: value}`` when they also depend on the inputs (Mealy).
    """

    states: Sequence[str] = ()
    reset_state: str | None = None
    transitions: Mapping[str, Sequence[tuple[Any, str]]] = {}
    output: Any = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.states:
            cls.regs = {"state": State(*cls.states, reset=cls.reset_state)}
            if isinstance(cls.output, Mapping):
                cls.moore = True

    def next_state(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, Any]:
        for condition, target in self.transitions.get(s["state"], ()):
            if condition is True or condition is None or condition(x):
                return {"state": target}
        return {}

    def outputs(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, int | None]:
        if callable(self.output):
            return self.output(s["state"], x)
        if s["state"] not in self.output:
            raise RefKitError(f"output has no entry for state {s['state']!r} (have: {sorted(self.output)})")
        return self.output[s["state"]]


# ─── running it ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResetSpec:
    """How the block's reset port acts: `port` is the input, `active` the level that asserts it, `style` is "sync" or "async"."""

    port: str
    active: int = 1
    style: str = "sync"


def _resolve(machine: type[Machine] | Machine) -> Machine:
    return machine() if isinstance(machine, type) else machine


def _after_reset(m: Machine, s: Mapping[str, Any]) -> dict[str, Any]:
    """The registers once the reset has acted: every register with a reset value takes it; one declared `reset=None` keeps
    its value; a memory is cleared only when declared with a reset value; an instance's registers follow the same rules."""
    out = dict(s)
    for name, decl in m.regs.items():
        if isinstance(decl, Reg):
            if decl.reset is not None:
                out[name] = mask(decl.reset, _reg_width(m, name, decl))
        else:
            out[name] = decl.reset
    for name, decl in getattr(m, "_mems", {}).items():
        if decl.reset is not None:
            out[name] = tuple([mask(decl.reset, decl.width)] * decl.depth)
    for name, decl in getattr(m, "_instances", {}).items():
        out[name] = _freeze(_after_reset(decl.module, dict(s[name])))
    return out


def _freeze(state: Mapping[str, Any]) -> tuple:
    return tuple(sorted(state.items()))


def _asserted(spec: ResetSpec | None, x: Mapping[str, int]) -> bool:
    return spec is not None and spec.port in x and int(x[spec.port]) == spec.active


def _step(
    m: Machine, s: dict[str, Any], x: Mapping[str, int], reset: ResetSpec | None, widths: Mapping[str, int] | None,
    seen: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, int | None]]:
    """One clock cycle: the outputs of this cycle (sampled before the edge) and the registers after the edge. `seen`, when given,
    receives the registers as this cycle's blocks read them (an asynchronous reset already acted)."""
    m._out_widths = widths  # type: ignore[attr-defined]  # a Module needs them to give its outputs their widths
    init = m.reset_values()
    if reset is not None and reset.style == "async" and _asserted(reset, x):
        s = _after_reset(m, s)                           # an asynchronous reset shows in this very cycle
    if seen is not None:
        seen.append(dict(s))
    sv, xv = _View(s), _View(x)
    out = m._engine_outputs(sv, xv)
    if not isinstance(out, Mapping):
        raise RefKitError(f"outputs must return a dict, got {type(out).__name__}")
    result: dict[str, int | None] = {}
    for name, value in out.items():
        if value is None:
            result[name] = None
        else:
            value = int(value)
            result[name] = mask(value, widths[name]) if widths is not None and name in widths else value
    if widths is not None:
        missing = sorted(set(widths) - set(result))
        extra = sorted(set(result) - set(widths))
        if missing:
            raise RefKitError(f"outputs did not give {missing}; every output port needs a value in every cycle (None if it does not matter)")
        if extra:
            raise RefKitError(f"outputs gave {extra}, which are not output ports (ports: {sorted(widths)})")
    change = m._engine_next(sv, xv)
    if reset is not None and _asserted(reset, x):
        _check_reset_overrides(m, change, reset)
    nxt = m._apply(s, change)
    if reset is not None and _asserted(reset, x):
        nxt = _after_reset(m, nxt)                       # held in reset (sync: takes effect at this edge); registers without one go on
    return nxt, result


def _caller_line() -> int | None:
    """The line of the model's code that is running: the innermost frame that is not RefKit's own."""
    import sys
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    return frame.f_lineno if frame is not None else None


def _check_reset_overrides(m: Machine, change: Mapping[str, Any] | None, spec: ResetSpec) -> None:
    """A @seq block that reads the reset port and assigns a register, in a cycle in which the reset is asserted, a value other
    than its declared reset value: the reset puts the register back at that edge, so the assignment has no effect. It is the
    ordinary reset handled by hand, disagreeing with the declaration (or with the reset's polarity); never what was meant."""
    if not change or spec.port not in getattr(m, "_seq_reads", ()):
        return
    for name, value in change.items():
        decl = m.regs.get(name)
        if isinstance(decl, Reg) and decl.reset is not None:
            width = _reg_width(m, name, decl)
            lost, reset_value = mask(value, width) != mask(decl.reset, width), mask(decl.reset, width)
        elif isinstance(decl, State):
            lost, reset_value = value != decl.reset, decl.reset
        else:
            continue
        if lost:
            level = "high" if spec.active == 1 else "low"
            line = (getattr(m, "_seq_origins", None) or {}).get(name)
            raise RefKitError(
                f"{name}.next = {value!r}{f' (line {line})' if line else ''} is assigned in a cycle in which the reset {spec.port} is asserted ({spec.port} = "
                f"{spec.active}, active {level}); RefKit's reset puts {name} back to {reset_value!r} at that edge, so this "
                f"assignment has no effect. RefKit applies the reset by itself: do not test {spec.port} in the blocks; give each "
                "register its reset value in its declaration")


def run(
    machine: type[Machine] | Machine, cycles: Sequence[Mapping[str, int]], *, reset: str | None = None, active: int = 1,
    style: str = "sync", widths: Mapping[str, int] | None = None, inputs: Mapping[str, int] | None = None,
    memory: Any = None, _examples: bool = True,
) -> list[dict[str, int | None]]:
    """Run `machine` over `cycles` (one dict of input values per clock cycle) and return one dict of outputs per cycle.

    The outputs of cycle i are read BEFORE the rising edge that ends cycle i; the registers change at that edge, so a register
    written in cycle i shows from cycle i+1. A synchronous reset acts at the edge (cycle i still shows the old registers); an
    asynchronous one acts at once (cycle i already shows the reset values). Registers start at their reset values.
    """
    m = _resolve(machine)
    m._out_widths = widths  # type: ignore[attr-defined]
    if inputs is not None:
        m._in_widths = dict(inputs)  # type: ignore[attr-defined]
    if memory is not None:
        m._memory = memory  # type: ignore[attr-defined]  # contents loaded from a file: self.memory[i]
    spec = ResetSpec(reset, active, style) if reset is not None else None
    if _examples and m.examples:
        _check_examples(m, spec, widths)
    if m.moore is True:
        _check_moore(m, cycles, spec, widths)
    s = m.reset_values()
    out: list[dict[str, int | None]] = []
    for x in cycles:
        s, o = _step(m, s, x, spec, widths)
        out.append(o)
    return out


def _check_examples(m: Machine, spec: ResetSpec | None, widths: Mapping[str, int] | None) -> None:
    """Each example reproduced. An example may also give an inner register's (or a State's) value in a cycle, as the register holds
    it during that cycle; a Module's mismatch names the line of the module that gave the value."""
    kwargs: dict[str, Any] = dict(inputs=getattr(m, "_in_widths", None), reset=spec.port if spec else None,
                                  active=spec.active if spec else 1, style=spec.style if spec else "sync", widths=widths)
    for number, (inputs, expected) in enumerate(m.examples, start=1):
        names = {n for want in expected for n, v in want.items() if v is not None}
        inner = {n for n in names if n in m.regs and (widths is None or n not in widths)}
        unknown = sorted(n for n in names - inner if widths is not None and n not in widths)
        if unknown:
            raise RefKitError(f"example {number} gives {unknown}, which are neither outputs nor registers of the module "
                              f"(outputs: {sorted(widths or ())}; registers: {sorted(m.regs)})")
        followed = None
        if isinstance(m, Module) and not m.needs_steps:
            followed = trace(m, inputs, **{k: v for k, v in kwargs.items() if k != "inputs"}, inputs=kwargs["inputs"])
            got = [c.outputs for c in followed.cycles]
        elif inner:
            raise RefKitError(f"example {number} gives the registers {sorted(inner)}: only a Module run cycle by cycle can be followed")
        else:
            got = run(m, inputs, **kwargs, _examples=False)
        for cycle, (want, have) in enumerate(zip(expected, got)):
            for name, value in want.items():
                if value is None:
                    continue
                now = followed.cycles[cycle].registers.get(name) if name in inner else have.get(name)
                if now != value:
                    raise ExampleMismatch(
                        f"example {number}, cycle {cycle}: the description gives {name} = {value}, the machine gives "
                        f"{now} (inputs of that cycle: {dict(inputs[cycle])})" + _origin(followed, cycle, name, inner))


def _origin(followed: "Trace | None", cycle: int, name: str, inner: set[str]) -> str:
    """Where a value an example disputes came from, as `Trace.explain` says it: the line behind it, and the cycle it ran in."""
    if followed is None:
        return ""
    c = followed.cycles[cycle]

    def then(ran: int) -> str:
        """The registers in the cycle a line ran: the values it computed from."""
        if not 0 <= ran < len(followed.cycles):
            return ""
        shown = " ".join(f"{k}={v if not isinstance(v, int) or v < 16 else hex(v)}" for k, v in followed.cycles[ran].registers.items())
        return f" (registers then: {shown})" if shown else ""

    if name in inner:
        line = followed.cycles[cycle - 1].written.get(name) if cycle > 0 else None
        if cycle > 0 and followed.cycles[cycle - 1].reset or line is None and cycle > 0 and c.reset:
            return "; the reset gave it"
        return "" if not isinstance(line, int) else f"; {name} is a register: line {line} `{followed.line(line)}` wrote it in cycle {cycle - 1}{then(cycle - 1)}"
    origin = c.set_by.get(name)
    if origin == RESET:
        return "; the reset gave it"
    if not isinstance(origin, int):
        return ""
    text = f"line {origin} `{followed.line(origin)}`"
    if name in c.written_in:
        return f"; {name} is a register: {text} wrote it in cycle {c.written_in[name]}{then(c.written_in[name])}"
    return f"; {text} set it in this cycle (@comb){then(cycle)}"


def _check_moore(m: Machine, cycles: Sequence[Mapping[str, int]], spec: ResetSpec | None, widths: Mapping[str, int] | None) -> None:
    """`moore = True` says no output reads the inputs: perturb every input of each cycle and see that the outputs hold."""
    s = m.reset_values()
    for x in cycles[:64]:
        base = _step(m, s, x, spec, widths)[1] if spec is None or spec.style != "async" else None
        if base is not None:
            for name, value in x.items():
                if spec is not None and name == spec.port:
                    continue
                flipped = dict(x)
                flipped[name] = int(value) ^ 1
                if _step(m, s, flipped, spec, widths)[1] != base:
                    raise RefKitError(
                        f"moore = True, but the outputs change with the input {name!r} in the same cycle: an output that reads an "
                        "input is a Mealy output (set moore = False) -- or it should be registered, so that it shows one cycle later")
        s = _step(m, s, x, spec, widths)[0]


# ─── comparing two readings exactly ─────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Comparison:
    equivalent: bool
    #: Shortest input sequence after which the two machines' outputs differ (empty when equivalent).
    counterexample: list[dict[str, int]] = field(default_factory=list)
    outputs_a: dict[str, int | None] = field(default_factory=dict)
    outputs_b: dict[str, int | None] = field(default_factory=dict)
    #: False when the search stopped at `max_states` without finding a difference: "no difference found", not a proof.
    complete: bool = True
    states_seen: int = 0


def compare(
    a: type[Machine] | Machine, b: type[Machine] | Machine, inputs: Mapping[str, int], *, reset: str | None = None,
    active: int = 1, style: str = "sync", widths: Mapping[str, int] | None = None, max_states: int = 20000,
    max_input_values: int = 4096,
) -> Comparison:
    """Do two machines give the same outputs for EVERY input sequence? `inputs` maps each non-clock input to its width.

    Breadth-first over the pairs of registers the two machines can reach together, so the counterexample returned is the
    shortest. Exact for machines whose reachable state space fits in `max_states`; otherwise `complete` is False.
    """
    ma, mb = _resolve(a), _resolve(b)
    ma._in_widths = dict(inputs)  # type: ignore[attr-defined]
    mb._in_widths = dict(inputs)  # type: ignore[attr-defined]
    ma._out_widths = mb._out_widths = widths  # type: ignore[attr-defined]
    spec = ResetSpec(reset, active, style) if reset is not None else None
    names = list(inputs)
    total = 1
    for n in names:
        total *= 1 << inputs[n]
    if total > max_input_values:
        raise RefKitError(f"{total} input combinations per cycle is more than max_input_values={max_input_values}")
    alphabet = [dict(zip(names, values)) for values in product(*[range(1 << inputs[n]) for n in names])]
    key = lambda sa, sb: (tuple(sorted(sa.items())), tuple(sorted(sb.items())))  # noqa: E731
    start = (ma.reset_values(), mb.reset_values())
    seen = {key(*start)}
    queue: deque[tuple[dict[str, Any], dict[str, Any], list[dict[str, int]]]] = deque([(*start, [])])
    while queue:
        sa, sb, path = queue.popleft()
        for x in alphabet:
            na, oa = _step(ma, sa, x, spec, widths)
            nb, ob = _step(mb, sb, x, spec, widths)
            if any(oa.get(n) is not None and ob.get(n) is not None and oa.get(n) != ob.get(n) for n in set(oa) | set(ob)):
                return Comparison(False, [*path, x], oa, ob, True, len(seen))
            k = key(na, nb)
            if k not in seen:
                if len(seen) >= max_states:
                    return Comparison(True, [], {}, {}, False, len(seen))
                seen.add(k)
                queue.append((na, nb, [*path, x]))
    return Comparison(True, [], {}, {}, True, len(seen))


# ═══ The Verilog-shaped layer ═══════════════════════════════════════════════════════════════════════════════════════════
#
# The same machine, written the way a Verilog module is: registers declared at the top, `@seq` blocks for `always @(posedge
# clk)` with non-blocking assignments (`self.count.next = ...`, Verilog `count <= ...`), `@comb` blocks for `assign` and
# `always @(*)`, and values that are bit vectors with Verilog's widths (`Bits`). It runs on the same engine as `Machine`, so
# the timing, the reset, `run` and `compare` are the same.

class Value:
    """A Verilog expression: a bit vector whose value is computed with Verilog's width rules when it is used.

    Every input, register, wire and output is a `Bits` (a sized value). Operators do not compute at once: they build an expression
    that is evaluated where Verilog evaluates it, at the width Verilog would use (IEEE 1364 section 5.4):

    - `+ - * / % & | ^ ~` and unary `-` are context-determined: in an assignment they are computed at the larger of the
      destination's width and the widest operand, so `self.sum9 = self.a8 + self.b8` keeps the carry, exactly as
      `assign sum9 = a8 + b8;` does;
    - a comparison computes both sides at the width of the wider side and gives a Python bool;
    - a plain Python integer is a Verilog unsized constant: 32 bits wide (so `self.count + 1 == 16` is true for a 4-bit count at 15,
      as in Verilog);
    - shifts take the width of the left operand; the shift amount is self-determined;
    - `cat()`, part selects, reductions and `int()` / `bool()` / `if` use the expression's own width.
    All values are unsigned; `.signed`, `.sext()`, `.sra()`, `.slt()` ... give the signed meaning where the description needs it.
    """

    __slots__ = ()
    w: int

    def _ev(self, ctx: int) -> int:  # the value at width `ctx` (>= self.w), as an unsigned integer
        raise NotImplementedError

    # conversions (self-determined)
    def __int__(self) -> int:
        return self._ev(self.w)

    def __index__(self) -> int:
        return self._ev(self.w)

    def __bool__(self) -> bool:
        return self._ev(self.w) != 0

    def __hash__(self) -> int:
        return hash(int(self))

    def __repr__(self) -> str:
        return f"{self.w}'d{int(self)}"

    def __format__(self, spec: str) -> str:
        return format(int(self), spec)

    @property
    def width(self) -> int:
        return self.w

    @property
    def value(self) -> int:
        return int(self)

    @property
    def signed(self) -> int:
        """The two's-complement meaning of the value at its own width (Verilog `$signed(v)` read as a number)."""
        return signed(int(self), self.w)

    # building expressions
    def __add__(self, o: Any) -> "Value": return _Bin("+", self, _lift(o))
    def __radd__(self, o: Any) -> "Value": return _Bin("+", _lift(o), self)
    def __sub__(self, o: Any) -> "Value": return _Bin("-", self, _lift(o))
    def __rsub__(self, o: Any) -> "Value": return _Bin("-", _lift(o), self)
    def __mul__(self, o: Any) -> "Value": return _Bin("*", self, _lift(o))
    def __rmul__(self, o: Any) -> "Value": return _Bin("*", _lift(o), self)
    def __floordiv__(self, o: Any) -> "Value": return _Bin("/", self, _lift(o))
    def __rfloordiv__(self, o: Any) -> "Value": return _Bin("/", _lift(o), self)
    def __truediv__(self, o: Any) -> "Value": return _Bin("/", self, _lift(o))
    def __mod__(self, o: Any) -> "Value": return _Bin("%", self, _lift(o))
    def __rmod__(self, o: Any) -> "Value": return _Bin("%", _lift(o), self)
    def __pow__(self, o: Any) -> "Value": return _Bin("**", self, _lift(o))
    def __and__(self, o: Any) -> "Value": return _Bin("&", self, _lift(o))
    def __rand__(self, o: Any) -> "Value": return _Bin("&", _lift(o), self)
    def __or__(self, o: Any) -> "Value": return _Bin("|", self, _lift(o))
    def __ror__(self, o: Any) -> "Value": return _Bin("|", _lift(o), self)
    def __xor__(self, o: Any) -> "Value": return _Bin("^", self, _lift(o))
    def __rxor__(self, o: Any) -> "Value": return _Bin("^", _lift(o), self)
    def __invert__(self) -> "Value": return _Un("~", self)
    def __neg__(self) -> "Value": return _Un("-", self)
    def __pos__(self) -> "Value": return self
    def __lshift__(self, n: Any) -> "Value": return _Shift("<<", self, int(n))
    def __rshift__(self, n: Any) -> "Value": return _Shift(">>", self, int(n))

    @property
    def next(self) -> Any:
        raise RefKitError("only a register (Reg or State) has .next; this is a value of the current cycle")

    @next.setter
    def next(self, value: Any) -> None:
        raise RefKitError("only a register (Reg or State) has .next; this is a value of the current cycle: declare a Reg to "
                          "hold a value across clock edges")

    def xnor(self, o: Any) -> "Value":
        """Verilog `a ~^ b`."""
        return _Un("~", _Bin("^", self, _lift(o)))

    # comparisons: both sides at the wider width, unsigned
    def _cmp(self, o: Any) -> tuple[int, int]:
        other = _lift(o)
        ctx = max(self.w, other.w)
        return self._ev(ctx), other._ev(ctx)

    def __eq__(self, o: Any) -> bool:  # type: ignore[override]
        if isinstance(o, (str, _StateHandle)):
            return False
        try:
            a, b = self._cmp(o)
        except (TypeError, ValueError, RefKitError):
            return False
        return a == b

    def __ne__(self, o: Any) -> bool:  # type: ignore[override]
        return not self.__eq__(o)

    def __lt__(self, o: Any) -> bool: a, b = self._cmp(o); return a < b
    def __le__(self, o: Any) -> bool: a, b = self._cmp(o); return a <= b
    def __gt__(self, o: Any) -> bool: a, b = self._cmp(o); return a > b
    def __ge__(self, o: Any) -> bool: a, b = self._cmp(o); return a >= b

    # signed comparisons and arithmetic (Verilog with $signed operands)
    def _scmp(self, o: Any) -> tuple[int, int]:
        other = _lift(o)
        ctx = max(self.w, other.w)
        return signed(_sext_to(self, ctx), ctx), signed(_sext_to(other, ctx), ctx)

    def slt(self, o: Any) -> bool: a, b = self._scmp(o); return a < b
    def sle(self, o: Any) -> bool: a, b = self._scmp(o); return a <= b
    def sgt(self, o: Any) -> bool: a, b = self._scmp(o); return a > b
    def sge(self, o: Any) -> bool: a, b = self._scmp(o); return a >= b

    def sdiv(self, o: Any) -> "Bits":
        """Signed division, truncating toward zero (Verilog `$signed(a) / $signed(b)`); division by zero gives all ones (x)."""
        other = _lift(o)
        ctx = max(self.w, other.w)
        a, b = signed(_sext_to(self, ctx), ctx), signed(_sext_to(other, ctx), ctx)
        if b == 0:
            return Bits((1 << ctx) - 1, ctx)
        q = abs(a) // abs(b)
        return Bits(q if (a < 0) == (b < 0) else -q, ctx)

    def smod(self, o: Any) -> "Bits":
        """Signed remainder, with the sign of the dividend (Verilog `%` on signed operands)."""
        other = _lift(o)
        ctx = max(self.w, other.w)
        a, b = signed(_sext_to(self, ctx), ctx), signed(_sext_to(other, ctx), ctx)
        if b == 0:
            return Bits((1 << ctx) - 1, ctx)
        r = abs(a) % abs(b)
        return Bits(r if a >= 0 else -r, ctx)

    def smul(self, o: Any) -> "Bits":
        """Signed multiplication of the two values (each sign-extended), as wide as both widths together."""
        other = _lift(o)
        return Bits(self.signed * other.signed, self.w + other.w)

    def sra(self, n: Any) -> "Bits":
        """Arithmetic shift right (Verilog `>>>` on a signed value): the sign bit is copied in."""
        return Bits(self.signed >> int(n), self.w)

    # selects (self-determined)
    def __getitem__(self, key: Any) -> "Bits":
        v = int(self)
        if isinstance(key, slice):
            if key.step is not None or key.start is None or key.stop is None:
                raise RefKitError("slice a value as v[high:low], both ends given, e.g. v[7:4]")
            high, low = int(key.start), int(key.stop)
            if high < low:
                raise RefKitError(f"v[{high}:{low}]: write the high bit first, as Verilog does (v[{low}:{high}] -> v[{high}:{low}])")
            if high >= self.w:
                raise RefKitError(f"v[{high}:{low}] of a {self.w}-bit value: bit {high} does not exist")
            return Bits(v >> low, high - low + 1)
        i = int(key)
        if not 0 <= i < self.w:
            raise RefKitError(f"bit {i} of a {self.w}-bit value does not exist")
        return Bits((v >> i) & 1, 1)

    def part(self, base: Any, width: int) -> "Bits":
        """Verilog `v[base +: width]`: `width` bits starting at bit `base` going up."""
        b = int(base)
        if b < 0 or b + width > self.w:
            raise RefKitError(f"v[{b} +: {width}] of a {self.w}-bit value is out of range")
        return Bits(int(self) >> b, width)

    def part_down(self, base: Any, width: int) -> "Bits":
        """Verilog `v[base -: width]`: `width` bits ending at bit `base` going down."""
        return self.part(int(base) - width + 1, width)

    def set_bit(self, index: Any, bit_value: Any) -> "Bits":
        """A copy with bit `index` replaced (for `r[i] <= b;` write `self.r.next = self.r.set_bit(i, b)`, or use `next_bit`)."""
        i = int(index)
        if not 0 <= i < self.w:
            raise RefKitError(f"bit {i} of a {self.w}-bit value does not exist")
        v = int(self) & ~(1 << i) | ((int(bit_value) & 1) << i)
        return Bits(v, self.w)

    def set_slice(self, high: int, low: int, part_value: Any) -> "Bits":
        """A copy with bits high..low replaced (Verilog `r[high:low] <= v;`)."""
        if high < low or high >= self.w:
            raise RefKitError(f"[{high}:{low}] of a {self.w}-bit value is out of range or reversed")
        width = high - low + 1
        m = ((1 << width) - 1) << low
        return Bits(int(self) & ~m | ((_lift(part_value)._ev(max(width, _lift(part_value).w)) & ((1 << width) - 1)) << low), self.w)

    def zext(self, width: int) -> "Bits":
        """Zero-extend (or truncate) to `width` bits."""
        return Bits(int(self), width)

    def sext(self, width: int) -> "Bits":
        """Sign-extend (or truncate) to `width` bits."""
        return Bits(self.signed, width)

    def rep(self, times: int) -> "Bits":
        """Verilog `{times{v}}`."""
        return cat(*([Bits(int(self), self.w)] * times))

    def shift_in_low(self, new: Any) -> "Bits":
        """The value with `new` shifted in at bit 0: every old bit moves up (toward the most significant bit) by the width of
        `new`, and the top ones drop out. Verilog `{v[w-2:0], new}`, or `(v << 1) | new` for one bit. This is the shift for
        bits that arrive MOST significant first: after w shifts the first bit is the top bit."""
        n = _shift_part(new)
        return Bits((int(self) << n.w) | int(n), self.w)

    def shift_in_high(self, new: Any) -> "Bits":
        """The value with `new` shifted in at the top bit: every old bit moves down (toward bit 0) by the width of `new`, and the
        bottom ones drop out. Verilog `{new, v[w-1:1]}` for one bit. This is the shift for bits that arrive LEAST significant
        first: after w shifts the first bit is bit 0."""
        n = _shift_part(new)
        return Bits(((int(n) << self.w) | int(self)) >> n.w, self.w)

    def reduce_and(self) -> "Bits": return Bits(int(int(self) == (1 << self.w) - 1), 1)
    def reduce_or(self) -> "Bits": return Bits(int(int(self) != 0), 1)
    def reduce_xor(self) -> "Bits": return Bits(popcount(int(self)) & 1, 1)
    def reduce_nand(self) -> "Bits": return Bits(1 - int(self.reduce_and()), 1)
    def reduce_nor(self) -> "Bits": return Bits(1 - int(self.reduce_or()), 1)
    def reduce_xnor(self) -> "Bits": return Bits(1 - int(self.reduce_xor()), 1)


class Bits(Value):
    """A sized value: `Bits(5, 4)` is Verilog `4'd5`. Inputs, registers, wires and outputs are Bits."""

    __slots__ = ("v", "w")

    def __init__(self, value: Any, width: int) -> None:
        if width < 1:
            raise RefKitError(f"a value needs a width of at least 1, got {width}")
        self.w = int(width)
        if isinstance(value, Value):
            raw = value._ev(max(self.w, value.w))      # an assignment: computed at the wider of the two widths, then truncated
        elif isinstance(value, _StateHandle):
            raise RefKitError("a state name is not a number: compare it (self.st == 'A') instead of converting it")
        else:
            raw = int(value)
        self.v = raw & ((1 << self.w) - 1)

    def _ev(self, ctx: int) -> int:
        return self.v


class _Bin(Value):
    __slots__ = ("op", "a", "b", "w")

    def __init__(self, op: str, a: Value, b: Value) -> None:
        self.op, self.a, self.b = op, a, b
        self.w = max(a.w, b.w)

    def _ev(self, ctx: int) -> int:
        x, y, m = self.a._ev(ctx), self.b._ev(ctx), (1 << ctx) - 1
        op = self.op
        if op == "+": r = x + y
        elif op == "-": r = x - y
        elif op == "*": r = x * y
        elif op == "/": r = x // y if y else m          # division by zero is x in Verilog: all ones here
        elif op == "%": r = x % y if y else m
        elif op == "**": r = pow(x, y, 1 << ctx)
        elif op == "&": r = x & y
        elif op == "|": r = x | y
        else: r = x ^ y
        return r & m


class _Un(Value):
    __slots__ = ("op", "a", "w")

    def __init__(self, op: str, a: Value) -> None:
        self.op, self.a, self.w = op, a, a.w

    def _ev(self, ctx: int) -> int:
        x, m = self.a._ev(ctx), (1 << ctx) - 1
        return (~x & m) if self.op == "~" else (-x & m)


class _Shift(Value):
    __slots__ = ("op", "a", "n", "w")

    def __init__(self, op: str, a: Value, n: int) -> None:
        if n < 0:
            raise RefKitError(f"shift by a negative amount ({n})")
        self.op, self.a, self.n, self.w = op, a, n, a.w

    def _ev(self, ctx: int) -> int:
        x, m = self.a._ev(ctx), (1 << ctx) - 1
        return ((x << self.n) & m) if self.op == "<<" else (x >> self.n)


def _lift(o: Any) -> Value:
    """An operand: a value as it is, a bool as 1'b0/1'b1, a Python integer as a Verilog unsized constant (32 bits, or wider)."""
    if isinstance(o, Value):
        return o
    if isinstance(o, bool):
        return Bits(int(o), 1)
    if isinstance(o, int):
        width = max(32, o.bit_length() + (1 if o < 0 else 0))
        return Bits(o, width)
    if isinstance(o, _StateHandle):
        raise RefKitError("a state name cannot be used in arithmetic; compare it with a name (self.st == 'A')")
    raise RefKitError(f"cannot use {type(o).__name__} {o!r} as a Verilog value")


def _shift_part(new: Any) -> Value:
    """What `shift_in_low` / `shift_in_high` shift in: a sized value, or a plain 0, 1, True or False (one bit)."""
    if isinstance(new, Value):
        return new
    if isinstance(new, bool) or (isinstance(new, int) and new in (0, 1)):
        return Bits(int(new), 1)
    raise RefKitError(f"shift_in_low/shift_in_high({new!r}): shift in a sized value (an input, a bit, B(value, width)) or 0/1")


def _sext_to(v: Value, width: int) -> int:
    return mask(signed(int(v), v.w), width)


def B(value: Any, width: int) -> Bits:
    """A sized constant: `B(5, 4)` is Verilog `4'd5`, `B(-1, 8)` is `8'hFF`."""
    return Bits(value, width)


def cat(*parts: Any) -> Bits:
    """Verilog `{a, b, c}`: the first part ends up in the most significant bits. Every part must be sized (an input, a register,
    a select, or a constant written B(value, width)), because a concatenation needs each part's width, as in Verilog."""
    out, width = 0, 0
    for p in parts:
        if isinstance(p, bool):
            p = Bits(int(p), 1)
        if not isinstance(p, Value):
            raise RefKitError(f"cat() needs sized parts so their widths are known; got {p!r}: write B({p!r}, width)")
        out, width = (out << p.w) | int(p), width + p.w
    return Bits(out, width)


def mux(condition: Any, if_true: Any, if_false: Any) -> Value:
    """Verilog `c ? a : b`, with the width of the wider branch (Python's `a if c else b` works too, at the branch's own width)."""
    a, b = _lift(if_true), _lift(if_false)
    chosen = a if bool(condition) else b
    return _Resize(chosen, max(a.w, b.w))


class _Resize(Value):
    __slots__ = ("a", "w")

    def __init__(self, a: Value, width: int) -> None:
        self.a, self.w = a, width

    def _ev(self, ctx: int) -> int:
        return self.a._ev(ctx)


def clog2(n: int) -> int:
    """Verilog `$clog2(n)`: the number of bits needed to count n values (0 for n <= 1)."""
    n = int(n)
    return 0 if n <= 1 else (n - 1).bit_length()


@dataclass(frozen=True)
class Wire:
    """An internal combinational signal of `width` bits (Verilog `wire`), set in a @comb block and readable after it."""

    width: int


@dataclass(frozen=True)
class Mem:
    """An array of `depth` registers of `width` bits each (Verilog `reg [width-1:0] mem [0:depth-1]`), all reset to `reset`.

    Read `self.mem[addr]`; write at the clock edge with `self.mem.write(addr, value)` in a @seq block."""

    depth: int
    width: int
    reset: int = 0


@dataclass(frozen=True)
class Input:
    """Optional declaration of an input port of `width` bits (Verilog `input [width-1:0] name`). Needed only in a module used as
    an Instance inside another; the top module's ports come from the block's interface."""

    width: int


@dataclass(frozen=True)
class Output:
    """Optional declaration of an output port of `width` bits (Verilog `output [width-1:0] name`). Needed only in a module used as
    an Instance inside another."""

    width: int


@dataclass(frozen=True)
class Instance:
    """A submodule (Verilog `Child u1 (.a(x), .y(w));`): `u1 = Instance(Child)`. In a @comb block call it with its inputs,
    `out = self.u1(a=self.x)`, and read its outputs as `out.y`. It takes the clock edge with the parent, with the inputs it was
    given in that cycle; its registers are reset with the parent's."""

    module: type

    def instance(self) -> "Module":
        return self.module()


class _InstanceHandle:
    __slots__ = ("_decl", "_state", "_frame", "_name")

    def __init__(self, decl: Instance, state: tuple, frame: "_Frame", name: str) -> None:
        self._decl, self._state, self._frame, self._name = decl, state, frame, name

    def __call__(self, **inputs: Any) -> "_View":
        child = self._decl.instance()
        child_inputs = {k: (v if isinstance(v, Value) else _lift(v)) for k, v in inputs.items()}
        widths = {k: d.width for k, d in child._decls.items() if isinstance(d, Input)}
        x = {k: (Bits(v, widths[k]).v if k in widths else int(v)) for k, v in child_inputs.items()}
        child._in_widths = {k: widths.get(k, child_inputs[k].w) for k in x}
        object.__getattribute__(self._frame, "_connections")[self._name] = x
        outs = child._engine_outputs(dict(self._state), x)
        out_widths = {k: d.width for k, d in child._decls.items() if isinstance(d, Output)}
        return _View({k: (None if v is None else Bits(v, out_widths.get(k, max(1, int(v).bit_length())))) for k, v in outs.items()})


_MISSING = object()


def _same_value(view: Any, name: str, value: Any) -> bool:
    """Whether `value` is the register's own current value (writing an output-register to itself in @comb is a no-op)."""
    try:
        current = getattr(view, name)
    except RefKitError:
        return False
    try:
        return int(value) == int(current)
    except (TypeError, ValueError):
        return False


class _FileMemory:
    """Contents loaded from a file (the harness gives the same words to the hardware): `self.memory[i]` is word i, as an int."""

    __slots__ = ("_words",)

    def __init__(self, words: Any) -> None:
        self._words = words

    def __getitem__(self, index: Any) -> int:
        return int(self._words[int(index)])


def seq(function: Callable[..., Any] | None = None, *, edge: str = "pos") -> Any:
    """Marks a clocked block; assign registers in it with `self.r.next = value`.

    `@seq` is `always @(posedge clk)`. `@seq(edge="neg")` is `always @(negedge clk)` and `@seq(edge="both")` is
    `always @(posedge clk or negedge clk)` (a dual-edge flip-flop); a module that uses either is run step by step
    (`run_stepped`), with the clock as an input."""
    if edge not in ("pos", "neg", "both"):
        raise RefKitError(f'@seq(edge=...) takes "pos", "neg" or "both", not {edge!r}')

    def mark(f: Callable[..., Any]) -> Callable[..., Any]:
        f._refkit_kind = "seq"  # type: ignore[attr-defined]
        f._refkit_edge = edge  # type: ignore[attr-defined]
        return f

    return mark(function) if function is not None else mark


def latch(function: Callable[..., Any]) -> Callable[..., Any]:
    """Marks a level-sensitive block (a latch, Verilog `always @(*) if (en) q <= d;`): it runs at every step, and a `.next`
    assignment it makes takes effect AT ONCE (the latch is transparent); when it assigns nothing, the register holds. A module
    that uses one is run step by step (`run_stepped`)."""
    function._refkit_kind = "latch"  # type: ignore[attr-defined]
    return function


def casez(value: Any, pattern: str) -> bool:
    """Verilog `casez`/`casex` matching: `pattern` is the bits from the most significant, each 0, 1, or ? / x / z for "any";
    `_` is ignored. `casez(self.op, "1?0")` is true for 100 and 110. A pattern shorter than the value matches its low bits."""
    bits_ = pattern.replace("_", "")
    v = int(value)
    for i, ch in enumerate(reversed(bits_)):
        if ch in "?xzXZ":
            continue
        if ch not in "01":
            raise RefKitError(f"casez pattern {pattern!r}: use only 0, 1, ?, x, z and _")
        if (v >> i) & 1 != int(ch):
            return False
    return True


def comb(function: Callable[..., Any]) -> Callable[..., Any]:
    """Marks `assign` / `always @(*)`: runs every cycle, before the edge; set outputs and wires with `self.y = value`."""
    function._refkit_kind = "comb"  # type: ignore[attr-defined]
    return function


class _RegHandle(Bits):
    """The current value of a register inside a block; `.next = v` is the non-blocking assignment."""

    __slots__ = ("_frame", "_name")

    def __init__(self, value: int, width: int, frame: "_Frame", name: str) -> None:
        super().__init__(value, width)
        object.__setattr__(self, "_frame", frame)
        object.__setattr__(self, "_name", name)

    @property
    def next(self) -> Bits:
        raise RefKitError(f"{self._name}.next is write-only; read {self._name} for the value it has now")

    @next.setter
    def next(self, value: Any) -> None:
        self._frame._assign_next(self._name, value)

    def __setitem__(self, key: Any, value: Any) -> None:
        raise RefKitError(f"self.{self._name}[...] = ... : a register changes only at the clock edge; in @seq write "
                          f"self.{self._name}.next_bit(i, v) (Verilog {self._name}[i] <= v) or "
                          f"self.{self._name}.next_slice(hi, lo, v) ({self._name}[hi:lo] <= v)")

    def next_bit(self, index: Any, bit_value: Any) -> None:
        """Verilog `r[index] <= bit_value;`: only that bit changes at the edge; several in one edge all take effect."""
        current = self._frame._pending_value(self._name, int(self), self.w)
        self._frame._assign_next(self._name, Bits(current, self.w).set_bit(index, bit_value))

    def next_slice(self, high: int, low: int, part_value: Any) -> None:
        """Verilog `r[high:low] <= part_value;`."""
        current = self._frame._pending_value(self._name, int(self), self.w)
        self._frame._assign_next(self._name, Bits(current, self.w).set_slice(high, low, part_value))



class _PortBits(Bits):
    """The value of an input port in this cycle. The block reads it; it has no `.next`."""

    __slots__ = ("_name",)

    def __init__(self, value: int, width: int, name: str) -> None:
        super().__init__(value, width)
        object.__setattr__(self, "_name", name)

    @property
    def next(self) -> Any:
        raise RefKitError(f"{self._name} is an input port: it has no .next")

    @next.setter
    def next(self, value: Any) -> None:
        raise RefKitError(
            f"self.{self._name}.next = ... : {self._name} is an input port; the block reads it and cannot change it. A value the "
            "block keeps from one cycle to the next is a register: declare it (r = Reg(width)) and assign self.r.next in @seq")

    def __setitem__(self, key: Any, value: Any) -> None:
        raise RefKitError(f"self.{self._name}[...] = ... : {self._name} is an input port; the block cannot change it")


class _NetBits(Bits):
    """An output or a wire inside a block. In @comb its bits can be set one at a time (`self.g[0] = ...`, `self.g[3:2] = ...`,
    Verilog `assign g[0] = ...;`); it can be read once every bit is set."""

    __slots__ = ("_frame", "_name", "_unset")

    def __init__(self, value: int, width: int, frame: "_Frame", name: str, unset: int) -> None:
        super().__init__(value, width)
        object.__setattr__(self, "_frame", frame)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_unset", unset)

    def _ev(self, ctx: int) -> int:
        if self._unset:
            if self._unset == (1 << self.w) - 1:
                raise RefKitError(f"{self._name} is read before any @comb block set it: put the block that sets it first")
            raise RefKitError(f"{self._name} is read before all its bits are set (bits not set: {_bit_list(self._unset)})")
        return self.v

    def __setitem__(self, key: Any, value: Any) -> None:
        self._frame._set_bits(self._name, key, value)

    @property
    def next(self) -> Any:
        raise RefKitError(f"{self._name} is an output or a wire: it has no .next")

    @next.setter
    def next(self, value: Any) -> None:
        raise RefKitError(
            f"self.{self._name}.next = ... : {self._name} is an output (or a wire), set in @comb with self.{self._name} = ... . "
            f"For an output that is a register, declare a Reg with its name ({self._name} = Reg()) and assign "
            f"self.{self._name}.next in @seq")


def _bit_list(bits_: int) -> list[int]:
    return [i for i in range(bits_.bit_length()) if (bits_ >> i) & 1]


class _StateHandle:
    """The current state of a `State` register: compare it with a name; `.next = "NAME"` changes it at the edge."""

    __slots__ = ("_value", "_frame", "_name")

    def __init__(self, value: str, frame: "_Frame", name: str) -> None:
        self._value, self._frame, self._name = value, frame, name

    def __eq__(self, other: Any) -> bool:  # type: ignore[override]
        return self._value == (other._value if isinstance(other, _StateHandle) else other)

    def __ne__(self, other: Any) -> bool:  # type: ignore[override]
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self._value)

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return repr(self._value)

    @property
    def value(self) -> str:
        return self._value

    @property
    def next(self) -> str:
        raise RefKitError(f"{self._name}.next is write-only; read {self._name} for the state it is in now")

    @next.setter
    def next(self, value: Any) -> None:
        self._frame._assign_next(self._name, str(value) if isinstance(value, _StateHandle) else value)


class _MemHandle:
    __slots__ = ("_values", "_decl", "_frame", "_name")

    def __init__(self, values: tuple[int, ...], decl: Mem, frame: "_Frame", name: str) -> None:
        self._values, self._decl, self._frame, self._name = values, decl, frame, name

    def __getitem__(self, address: Any) -> Bits:
        a = int(address)
        if not 0 <= a < self._decl.depth:
            raise RefKitError(f"{self._name}[{a}]: the memory has addresses 0 to {self._decl.depth - 1}")
        return Bits(self._values[a], self._decl.width)

    def __len__(self) -> int:
        return self._decl.depth

    def write(self, address: Any, value: Any) -> None:
        """Non-blocking write: the word changes at the clock edge (Verilog `mem[address] <= value`)."""
        self._frame._write_mem(self._name, int(address), value)


class _Frame:
    """What `self` is inside a block: inputs, registers, wires and outputs of one cycle, with Verilog's rules enforced."""

    def __init__(self, module: "Module", s: Mapping[str, Any], x: Mapping[str, int], mode: str) -> None:
        d = object.__setattr__
        d(self, "_module", module)
        d(self, "_s", s)
        d(self, "_x", x)
        d(self, "_mode", mode)
        d(self, "_nets", {})
        d(self, "_pending", {})
        d(self, "_mem_writes", {})
        d(self, "_connections", {})
        d(self, "_partial", {})         # outputs and wires with only some bits set: name -> (value, bits not yet set)
        d(self, "_reads", set())        # inputs read by @seq blocks (the reset check needs to know)
        d(self, "_origins", {})         # register -> the line of the model's code that last assigned its .next
        d(self, "_net_origins", {})     # output or wire -> the line of the model's code that last set it in @comb

    def __getattr__(self, name: str) -> Any:
        m = object.__getattribute__(self, "_module")
        nets = object.__getattribute__(self, "_nets")
        s = object.__getattribute__(self, "_s")
        x = object.__getattribute__(self, "_x")
        if name in nets:
            v = nets[name]
            return v if v is None else _NetBits(v.v, v.w, self, name, 0)
        decl = m._decls.get(name)
        if isinstance(decl, Reg):
            return _RegHandle(s[name], _reg_width(m, name, decl), self, name)
        if isinstance(decl, State):
            return _StateHandle(s[name], self, name)
        if isinstance(decl, Mem):
            return _MemHandle(s[name], decl, self, name)
        if isinstance(decl, Instance):
            return _InstanceHandle(decl, s[name], self, name)
        key = name[:-1] if name.endswith("_") and name not in x and name[:-1] in x else name
        if key in x:
            width = (getattr(m, "_in_widths", None) or {}).get(key)
            if width is None and isinstance(m._decls.get(key), Input):
                width = m._decls[key].width
            if object.__getattribute__(self, "_mode") == "seq":
                object.__getattribute__(self, "_reads").add(key)
            return _PortBits(x[key], width if width is not None else max(1, int(x[key]).bit_length()), key)
        if name == "memory" and getattr(m, "_memory", None) is not None:
            return _FileMemory(m._memory)
        attr = getattr(type(m), name, _MISSING)
        if attr is not _MISSING and not isinstance(attr, (Reg, State, Mem, Wire, Input, Output, Instance)):
            if callable(attr) and getattr(attr, "_refkit_kind", None) is None and not isinstance(attr, type):
                return attr.__get__(self)                # a function of the module (a Verilog function): self is this cycle
            if not callable(attr):
                return attr                              # a parameter (WIDTH = 8)
        width = self._net_width(name)
        if width is not None:                            # not set yet: its bits can be set one by one; reading it raises
            value, unset = object.__getattribute__(self, "_partial").get(name, (0, (1 << width) - 1))
            return _NetBits(value, width, self, name, unset)
        raise RefKitError(
            f"no input, register, wire or output called {name!r}. Inputs: {sorted(x)}; "
            f"registers: {sorted(n for n, d in m._decls.items() if isinstance(d, (Reg, State, Mem)))}")

    def __setattr__(self, name: str, value: Any) -> None:
        m = object.__getattribute__(self, "_module")
        mode = object.__getattribute__(self, "_mode")
        decl = m._decls.get(name)
        if isinstance(decl, Reg) and mode == "comb" and _same_value(self, name, value):
            return                                       # `self.q = self.q` for an output that is a register: it already is
        if isinstance(decl, (Reg, State, Mem)):
            raise RefKitError(
                f"self.{name} = ... : {name} is a register; assign it in a @seq block with self.{name}.next = ... "
                "(Verilog's non-blocking <=)" + (", or self.{0}.write(addr, value) for a memory".format(name) if isinstance(decl, Mem) else ""))
        outs = getattr(m, "_out_widths", None) or {}
        if mode in ("seq", "latch"):
            raise RefKitError(
                f"self.{name} = ... inside @seq: a @seq block only assigns registers (self.r.next = ...). Drive outputs and wires "
                "in a @comb block; for an output that is a register, declare a Reg with the output's name")
        if isinstance(decl, Wire):
            width = decl.width
        elif isinstance(decl, Output) and name not in outs:
            width = decl.width
        elif name in outs:
            width = outs[name]
        else:
            raise RefKitError(f"self.{name} = ... : {name} is not an output port or a declared Wire (outputs: {sorted(outs)})")
        nets = object.__getattribute__(self, "_nets")
        nets[name] = None if value is None else Bits(value, width)
        object.__getattribute__(self, "_partial").pop(name, None)
        object.__getattribute__(self, "_net_origins")[name] = _caller_line()

    def _net_width(self, name: str) -> int | None:
        """The width of an output port or a wire called `name`, or None when it is neither."""
        m = object.__getattribute__(self, "_module")
        decl = m._decls.get(name)
        outs = getattr(m, "_out_widths", None) or {}
        if isinstance(decl, Wire):
            return decl.width
        if name in outs and not isinstance(decl, (Reg, State, Mem)):
            return outs[name]
        if isinstance(decl, Output):
            return decl.width
        return None

    def _set_bits(self, name: str, key: Any, value: Any) -> None:
        """`self.g[i] = v` / `self.g[hi:lo] = v` in @comb (Verilog `assign g[i] = v;`)."""
        if object.__getattribute__(self, "_mode") != "comb":
            raise RefKitError(
                f"self.{name}[...] = ... inside @seq: a @seq block only assigns registers (self.r.next = ...). Drive outputs and "
                "wires in a @comb block")
        width = self._net_width(name)
        if isinstance(key, slice):
            if key.step is not None or key.start is None or key.stop is None:
                raise RefKitError(f"self.{name}[high:low] = ... : give both ends, high bit first")
            high, low = int(key.start), int(key.stop)
        else:
            high = low = int(key)
        if high < low:
            raise RefKitError(f"self.{name}[{high}:{low}] = ... : write the high bit first ({name}[{low}:{high}])")
        if low < 0 or high >= width:
            raise RefKitError(f"self.{name}[{high}:{low}] = ... : {name} has bits {width - 1} to 0")
        if value is None:
            raise RefKitError(f"self.{name}[...] = None: a don't-care is given for a whole output (self.{name} = None)")
        nets = object.__getattribute__(self, "_nets")
        partial = object.__getattribute__(self, "_partial")
        if nets.get(name) is not None:
            current, unset = int(nets[name]), 0
        else:
            current, unset = partial.get(name, (0, (1 << width) - 1))
        new = int(Bits(current, width).set_slice(high, low, value))
        unset &= ~(((1 << (high - low + 1)) - 1) << low)
        object.__getattribute__(self, "_net_origins")[name] = _caller_line()
        if unset:
            partial[name] = (new, unset)
        else:
            partial.pop(name, None)
            nets[name] = Bits(new, width)

    def _assign_next(self, name: str, value: Any) -> None:
        if object.__getattribute__(self, "_mode") not in ("seq", "latch"):
            raise RefKitError(f"self.{name}.next = ... outside @seq: registers change only at the clock edge, in a @seq block")
        pending = object.__getattribute__(self, "_pending")
        object.__getattribute__(self, "_origins")[name] = _caller_line()
        decl = object.__getattribute__(self, "_module")._decls.get(name)
        if isinstance(decl, State):
            pending[name] = value._value if isinstance(value, _StateHandle) else value
        else:
            module = object.__getattribute__(self, "_module")
            pending[name] = Bits(value, _reg_width(module, name, decl)).v if isinstance(decl, Reg) else int(value)

    def _pending_value(self, name: str, current: int, width: int) -> int:
        return object.__getattribute__(self, "_pending").get(name, current)

    def _write_mem(self, name: str, address: int, value: Any) -> None:
        if object.__getattribute__(self, "_mode") != "seq":
            raise RefKitError(f"self.{name}.write(...) outside @seq: a memory is written at the clock edge, in a @seq block")
        width = object.__getattribute__(self, "_module")._decls[name].width
        object.__getattribute__(self, "_mem_writes").setdefault(name, []).append((address, Bits(value, width).v))


class Module(Machine):
    """A block written like a Verilog module. Declare its state as class attributes and its logic as decorated methods:

        class Ref(Module):
            count = Reg(4)                  # reg [3:0] count;  (reset value 0)
            busy = Wire(1)                  # wire busy;

            @comb                           # assign / always @(*)
            def logic(self):
                self.busy = self.count != 0
                self.q = self.count         # an output port

            @seq                            # always @(posedge clk)
            def counter(self):
                if self.en:
                    self.count.next = self.count + 1    # count <= count + 1;

    Inside a block `self.<name>` is an input port, a register (its value now), a wire or an output. @comb blocks run in the order
    they are written; @seq blocks see the values from before the edge, and all `.next` assignments take effect together at the
    edge, exactly like non-blocking assignments. An output port with the same name as a Reg is that register (`output reg`).
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        decls: dict[str, Any] = {}
        blocks: list[tuple[str, Callable[..., Any]]] = []
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, (Reg, State, Mem, Wire, Input, Output, Instance)):
                    decls[name] = value
                kind = getattr(value, "_refkit_kind", None)
                if kind is not None:
                    blocks = [b for b in blocks if b[1].__name__ != name]
                    blocks.append((kind, value))
        cls._decls = decls
        cls._comb = [f for k, f in blocks if k == "comb"]
        cls._seq = [f for k, f in blocks if k == "seq"]
        cls._latch = [f for k, f in blocks if k == "latch"]
        cls.needs_steps = bool(cls._latch) or any(getattr(f, "_refkit_edge", "pos") != "pos" for f in cls._seq)
        cls.regs = {n: d for n, d in decls.items() if isinstance(d, (Reg, State))}
        cls._mems = {n: d for n, d in decls.items() if isinstance(d, Mem)}
        cls._instances = {n: d for n, d in decls.items() if isinstance(d, Instance)}

    _decls: dict[str, Any] = {}
    _comb: list[Callable[..., Any]] = []
    _seq: list[Callable[..., Any]] = []
    _latch: list[Callable[..., Any]] = []
    _mems: dict[str, Mem] = {}
    _instances: dict[str, "Instance"] = {}
    needs_steps: bool = False

    @_class_or_instance
    def reset_values(cls) -> dict[str, Any]:
        out = {n: (mask(d.power_up, _reg_width(cls, n, d)) if isinstance(d, Reg) else d.reset) for n, d in cls.regs.items()}
        for n, d in cls._mems.items():
            out[n] = tuple([mask(d.reset or 0, d.width)] * d.depth)
        for n, d in cls._instances.items():
            out[n] = _freeze(d.module.reset_values())
        return out

    def _comb_frame(self, s: Mapping[str, Any], x: Mapping[str, int], mode: str) -> _Frame:
        frame = _Frame(self, dict(s), dict(x), "comb")
        for block in self._comb:
            block(frame)
        object.__setattr__(frame, "_mode", mode)
        return frame

    def _engine_outputs(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, int | None]:
        frame = self._comb_frame(s, x, "comb")
        nets = object.__getattribute__(frame, "_nets")
        partial = object.__getattribute__(frame, "_partial")
        self._net_origins = dict(object.__getattribute__(frame, "_net_origins"))  # type: ignore[attr-defined]  # for trace()
        out: dict[str, int | None] = {}
        for name in (getattr(self, "_out_widths", None) or {n: d.width for n, d in self._decls.items() if isinstance(d, Output)}):
            if name in partial:
                raise RefKitError(f"output {name}: bits {_bit_list(partial[name][1])} are not set in this cycle; set every bit "
                                  f"(self.{name}[i] = ...) or the whole output (self.{name} = ...)")
            if name in nets:
                out[name] = None if nets[name] is None else int(nets[name])
            elif isinstance(self._decls.get(name), Reg):
                out[name] = int(s[name])
            elif isinstance(self._decls.get(name), State):
                raise RefKitError(f"output {name} is a State register; drive the output's number in a @comb block")
            else:
                raise RefKitError(f"no @comb block sets the output {name}, and no Reg is called {name}")
        if getattr(self, "_out_widths", None) is None and not any(isinstance(d, Output) for d in self._decls.values()):
            out = {n: (None if v is None else int(v)) for n, v in nets.items() if not isinstance(self._decls.get(n), Wire)}
        return out

    def _engine_next(self, s: Mapping[str, Any], x: Mapping[str, int]) -> Mapping[str, Any]:
        if self.needs_steps:
            raise RefKitError("this module has a negedge or dual-edge @seq block or a @latch: it must be run step by step "
                              "(run_stepped), with the clock as an input")
        return self._change(s, x, self._seq, "seq")

    def _change(self, s: Mapping[str, Any], x: Mapping[str, int], blocks: Sequence[Callable[..., Any]], mode: str) -> dict[str, Any]:
        frame = self._comb_frame(s, x, mode)
        for block in blocks:
            block(frame)
        change: dict[str, Any] = dict(object.__getattribute__(frame, "_pending"))
        self._seq_reads = set(object.__getattribute__(frame, "_reads"))  # type: ignore[attr-defined]
        self._seq_origins = dict(object.__getattribute__(frame, "_origins"))  # type: ignore[attr-defined]
        if mode == "seq":                                 # instances take the clock edge with the inputs connected this cycle
            for name, decl in self._instances.items():
                connected = object.__getattribute__(frame, "_connections").get(name)
                if connected is None:
                    raise RefKitError(f"instance {name} was not connected in this cycle: call self.{name}(port=value, ...) in a @comb block")
                child, child_state = decl.instance(), dict(s[name])
                change[name] = _freeze(child._apply(child_state, child._change(child_state, connected, child._seq, "seq")))
        for name, writes in object.__getattribute__(frame, "_mem_writes").items():
            decl = self._mems[name]
            words = list(s[name])
            for address, value in writes:
                if not 0 <= address < decl.depth:
                    raise RefKitError(f"{name}.write({address}, ...): the memory has addresses 0 to {decl.depth - 1}")
                words[address] = mask(value, decl.width)
            change[name] = tuple(words)
        return change

    def _apply(self, s: dict[str, Any], change: Mapping[str, Any] | None) -> dict[str, Any]:
        mems = {n: v for n, v in (change or {}).items() if n in self._mems or n in self._instances}
        rest = {n: v for n, v in (change or {}).items() if n not in mems}
        new = super()._apply(s, rest)
        new.update(mems)
        return new




# ─── step mode: negedge, dual-edge and latches ──────────────────────────────────────────────────────────────────────────

def run_stepped(
    machine: type[Module] | Module, steps: Sequence[Mapping[str, int]], *, clock: str, reset: str | None = None, active: int = 1,
    style: str = "sync", widths: Mapping[str, int] | None = None, inputs: Mapping[str, int] | None = None, memory: Any = None,
) -> list[dict[str, int | None]]:
    """Run a Module step by step, the clock being an input of every step (the harness's step mode).

    Within a step the clock changes FIRST and the other inputs just after: an edge (0 to 1 rising, 1 to 0 falling, between the
    previous step and this one) runs the @seq blocks of that edge with the inputs of the PREVIOUS step; then this step's inputs
    are applied, an asynchronous reset acts, the @latch blocks run with them (transparent: their assignments take effect at
    once), and the outputs are read. Before the first step the clock and every input were 0. A synchronous reset asserted in the
    previous step acts at an edge that has @seq blocks, instead of those blocks.
    """
    m = _resolve(machine)
    if not isinstance(m, Module):
        raise RefKitError("run_stepped runs a Module")
    m._out_widths = widths  # type: ignore[attr-defined]
    if inputs is not None:
        m._in_widths = dict(inputs)  # type: ignore[attr-defined]
    if memory is not None:
        m._memory = memory  # type: ignore[attr-defined]
    spec = ResetSpec(reset, active, style) if reset is not None else None
    init = m.reset_values()
    s = dict(init)
    previous: dict[str, int] = {name: 0 for name in (inputs or {})}
    previous[clock] = 0
    out: list[dict[str, int | None]] = []
    for step in steps:
        x = {**previous, **{k: int(v) for k, v in step.items()}}
        before, now = int(previous.get(clock, 0)), int(x.get(clock, 0))
        edges = ("pos", "both") if (before, now) == (0, 1) else ("neg", "both") if (before, now) == (1, 0) else ()
        if edges:
            blocks = [f for f in m._seq if getattr(f, "_refkit_edge", "pos") in edges]
            if blocks:
                change = m._change(s, previous, blocks, "seq")
                if spec is not None and spec.style == "sync" and _asserted(spec, previous):
                    _check_reset_overrides(m, change, spec)
                s = m._apply(s, change)
            # A synchronous reset acts on the edge the registers are clocked by, not on every edge of the clock: a block
            # clocked on the falling edge does not see its reset at a rising one.
            if blocks and spec is not None and spec.style == "sync" and _asserted(spec, previous):
                s = _after_reset(m, s)
        if spec is not None and spec.style == "async" and _asserted(spec, x):
            s = _after_reset(m, s)
        for _ in range(4):                                  # latches feeding latches settle in a few passes
            settled = m._apply(s, m._change(s, x, m._latch, "latch")) if m._latch else s
            if settled == s:
                break
            s = settled
        out.append(dict(m._engine_outputs(s, x)))
        previous = x
    return out


__all__ += ["Value", "Bits", "B", "cat", "mux", "clog2", "Wire", "Mem", "Input", "Output", "Instance", "seq", "comb", "latch", "casez", "Module", "run_stepped"]


# ─── following a run: where each output's value came from ───────────────────────────────────────────────────────────────

#: An output or register given its value by the reset rather than by a line of the module.
RESET = "reset"


@dataclass
class TraceCycle:
    """One clock cycle of a traced run. `registers` are the registers and states as this cycle's blocks read them (memories and
    instances left out); `outputs` are read before the edge that ends the cycle. `set_by` gives, for each output, the line of the
    module that gave it this cycle's value: for a @comb output the line that set it in this cycle, for a register output the line
    whose `.next` wrote it in an earlier cycle (RESET when the reset gave it, None for its power-up value). `written` gives, for
    each register assigned in this cycle, the line whose `.next` takes effect at the edge that ends it."""

    cycle: int
    inputs: dict[str, int]
    registers: dict[str, Any]
    outputs: dict[str, int | None]
    set_by: dict[str, int | str | None]
    written: dict[str, int | None]
    reset: bool = False
    written_in: dict[str, int] = field(default_factory=dict)   # register output -> the cycle in which `set_by` ran


@dataclass
class Trace:
    """A traced run (`trace`): one TraceCycle per cycle, and the file the module was written in, when it has one."""

    cycles: list[TraceCycle]
    states: list[str]
    file: str | None = None

    def line(self, number: int | None, source: str | None = None) -> str:
        """The text of a line of the module, from `source` when given (a module compiled from a string), else from its file."""
        if not isinstance(number, int):
            return ""
        if source is not None:
            lines = source.splitlines()
            return lines[number - 1].strip() if 0 < number <= len(lines) else ""
        if self.file is None:
            return ""
        import linecache
        return linecache.getline(self.file, number).strip()

    def explain(self, source: str | None = None, *, outputs: Sequence[str] | None = None, registers: Sequence[str] = (),
                limit: int = 40) -> str:
        """The run as a model reads it: a table of the cycles, then, for each change of an output, the line of the module that made
        it and, for a register, the cycle in which that line ran and the states then. `outputs` limits the outputs whose changes are
        explained; `registers` names inner registers whose changes are explained too (a value a description speaks of that is not
        an output: what `.next` gave it, in which cycle)."""
        def show(v: Any) -> str:
            return "-" if v is None else str(v) if not isinstance(v, int) or v < 16 else f"0x{v:x}"

        def row(values: Mapping[str, Any]) -> str:
            return " ".join(f"{k}={show(v)}" for k, v in values.items()) or "-"

        out = ["cycle | inputs | registers during the cycle | outputs (read before the clock edge that ends the cycle)"]
        for c in self.cycles:
            inner = {k: v for k, v in c.registers.items() if k not in c.outputs}     # a register output shows once, as an output
            out.append(f"{c.cycle} | {row(c.inputs)} | {row(inner)} | {row(c.outputs)}" + (" (reset asserted)" if c.reset else ""))
        changes: list[str] = []
        for previous, c in zip(self.cycles, self.cycles[1:]):
            for name, value in c.outputs.items():
                if (outputs is not None and name not in outputs) or value == previous.outputs.get(name):
                    continue
                head = f"cycle {c.cycle}: {name} {show(previous.outputs.get(name))} -> {show(value)}."
                origin = c.set_by.get(name)
                if origin == RESET:
                    changes.append(f"{head} The reset gave it.")
                elif name in c.written_in and isinstance(origin, int):
                    ran = c.written_in[name]
                    when = self.cycles[ran] if 0 <= ran < len(self.cycles) else None
                    states = row({k: v for k, v in when.registers.items() if k in self.states}) if when is not None else "-"
                    changes.append(f"{head} {name} is a register: line {origin} `{self.line(origin, source)}` wrote it in cycle {ran}"
                                   + (f" (then {states})" if states != "-" else "") + ", and a register shows what is written in a "
                                   "cycle from the next cycle on.")
                elif isinstance(origin, int):
                    changes.append(f"{head} Set in this same cycle by line {origin} `{self.line(origin, source)}` (@comb).")
            for name in registers:
                if name in c.outputs or name not in c.registers or c.registers[name] == previous.registers.get(name):
                    continue
                head = f"cycle {c.cycle}: {name} {show(previous.registers.get(name))} -> {show(c.registers[name])}."
                line = previous.written.get(name)
                if previous.reset or (c.reset and line is None):
                    changes.append(f"{head} The reset gave it.")
                elif isinstance(line, int):
                    states = row({k: v for k, v in previous.registers.items() if k in self.states})
                    changes.append(f"{head} {name} is a register: line {line} `{self.line(line, source)}` wrote it in cycle {previous.cycle}"
                                   + (f" (then {states})" if states != "-" else "") + ".")
        if changes:
            out.append("")
            out.append("Where each change of an output came from:")
            out.extend(changes[:limit])
            if len(changes) > limit:
                out.append(f"... and {len(changes) - limit} more changes")
        return "\n".join(out)


def trace(
    machine: type[Module] | Module, cycles: Sequence[Mapping[str, int]], *, reset: str | None = None, active: int = 1,
    style: str = "sync", widths: Mapping[str, int] | None = None, inputs: Mapping[str, int] | None = None, memory: Any = None,
) -> Trace:
    """Run a Module as `run` does and follow it: the registers in each cycle, and which line of the module gave each output and
    each register its value. A model checks a run it can read cycle by cycle, with the line behind every change, far better than
    it simulates its own code in its head (a register written in a state shows in the cycle AFTER the state is entered)."""
    m = _resolve(machine)
    if not isinstance(m, Module):
        raise RefKitError("trace follows a Module")
    if m.needs_steps:
        raise RefKitError("trace follows a Module run cycle by cycle (no negedge or dual-edge @seq block, no @latch)")
    m._out_widths = widths  # type: ignore[attr-defined]
    if inputs is not None:
        m._in_widths = dict(inputs)  # type: ignore[attr-defined]
    if memory is not None:
        m._memory = memory  # type: ignore[attr-defined]
    spec = ResetSpec(reset, active, style) if reset is not None else None
    resettable = {n for n, d in m.regs.items() if not isinstance(d, Reg) or d.reset is not None}
    s = m.reset_values()
    last: dict[str, tuple[int | str | None, int]] = {}      # register -> (the line that gave it its value, the cycle it ran in)
    rows: list[TraceCycle] = []
    for i, x in enumerate(cycles):
        asserted = _asserted(spec, x)
        if asserted and spec is not None and spec.style == "async":
            last.update({n: (RESET, i) for n in resettable})
        seen: list[dict[str, Any]] = []
        nxt, o = _step(m, s, x, spec, widths, seen)
        nets = dict(getattr(m, "_net_origins", {}))
        written = dict(getattr(m, "_seq_origins", {}))
        set_by: dict[str, int | str | None] = {}
        written_in: dict[str, int] = {}
        for name in o:
            if name in nets:
                set_by[name] = nets[name]
            elif name in m.regs:
                origin, ran = last.get(name, (None, -1))
                set_by[name] = origin
                if isinstance(origin, int):
                    written_in[name] = ran
        registers = {n: v for n, v in seen[0].items() if n in m.regs}
        rows.append(TraceCycle(i, {k: int(v) for k, v in x.items()}, registers, o, set_by, written, asserted, written_in))
        for name, line in written.items():
            if name in m.regs and nxt.get(name) != seen[0].get(name) or name not in last:
                last[name] = (line, i)
        if asserted:
            last.update({n: (RESET, i) for n in resettable})
        s = nxt
    # The lines a trace names are lines of the module's blocks: the file they were compiled from (a subclass may live elsewhere).
    blocks = [*m._comb, *m._seq]
    file = blocks[0].__code__.co_filename if blocks else None
    return Trace(rows, [n for n, d in m.regs.items() if isinstance(d, State)], file)


__all__ += ["trace", "Trace", "TraceCycle", "RESET", "load"]


# ─── exact comparison of two completed references (harness `reference-stability`) ─────────────────────────────────────

def compare_sources(source_a: str, source_b: str, *, max_states: int = 20000, max_input_values: int = 4096) -> dict[str, Any] | None:
    """Compare two RefKit references exactly, by exploring every state the two can reach together on every input.

    Both sources must be references the backend completed (they carry `__refkit__`). Returns None when that is not so, or when
    the comparison cannot be made exactly (step mode, too many input values per cycle); the caller then compares on a stimulus.
    A difference found is always real; `complete` False means the state space was larger than `max_states` and no difference
    was found in the part explored.
    """
    metas = []
    for source in (source_a, source_b):
        ns: dict[str, Any] = {}
        try:
            exec(compile(source, "<reference>", "exec"), ns)  # noqa: S102
        except Exception:  # noqa: BLE001 - not loadable: not comparable here
            return None
        meta = ns.get("__refkit__")
        if not isinstance(meta, dict):
            return None
        metas.append(meta)
    a, b = metas
    if a["clock"] is not None or b["clock"] is not None or a["inputs"] != b["inputs"] or a["widths"] != b["widths"]:
        return None
    combinational = a["combinational"] and b["combinational"]
    try:
        result = compare(a["module"], b["module"], a["inputs"], reset=a["reset"], active=a["active"], style=a["style"],
                         widths=a["widths"], max_states=1 if combinational else max_states,
                         max_input_values=65536 if combinational else max_input_values)
    except RefKitError:
        return None
    except Exception as error:  # noqa: BLE001 - a reading that raises on some input is a difference from one that does not
        return {"equivalent": False, "complete": True, "exact": True, "detail": f"a reading raised while being compared: {error}"}
    if not result.equivalent:
        cycles = len(result.counterexample)
        return {"equivalent": False, "complete": True, "exact": True, "counterexample": result.counterexample,
                "detail": (f"the two readings differ after {cycles} cycle(s) of input {result.counterexample}: "
                           f"one gives {result.outputs_a}, the other {result.outputs_b}")}
    return {"equivalent": True, "complete": result.complete, "exact": result.complete,
            "detail": (f"the two readings agree on every input in every one of the {result.states_seen} reachable states"
                       if result.complete else f"no difference in the first {result.states_seen} reachable states (not all were explored)")}


__all__ += ["compare_sources"]


# ─── stimulus that visits every transition of a RefKit reference ────────────────────────────────────────────────────────

def load(source: str, namespace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a reference's source and return its namespace. It is compiled under a name of its own (`<reference-…>`) whose text is
    registered with linecache, so a traceback, `trace` and an example's mismatch can quote its lines."""
    import hashlib
    import linecache
    name = f"<reference-{hashlib.sha1(source.encode()).hexdigest()[:12]}>"
    linecache.cache[name] = (len(source), None, source.splitlines(True), name)
    ns: dict[str, Any] = {} if namespace is None else namespace
    exec(compile(source, name, "exec"), ns)  # noqa: S102
    return ns


def _meta_of(source: str) -> dict[str, Any] | None:
    try:
        ns = load(source)
    except Exception:  # noqa: BLE001
        return None
    meta = ns.get("__refkit__")
    return meta if isinstance(meta, dict) else None


def transition_walk(
    source: str, *, budget: int = 1500, max_states: int = 4000, max_work: int = 60000,
) -> tuple[list[dict[str, int]], dict[str, Any]] | None:
    """Input cycles that take the reference through every transition it can make (every reachable state, every input value).

    The reachable states are explored from reset; then a walk takes an untaken transition where there is one, otherwise the
    shortest way to a state that still has one (through a reset when that is shorter), until all are taken or `budget` cycles are
    spent. Returns None when the reference is not a clocked RefKit reference whose state graph is small enough to explore
    (step mode, no reset input, too many input values or states). The coverage reached is returned with the cycles."""
    meta = _meta_of(source)
    if meta is None or meta["combinational"] or meta["clock"] is not None or meta["reset"] is None:
        return None
    m = _resolve(meta["module"])
    inputs = {n: w for n, w in meta["inputs"].items() if n != meta["reset"]}
    total = 1
    for w in inputs.values():
        total *= 1 << w
    if total > 256:
        return None
    reset_port, active = meta["reset"], int(meta["active"])
    spec = ResetSpec(reset_port, active, meta["style"])
    widths = meta["widths"]
    m._in_widths = dict(meta["inputs"])  # type: ignore[attr-defined]
    m._out_widths = widths  # type: ignore[attr-defined]
    names = list(inputs)
    alphabet = [{**dict(zip(names, values)), reset_port: 1 - active}
                for values in product(*[range(1 << inputs[n]) for n in names])]
    key = lambda st: tuple(sorted(st.items()))  # noqa: E731
    start = _after_reset(m, m.reset_values())
    states: dict[tuple, dict[str, Any]] = {key(start): start}
    edges: dict[tuple, list[tuple]] = {}
    queue = deque([start])
    work = 0
    while queue:
        st = queue.popleft()
        k = key(st)
        row = []
        for x in alphabet:
            work += 1
            if work > max_work:
                return None
            nxt = _step(m, st, x, spec, widths)[0]
            nk = key(nxt)
            row.append(nk)
            if nk not in states:
                if len(states) >= max_states:
                    return None
                states[nk] = nxt
                queue.append(nxt)
        edges[k] = row
    untaken = {k: set(range(len(alphabet))) for k in edges}
    remaining = sum(len(v) for v in untaken.values())
    total_edges = remaining
    reset_cycle = {**{n: 0 for n in names}, reset_port: active}
    cycles: list[dict[str, int]] = [reset_cycle]
    current = key(start)

    def path_to_untaken(origin: tuple) -> list[int] | None:
        seen = {origin: []}
        q = deque([origin])
        while q:
            k = q.popleft()
            if untaken[k]:
                return seen[k]
            for i, nk in enumerate(edges[k]):
                if nk not in seen:
                    seen[nk] = [*seen[k], i]
                    q.append(nk)
        return None

    while remaining and len(cycles) < budget:
        if untaken[current]:
            i = min(untaken[current])
            untaken[current].discard(i)
            remaining -= 1
            cycles.append(alphabet[i])
            current = edges[current][i]
            continue
        here = path_to_untaken(current)
        from_reset = path_to_untaken(key(start))
        if here is None or (from_reset is not None and len(from_reset) + 1 < len(here)):
            cycles.append(reset_cycle)
            current = key(start)
            here = from_reset
        if here is None:
            break
        for i in here:
            if i in untaken[current]:
                untaken[current].discard(i)
                remaining -= 1
            cycles.append(alphabet[i])
            current = edges[current][i]
    covered = total_edges - remaining
    return cycles, {"states": len(states), "transitions": total_edges, "covered": covered}


__all__ += ["transition_walk"]
