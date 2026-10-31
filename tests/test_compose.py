"""RefKit compose: completed references of blocks run together as a plan wires them."""

import pytest

from refkit import RefKitError
from refkit.compose import run_composed


def ref(body: str, inputs: dict, widths: dict, reset: str | None = "rst") -> str:
    meta = (f'__refkit__ = {{"module": Ref, "combinational": False, "clock": None, "reset": {reset!r}, "active": 1, '
            f'"style": "sync", "widths": {widths!r}, "inputs": {inputs!r}}}')
    return f"from refkit import *\n\nclass Ref(Module):\n{body}\n\n{meta}\n"


EDGE_REGISTERED = ref("""    prev = Reg(1, reset=0)
    pos = Reg(1, reset=0)

    @seq
    def edge(self):
        self.prev.next = self.d
        self.pos.next = self.d & ~self.prev

    @comb
    def out(self):
        self.y = self.pos
""", {"rst": 1, "d": 1}, {"y": 1})

EDGE_COMBINATIONAL = ref("""    prev = Reg(1, reset=0)

    @seq
    def remember(self):
        self.prev.next = self.d

    @comb
    def out(self):
        self.y = self.d & ~self.prev
""", {"rst": 1, "d": 1}, {"y": 1})

REGISTER = ref("""    q = Reg(1, reset=0)

    @seq
    def take(self):
        self.q.next = self.a

    @comb
    def out(self):
        self.y = self.q
""", {"rst": 1, "a": 1}, {"y": 1})

PASS = ref("""    @comb
    def out(self):
        self.y = self.a
""", {"a": 1}, {"y": 1}, reset=None)

CYCLES = [{"rst": 1, "d": 0}, {"rst": 0, "d": 0}, {"rst": 0, "d": 1}, {"rst": 0, "d": 1}, {"rst": 0, "d": 0}, {"rst": 0, "d": 0}]


def test_two_registers_in_a_row_are_a_cycle_later_than_one():
    one = run_composed({"e": EDGE_COMBINATIONAL, "p": PASS}, [("top.rst", "e.rst"), ("top.d", "e.d"), ("e.y", "p.a"), ("p.y", "top.y")],
                       CYCLES, outputs={"y": 1})
    two = run_composed({"e": EDGE_REGISTERED, "r": REGISTER},
                       [("top.rst", "e.rst"), ("top.rst", "r.rst"), ("top.d", "e.d"), ("e.y", "r.a"), ("r.y", "top.y")], CYCLES, outputs={"y": 1})
    assert [o["y"] for o in one] == [0, 0, 1, 0, 0, 0]
    assert [o["y"] for o in two] == [0, 0, 0, 0, 1, 0]


ADDER = ref("""    @comb
    def add(self):
        self.s = self.a + 1
""", {"a": 3}, {"s": 3}, reset=None)

HOLD = ref("""    q = Reg(3, reset=0)

    @seq
    def take(self):
        self.q.next = self.n

    @comb
    def out(self):
        self.y = self.q
""", {"rst": 1, "n": 3}, {"y": 3})


def test_a_loop_through_a_register_in_another_part_settles():
    got = run_composed({"add": ADDER, "hold": HOLD}, [("top.rst", "hold.rst"), ("hold.y", "add.a"), ("add.s", "hold.n"), ("hold.y", "top.q")],
                       [{"rst": 1}] + [{"rst": 0}] * 4, outputs={"q": 3})
    assert [o["q"] for o in got] == [0, 0, 1, 2, 3]


INVERT = ref("""    @comb
    def out(self):
        self.y = ~self.a
""", {"a": 1}, {"y": 1}, reset=None)


def test_a_combinational_loop_that_never_settles_is_an_error():
    with pytest.raises(RefKitError, match="never settle"):
        run_composed({"i": INVERT}, [("i.y", "i.a"), ("i.y", "top.y")], [{}], outputs={"y": 1})


SPLIT = ref("""    @comb
    def out(self):
        self.lo = self.v
        self.hi = self.w
""", {"v": 2, "w": 2}, {"lo": 2, "hi": 2}, reset=None)


def test_slices_on_both_sides():
    got = run_composed({"s": SPLIT}, [("top.x[1:0]", "s.v"), ("top.x[3:2]", "s.w"), ("s.lo", "top.y[3:2]"), ("s.hi", "top.y[1:0]")],
                       [{"x": 0b1101}], outputs={"y": 4})
    assert got[0]["y"] == 0b0111
