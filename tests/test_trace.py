"""trace / Trace.explain: a run a model can read, with the line of the module behind every change of an output."""

import inspect

import pytest

from refkit import RESET, ExampleMismatch, Module, Reg, RefKitError, State, comb, load, run, seq, trace


class Late(Module):
    """psel is a register written while in SETUP, so it shows in the cycle after SETUP is entered (a model's usual slip)."""
    st = State("IDLE", "SETUP", "ACCESS", reset="IDLE")
    psel = Reg(1)

    @seq
    def fsm(self):
        self.psel.next = 0
        if self.st == "IDLE":
            if self.go:
                self.st.next = "SETUP"
        elif self.st == "SETUP":
            self.psel.next = 1
            self.st.next = "ACCESS"
        else:
            self.psel.next = 1
            self.st.next = "IDLE"


class OnTime(Module):
    """psel read from the state in @comb: high in the very cycles the state is SETUP or ACCESS."""
    st = State("IDLE", "SETUP", "ACCESS", reset="IDLE")

    @seq
    def fsm(self):
        if self.st == "IDLE":
            if self.go:
                self.st.next = "SETUP"
        elif self.st == "SETUP":
            self.st.next = "ACCESS"
        else:
            self.st.next = "IDLE"

    @comb
    def out(self):
        self.psel = 1 if self.st != "IDLE" else 0


CYCLES = [{"rst": 1, "go": 0}, {"rst": 0, "go": 1}, {"rst": 0, "go": 0}, {"rst": 0, "go": 0}, {"rst": 0, "go": 0}]


def test_register_output_names_the_line_and_the_cycle_it_ran_in():
    t = trace(Late, CYCLES, reset="rst", widths={"psel": 1})
    assert [c.registers["st"] for c in t.cycles] == ["IDLE", "IDLE", "SETUP", "ACCESS", "IDLE"]
    assert [c.outputs["psel"] for c in t.cycles] == [0, 0, 0, 1, 1]
    line = inspect.getsourcelines(Late)[1] + [l.strip() for l in inspect.getsourcelines(Late)[0]].index("self.psel.next = 1")
    assert t.cycles[3].set_by["psel"] == line and t.cycles[3].written_in["psel"] == 2
    text = t.explain()
    assert f"cycle 3: psel 0 -> 1. psel is a register: line {line} `self.psel.next = 1` wrote it in cycle 2 (then st=SETUP)" in text
    assert "2 | rst=0 go=0 | st=SETUP | psel=0" in text


def test_comb_output_names_the_line_of_this_cycle():
    t = trace(OnTime, CYCLES, reset="rst", widths={"psel": 1})
    assert [c.outputs["psel"] for c in t.cycles] == [0, 0, 1, 1, 0]
    assert "cycle 2: psel 0 -> 1. Set in this same cycle by line" in t.explain()
    assert "`self.psel = 1 if self.st != \"IDLE\" else 0` (@comb)" in t.explain()


def test_reset_and_source_given_as_text():
    source = (
        "from refkit import *\n"
        "class C(Module):\n"
        "    q = Reg(4)\n"
        "    @seq\n"
        "    def count(self):\n"
        "        self.q.next = self.q + 1\n"
    )
    namespace: dict = {}
    exec(compile(source, "<reference>", "exec"), namespace)
    cycles = [{"rst": 0}, {"rst": 0}, {"rst": 1}, {"rst": 0}, {"rst": 0}]
    t = trace(namespace["C"], cycles, reset="rst", style="async", widths={"q": 4})
    assert [c.outputs["q"] for c in t.cycles] == [0, 1, 0, 0, 1]   # held in reset at the edge that ends cycle 2
    assert t.cycles[2].set_by["q"] == RESET and t.cycles[2].reset
    text = t.explain(source)
    assert "cycle 1: q 0 -> 1. q is a register: line 6 `self.q.next = self.q + 1` wrote it in cycle 0" in text
    assert "cycle 2: q 1 -> 0. The reset gave it." in text
    assert "(reset asserted)" in text
    assert "cycle 4: q 0 -> 1. q is a register: line 6 `self.q.next = self.q + 1` wrote it in cycle 3" in text


