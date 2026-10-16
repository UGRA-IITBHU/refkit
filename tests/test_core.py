"""RefKit: the clocking and reset semantics it enforces, its checks, and the exact comparison of two readings."""

import pytest

from refkit import FSM, ExampleMismatch, Machine, Reg, RefKitError, State, compare, run, run_stepped


class Counter(Machine):
    regs = {"count": Reg(2)}

    def next_state(self, s, x):
        return {"count": s.count + 1} if x.en else {}

    def outputs(self, s, x):
        return {"q": s.count}


def test_register_shows_one_cycle_after_it_is_written_and_wraps():
    got = run(Counter, [{"en": 1}] * 5, widths={"q": 2})
    assert [o["q"] for o in got] == [0, 1, 2, 3, 0]


def test_sync_reset_acts_at_the_edge_async_at_once():
    cycles = [{"en": 1, "rst": 0}, {"en": 1, "rst": 0}, {"en": 1, "rst": 1}, {"en": 1, "rst": 0}]
    sync = run(Counter, cycles, reset="rst", style="sync", widths={"q": 2})
    asyn = run(Counter, cycles, reset="rst", style="async", widths={"q": 2})
    assert [o["q"] for o in sync] == [0, 1, 2, 0]
    assert [o["q"] for o in asyn] == [0, 1, 0, 0]


def test_active_low_reset():
    got = run(Counter, [{"en": 1, "rst_n": 1}, {"en": 1, "rst_n": 0}, {"en": 1, "rst_n": 1}], reset="rst_n", active=0)
    assert [o["q"] for o in got] == [0, 1, 0]


def test_undeclared_register_missing_output_and_bad_state_are_errors():
    class Bad(Machine):
        regs = {"a": Reg(1)}

        def next_state(self, s, x):
            return {"b": 1}

        def outputs(self, s, x):
            return {"y": s.a}

    with pytest.raises(RefKitError, match="not declared"):
        run(Bad, [{"i": 0}, {"i": 0}])
    with pytest.raises(RefKitError, match="did not give"):
        run(Counter, [{"en": 0}], widths={"q": 2, "z": 1})

    class BadState(Machine):
        regs = {"st": State("A", "B")}

        def next_state(self, s, x):
            return {"st": "C"}

        def outputs(self, s, x):
            return {"y": 0}

    with pytest.raises(RefKitError, match="not one of"):
        run(BadState, [{"i": 0}, {"i": 0}])


def test_inputs_and_registers_are_read_only_and_keyword_ports_work():
    class K(Machine):
        regs = {"r": Reg(1)}

        def next_state(self, s, x):
            return {"r": x.in_}

        def outputs(self, s, x):
            return {"out": s.r}

    assert [o["out"] for o in run(K, [{"in": 1}, {"in": 0}, {"in": 0}])] == [0, 1, 0]


class Detect(FSM):
    """Moore: 1 in the cycle after two consecutive 1s."""

    states = ("S0", "S1", "S2")
    transitions = {
        "S0": [(lambda x: x.a, "S1")],
        "S1": [(lambda x: x.a, "S2"), (True, "S0")],
        "S2": [(lambda x: not x.a, "S0")],
    }
    output = {"S0": {"z": 0}, "S1": {"z": 0}, "S2": {"z": 1}}


def test_fsm_table():
    got = run(Detect, [{"a": 1}, {"a": 1}, {"a": 1}, {"a": 0}, {"a": 0}])
    assert [o["z"] for o in got] == [0, 0, 1, 1, 0]


def test_moore_claim_is_checked():
    class Liar(Machine):
        moore = True
        regs = {"r": Reg(1)}

        def outputs(self, s, x):
            return {"z": x.a}

    with pytest.raises(RefKitError, match="Mealy"):
        run(Liar, [{"a": 0}, {"a": 1}])


def test_examples_from_the_description_are_enforced():
    class WithExample(Counter):
        examples = [([{"en": 1}, {"en": 1}, {"en": 0}], [{"q": 0}, {"q": 1}, {"q": 3}])]

    with pytest.raises(ExampleMismatch, match="cycle 2"):
        run(WithExample, [{"en": 0}])


def test_compare_finds_the_shortest_difference_and_proves_equality():
    class Detect2(Detect):
        transitions = {**Detect.transitions, "S2": [(True, "S0")]}   # forgets that a third 1 keeps it high

    same = compare(Detect, Detect, {"a": 1})
    assert same.equivalent and same.complete
    diff = compare(Detect, Detect2, {"a": 1})
    assert not diff.equivalent
    assert len(diff.counterexample) == 4 and diff.counterexample[:3] == [{"a": 1}] * 3
    assert (diff.outputs_a["z"], diff.outputs_b["z"]) == (1, 0)


# ─── the Verilog-shaped layer ───────────────────────────────────────────────────────────────────────────────────────────

from refkit import B, Bits, Mem, Module, Wire, cat, comb, seq  # noqa: E402


def test_bits_follow_verilog_widths():
    a, b = B(200, 8), B(100, 8)
    assert int(a + b) == 44                       # the carry is dropped, as in an 8-bit assignment
    assert int(a.zext(9) + b) == 300              # widen first to keep it
    assert int(~B(0, 4)) == 15
    assert int(B(0b10110100, 8)[7:4]) == 0b1011 and int(B(0b100, 3)[2]) == 1
    assert int(cat(B(1, 1), B(0b0101, 4))) == 0b10101
    assert B(0xF0, 8).signed == -16 and int(B(0xF0, 8).sra(2)) == 0xFC
    assert int(B(5, 3).rep(2)) == 0b101101
    with pytest.raises(RefKitError, match="high bit first"):
        B(5, 8)[0:3]
    with pytest.raises(RefKitError, match="widths are known"):
        cat(B(1, 1), 3)


class Shift(Module):
    sr = Reg(4)
    full = Wire(1)

    @comb
    def view(self):
        self.full = self.sr == 0b1111
        self.out = self.sr[3]
        self.f = self.full

    @seq
    def shift(self):
        self.sr.next = cat(self.sr[2:0], self.d)


def test_module_nonblocking_and_comb_order():
    got = run(Shift, [{"d": 1}] * 5, widths={"out": 1, "f": 1}, inputs={"d": 1})
    assert [o["out"] for o in got] == [0, 0, 0, 0, 1]
    assert [o["f"] for o in got] == [0, 0, 0, 0, 1]


def test_module_swap_is_nonblocking():
    class Swap(Module):
        a = Reg(4, reset=1)
        b = Reg(4, reset=2)

        @seq
        def swap(self):
            self.a.next = self.b
            self.b.next = self.a

    got = run(Swap, [{"i": 0}] * 3, widths={"a": 4, "b": 4})
    assert [(o["a"], o["b"]) for o in got] == [(1, 2), (2, 1), (1, 2)]   # output reg a / b are the registers


def test_module_rules_are_enforced():
    class Blocking(Module):
        r = Reg(1)

        @seq
        def bad(self):
            self.r = 1

    with pytest.raises(RefKitError, match="non-blocking"):
        run(Blocking, [{"i": 0}, {"i": 0}], widths={"r": 1})

    class Unknown(Module):
        @comb
        def bad(self):
            self.y = self.nope

    with pytest.raises(RefKitError, match="no input, register"):
        run(Unknown, [{"i": 0}], widths={"y": 1})


def test_memory_writes_at_the_edge():
    class Ram(Module):
        mem = Mem(4, 8)

        @seq
        def write(self):
            if self.we:
                self.mem.write(self.addr, self.din)

        @comb
        def read(self):
            self.dout = self.mem[self.addr]

    cycles = [{"we": 1, "addr": 2, "din": 7}, {"we": 0, "addr": 2, "din": 0}, {"we": 1, "addr": 2, "din": 9}, {"we": 0, "addr": 2, "din": 0}]
    got = run(Ram, cycles, widths={"dout": 8}, inputs={"we": 1, "addr": 2, "din": 8})
    assert [o["dout"] for o in got] == [0, 7, 7, 9]


def test_module_and_machine_readings_compare_exactly():
    class CounterModule(Module):
        count = Reg(2)

        @seq
        def tick(self):
            if self.en:
                self.count.next = self.count + 1

        @comb
        def show(self):
            self.q = self.count

    assert compare(Counter, CounterModule, {"en": 1}, widths={"q": 2}).equivalent


# ─── the guide the models are taught from must itself be correct ────────────────────────────────────────────────────────

import random  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

GUIDE = Path(__file__).resolve().parents[1] / "src" / "refkit" / "guide.md"