def test_outputs_limit_the_changes_explained_and_step_modules_are_refused():
    t = trace(Late, CYCLES, reset="rst", widths={"psel": 1})
    assert "psel" not in t.explain(outputs=["other"]).split("Where")[-1] or "Where" not in t.explain(outputs=["other"])

    class Neg(Module):
        q = Reg(1)

        @seq(edge="neg")
        def flip(self):
            self.q.next = ~self.q

    with pytest.raises(RefKitError, match="cycle by cycle"):
        trace(Neg, [{}], widths={"q": 1})


def test_inner_registers_named_are_explained_too():
    class Shifty(Module):
        tmp = Reg(8, reset=1)
        cnt = Reg(3)

        @comb
        def out(self):
            self.serial_out = self.tmp[7]

        @seq
        def step(self):
            self.tmp.next = self.tmp.shift_in_low(0)
            self.cnt.next = self.cnt + 1
            if self.cnt == 7:
                self.tmp.next = self.tmp + 1

    t = trace(Shifty, [{"rst": 1}] + [{"rst": 0}] * 10, reset="rst", widths={"serial_out": 1})
    lines = [l.strip() for l in inspect.getsourcelines(Shifty)[0]]
    shift = inspect.getsourcelines(Shifty)[1] + lines.index("self.tmp.next = self.tmp.shift_in_low(0)")
    text = t.explain(registers=["tmp"])
    assert f"cycle 2: tmp 1 -> 2. tmp is a register: line {shift} `self.tmp.next = self.tmp.shift_in_low(0)` wrote it in cycle 1." in text
    assert ": tmp " not in t.explain()


SHIFTY = """from refkit import *

class Shifty(Module):
    tmp = Reg(8, reset=1)
    cnt = Reg(3)

    @comb
    def out(self):
        self.serial_out = self.tmp[7]

    @seq
    def step(self):
        self.tmp.next = self.tmp.shift_in_low(0)
        self.cnt.next = self.cnt + 1
        if self.cnt == 7:
            self.tmp.next = self.tmp + 1
"""


def test_an_example_can_give_an_inner_register_and_its_mismatch_names_the_line():
    ns = load(SHIFTY)
    cycles = [{"rst": 1}] + [{"rst": 0}] * 10
    cls = type("WithExample", (ns["Shifty"],), {"examples": [(cycles, [{}] * 9 + [{"tmp": 2}])]})
    with pytest.raises(ExampleMismatch) as e:
        run(cls, cycles, reset="rst", widths={"serial_out": 1})
    assert "example 1, cycle 9: the description gives tmp = 2, the machine gives 129" in str(e.value)
    assert "tmp is a register: line 16 `self.tmp.next = self.tmp + 1` wrote it in cycle 8 (registers then: tmp=0x80 cnt=7)" in str(e.value)


def test_an_example_output_mismatch_names_the_comb_line_and_unknown_names_are_errors():
    ns = load(SHIFTY)
    cycles = [{"rst": 1}] + [{"rst": 0}] * 3
    cls = type("Out", (ns["Shifty"],), {"examples": [(cycles, [{}, {"serial_out": 1}])]})
    with pytest.raises(ExampleMismatch, match="line 9 `self.serial_out = self.tmp\\[7\\]` set it in this cycle"):
        run(cls, cycles, reset="rst", widths={"serial_out": 1})
    bad = type("Bad", (ns["Shifty"],), {"examples": [(cycles, [{"nothing": 1}])]})
    with pytest.raises(RefKitError, match="neither outputs nor registers"):
        run(bad, cycles, reset="rst", widths={"serial_out": 1})