def _guide_modules():
    for block in re.findall(r"```python\n(.*?)```", GUIDE.read_text(), re.S):
        if "class Ref(Module)" in block and "..." not in block:
            yield block


def _ports(source):
    """Inputs read and outputs set by a guide example, found by running it once on a probe (names only, widths 8)."""
    inputs = sorted(set(re.findall(r"self\.([a-z_]\w*)", source)) - set(re.findall(r"^\s+(\w+) = (?:Reg|State|Mem|Wire)\(", source, re.M)))
    outputs = sorted(set(re.findall(r"self\.(\w+) = ", source)))
    return inputs, outputs


def test_every_module_example_of_the_guide_runs():
    blocks = list(_guide_modules())
    assert len(blocks) >= 10
    for source in blocks:
        ns: dict = {}
        exec("from refkit import *\n" + source, ns)  # noqa: S102
        ref = ns["Ref"]
        decls = set(ref._decls)
        _, set_names = _ports(source)
        outputs = {n: 8 for n in set_names if n not in decls}
        read = set(re.findall(r"self\.([a-z_]\w*)", source)) - decls - set(outputs) - {"clk"}
        inputs = {n: 1 if n in {"en", "go", "x", "rx", "wr", "rd", "load", "clear", "valid", "din", "d"} and n != "d" else 8 for n in read}
        rng = random.Random(0)
        cycles = [{n: rng.randrange(1 << w) for n, w in inputs.items()} for _ in range(40)]
        if ref.needs_steps:
            steps = [{**c, "clk": i % 2} for i, c in enumerate(cycles)]
            got = run_stepped(ref, steps, clock="clk", widths=outputs, inputs=inputs)
        else:
            got = run(ref, cycles, widths=outputs, inputs=inputs)
        assert len(got) == 40, source


# ─── edges, latches, case ───────────────────────────────────────────────────────────────────────────────────────────────

from refkit import casez, latch, run_stepped  # noqa: E402


def _clocked(steps_data, key="d"):
    """Two steps per cycle: clock low with the cycle's data (read here, before the edge), then the rising edge, which takes it."""
    out = []
    for d in steps_data:
        out += [{"clk": 0, key: d}, {"clk": 1, key: d}]
    return out


def test_negedge_and_dual_edge_flip_flops():
    class Neg(Module):
        q = Reg(1)

        @seq(edge="neg")
        def f(self):
            self.q.next = self.d

    class Both(Module):
        q = Reg(1)

        @seq(edge="both")
        def f(self):
            self.q.next = self.d

    steps = [{"clk": 0, "d": 1}, {"clk": 1, "d": 1}, {"clk": 0, "d": 0}, {"clk": 1, "d": 0}, {"clk": 0, "d": 1}]
    neg = [o["q"] for o in run_stepped(Neg, steps, clock="clk", widths={"q": 1}, inputs={"d": 1})]
    both = [o["q"] for o in run_stepped(Both, steps, clock="clk", widths={"q": 1}, inputs={"d": 1})]
    assert neg == [0, 0, 1, 1, 0]        # falls at step 2 (d of step 1 = 1) and step 4 (d of step 3 = 0)
    assert both == [0, 1, 1, 0, 0]       # every change of the clock takes the d of the step before
    with pytest.raises(RefKitError, match="step by step"):
        run(Neg, [{"d": 1}, {"d": 0}], widths={"q": 1})


def test_latch_is_transparent_while_enabled_and_holds_otherwise():
    class L(Module):
        q = Reg(1)

        @latch
        def hold(self):
            if self.en:
                self.q.next = self.d

    steps = [{"clk": 0, "en": 1, "d": 1}, {"clk": 0, "en": 0, "d": 0}, {"clk": 0, "en": 1, "d": 0}]
    assert [o["q"] for o in run_stepped(L, steps, clock="clk", widths={"q": 1}, inputs={"en": 1, "d": 1})] == [1, 1, 0]


def test_posedge_in_step_mode_matches_cycle_mode():
    data = [1, 0, 1, 1, 0, 1, 1, 1]
    stepped = run_stepped(Shift, _clocked(data), clock="clk", widths={"out": 1, "f": 1}, inputs={"d": 1})
    cycle = run(Shift, [{"d": d} for d in data], widths={"out": 1, "f": 1}, inputs={"d": 1})
    assert stepped[0::2] == cycle          # the low step of each cycle reads what cycle mode reads before that edge


def test_match_and_casez():
    class Alu(Module):
        @comb
        def f(self):
            match int(self.op):
                case 0:
                    self.y = self.a + self.b
                case 1:
                    self.y = self.a - self.b
                case _:
                    self.y = 0

    got = run(Alu, [{"op": 0, "a": 3, "b": 2}, {"op": 1, "a": 3, "b": 2}, {"op": 3, "a": 3, "b": 2}], widths={"y": 4}, inputs={"op": 2, "a": 4, "b": 4})
    assert [o["y"] for o in got] == [5, 1, 0]
    assert casez(B(0b110, 3), "1?0") and casez(B(0b100, 3), "1?0") and not casez(B(0b101, 3), "1?0")


# ─── Verilog's expression widths, one to one ────────────────────────────────────────────────────────────────────────────

from refkit import Input, Instance, Output, clog2, mux  # noqa: E402


def _comb_once(body, outputs, inputs, values):
    ns: dict = {}
    exec("from refkit import *\nclass Ref(Module):\n    @comb\n    def f(self):\n" + "".join(f"        {line}\n" for line in body), ns)  # noqa: S102
    return run(ns["Ref"], [values], widths=outputs, inputs=inputs)[0]


def test_assignment_width_keeps_the_carry_like_verilog():
    out = _comb_once(["self.s9 = self.a + self.b", "self.s8 = self.a + self.b"], {"s9": 9, "s8": 8}, {"a": 8, "b": 8}, {"a": 200, "b": 100})
    assert out == {"s9": 300, "s8": 44}                 # assign s9 = a + b;  keeps the carry; an 8-bit destination drops it


def test_unsized_constants_are_32_bits_like_verilog():
    out = _comb_once(["self.wrap = (self.c + 1) == 0", "self.big = (self.c + 1) == 16", "self.inv = ~self.c == 0"],
                     {"wrap": 1, "big": 1, "inv": 1}, {"c": 4}, {"c": 15})
    assert out == {"wrap": 0, "big": 1, "inv": 0}       # c + 1 is 32 bits wide; ~c compared with 0 is extended first (a classic)


def test_signed_operations():
    a, b = B(-7, 8), B(2, 8)
    assert a.slt(b) and not a < b                         # signed vs unsigned comparison
    assert a.sdiv(b).signed == -3 and a.smod(b).signed == -1
    assert a.smul(b).signed == -14 and a.smul(b).width == 16
    assert int(B(0b1010, 4).part(1, 2)) == 0b01 and int(B(0b1010, 4).part_down(3, 2)) == 0b10
    assert clog2(1) == 0 and clog2(8) == 3 and clog2(9) == 4
    assert int(mux(True, B(1, 1), B(0xF, 4))) == 1 and mux(True, B(1, 1), B(0xF, 4)).width == 4


def test_register_without_reset_and_power_up_value():
    class R(Module):
        kept = Reg(4, reset=None)
        cnt = Reg(4, init=9)

        @seq
        def go(self):
            self.kept.next = self.kept + 1
            self.cnt.next = self.cnt + 1

    got = run(R, [{"rst": 0}, {"rst": 0}, {"rst": 1}, {"rst": 0}], reset="rst", widths={"kept": 4, "cnt": 4})
    assert [o["kept"] for o in got] == [0, 1, 2, 3]       # the reset does not touch it
    assert [o["cnt"] for o in got] == [9, 10, 11, 0]      # power-up 9, reset value 0


def test_bit_and_slice_nonblocking_assignments_merge():
    class R(Module):
        r = Reg(8)

        @seq
        def go(self):
            self.r.next_bit(0, 1)
            self.r.next_bit(7, 1)
            self.r.next_slice(4, 2, 0b111)

    assert run(R, [{"i": 0}, {"i": 0}], widths={"r": 8})[1]["r"] == 0b10011101


def test_parameters_functions_and_instances():
    class Child(Module):
        en = Input(1)
        q = Output(4)
        cnt = Reg(4)

        @seq
        def tick(self):
            if self.en:
                self.cnt.next = self.cnt + 1

        @comb
        def show(self):
            self.q = self.cnt

    class Top(Module):
        WIDTH = 4
        u1 = Instance(Child)
        u2 = Instance(Child)

        def double(self, v):                              # a Verilog function
            return v + v

        @comb
        def wire_up(self):
            a = self.u1(en=self.go)
            b = self.u2(en=B(1, 1))
            self.y = self.double(a.q)
            self.z = b.q
            self.w = B(self.WIDTH, 8)

    got = run(Top, [{"go": 1, "rst": 0}, {"go": 0, "rst": 0}, {"go": 1, "rst": 0}, {"go": 1, "rst": 1}, {"go": 0, "rst": 0}],
              reset="rst", widths={"y": 8, "z": 4, "w": 8}, inputs={"go": 1, "rst": 1})
    assert [o["y"] for o in got] == [0, 2, 2, 4, 0]
    assert [o["z"] for o in got] == [0, 1, 2, 3, 0]
    assert got[0]["w"] == 4


# ─── exact comparison of completed references ───────────────────────────────────────────────────────────────────────────

from refkit import compare_sources  # noqa: E402


def _completed(body: str, combinational: bool = False) -> str:
    """A reference as the backend completes it (pipeline/refkit.ts completeRefkit)."""
    meta = ('__refkit__ = {"module": Ref, "combinational": %s, "clock": None, "reset": "rst", "active": 1, "style": "sync", '
            '"widths": {"q": 2}, "inputs": {"en": 1, "rst": 1}}\n' % ("True" if combinational else "False"))
    return "from refkit import *\n" + body + "\n" + meta


def test_compare_sources_is_exact_for_refkit_readings():
    up = "class Ref(Module):\n    c = Reg(2)\n    @seq\n    def s(self):\n        if self.en:\n            self.c.next = self.c + 1\n    @comb\n    def o(self):\n        self.q = self.c\n"
    sat = up.replace("self.c.next = self.c + 1", "self.c.next = self.c + 1 if self.c != 3 else self.c")
    same = compare_sources(_completed(up), _completed(up))
    assert same["equivalent"] and same["exact"]
    diff = compare_sources(_completed(up), _completed(sat))
    assert not diff["equivalent"] and len(diff["counterexample"]) == 5      # 4 edges to wrap, seen in the 5th cycle
    assert compare_sources("def ref(cycles): return []", _completed(up)) is None


def test_transition_walk_takes_every_transition():
    from refkit import transition_walk
    src = _completed("class Ref(Module):\n    st = State('A', 'B', 'C')\n    @seq\n    def s(self):\n"
                     "        if self.st == 'A' and self.en:\n            self.st.next = 'B'\n"
                     "        elif self.st == 'B':\n            self.st.next = 'C' if self.en else 'A'\n"
                     "        elif self.st == 'C' and not self.en:\n            self.st.next = 'A'\n"
                     "    @comb\n    def o(self):\n        self.q = 2 if self.st == 'C' else 0\n")
    cycles, cov = transition_walk(src)
    assert cov == {"states": 3, "transitions": 6, "covered": 6}
    assert cycles[0]["rst"] == 1 and all(set(c) == {"en", "rst"} for c in cycles)


def test_an_output_register_assigned_to_itself_in_comb_is_a_no_op():
    class Echo(Module):
        q = Reg(4)

        @seq
        def count(self):
            self.q.next = self.q + 1

        @comb
        def out(self):
            self.q = self.q

    assert [o["q"] for o in run(Echo, [{"en": 0}] * 3, widths={"q": 4})] == [0, 1, 2]

    class Clash(Module):
        q = Reg(4)

        @comb
        def out(self):
            self.q = self.q + 1

    with pytest.raises(RefKitError, match="is a register"):
        run(Clash, [{"en": 0}], widths={"q": 4})


def test_a_synchronous_reset_acts_only_on_the_edge_the_register_is_clocked_by():
    class Neg(Module):
        q = Reg(8, reset=0x34)

        @seq(edge="neg")
        def f(self):
            self.q.next = self.d

    steps = [{"clk": 1, "d": 0x7F, "reset": 0}, {"clk": 0, "d": 0x7F, "reset": 0},
             {"clk": 0, "d": 0, "reset": 1}, {"clk": 1, "d": 0, "reset": 1}, {"clk": 0, "d": 0, "reset": 1}]
    q = [o["q"] for o in run_stepped(Neg, steps, clock="clk", reset="reset", active=1, style="sync",
                                     widths={"q": 8}, inputs={"d": 8, "reset": 1})]
    assert q == [0x34, 0x7F, 0x7F, 0x7F, 0x34]   # the rising edge after reset rose leaves q alone; the falling one resets it
