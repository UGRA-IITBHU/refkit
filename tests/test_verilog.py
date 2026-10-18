"""RefKit to Verilog, and the SymbiYosys proof of an RTL module against a RefKit reference."""

import shutil
import subprocess
from pathlib import Path

import pytest

from refkit.verilog import Unsupported, _tool, prove, translate

COUNTER = '''from refkit import *
class Ref(Module):
    cnt = Reg(4)
    @seq
    def s(self):
        if self.en:
            self.cnt.next = self.cnt + 1
    @comb
    def o(self):
        self.q = self.cnt
        self.tc = self.cnt == 15
__refkit__ = {"module": Ref, "combinational": False, "clock": None, "reset": "reset", "active": 1, "style": "sync",
  "widths": {"q": 4, "tc": 1}, "inputs": {"en": 1, "reset": 1}}
'''
GOOD = ("module TopModule(input clk, input reset, input en, output reg [3:0] q, output tc);\n"
        " always @(posedge clk) if (reset) q <= 0; else if (en) q <= q + 1;\n assign tc = q == 4'd15;\nendmodule\n")

FSM = '''from refkit import *
class Ref(Module):
    st = State("IDLE", "RUN", "DONE")
    cnt = Reg(4)
    q = Reg(8)
    @comb
    def logic(self):
        t = self.a + self.b
        self.s9 = t
        self.done = self.st == "DONE"
        self.top = (t >> 4)[3:0]
    @seq
    def fsm(self):
        match self.st.value:
            case "IDLE":
                if self.go:
                    self.st.next = "RUN"
                    self.cnt.next = 0
            case "RUN":
                self.cnt.next = self.cnt + 1
                if self.cnt == 9:
                    self.st.next = "DONE"
            case _:
                self.st.next = "IDLE"
        for i in range(2):
            self.q.next_bit(i, self.a[i])
__refkit__ = {"module": Ref, "combinational": False, "clock": None, "reset": "rst", "active": 1, "style": "sync",
  "widths": {"s9": 9, "done": 1, "top": 4, "q": 8}, "inputs": {"a": 8, "b": 8, "go": 1, "rst": 1}}
'''

have_tools = _tool("sby") is not None and _tool("yosys") is not None


def test_translation_is_verilog_yosys_accepts(tmp_path):
    if _tool("yosys") is None:
        pytest.skip("yosys not available")
    for source in (COUNTER, FSM):
        t = translate(source)
        (tmp_path / "r.sv").write_text(t.verilog)
        done = subprocess.run([_tool("yosys"), "-q", "-p", "read_verilog -sv r.sv; prep -top refkit_ref"], cwd=tmp_path,
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr + t.verilog
    assert "9'((a + b))" in translate(FSM).verilog            # the carry kept, as RefKit keeps it


def test_unsupported_constructs_are_refused():
    dual = COUNTER.replace("@seq\n", '@seq(edge="both")\n')
    with pytest.raises(Unsupported):
        translate(dual)
    mixed = COUNTER.replace("    @comb\n", '    @seq(edge="neg")\n    def n(self):\n        self.cnt.next = self.cnt\n    @comb\n')
    with pytest.raises(Unsupported):
        translate(mixed)


def test_a_falling_edge_module_translates_and_is_not_proven():
    stepped = COUNTER.replace("@seq\n", '@seq(edge="neg")\n').replace('"clock": None', '"clock": "clk"')
    t = translate(stepped)
    assert "always @(negedge clk)" in t.verilog and t.edge == "negedge"
    assert prove(stepped, GOOD, "TopModule", "clk").status == "unsupported"


# ─── a translation behaves like RefKit, cycle by cycle (co-simulation with Icarus Verilog) ─────────────────────────────

import random  # noqa: E402

from refkit import run  # noqa: E402


def _cosim(source: str, cycles: int = 80, seed: int = 1) -> None:
    iverilog, vvp = _tool("iverilog"), _tool("vvp")
    if iverilog is None or vvp is None:
        pytest.skip("iverilog not available")
    ns: dict = {}
    exec(compile(source, "<reference>", "exec"), ns)  # noqa: S102
    meta = ns["__refkit__"]
    inputs, widths = meta["inputs"], meta["widths"]
    rng = random.Random(seed)
    vectors = [{n: (1 if n == meta["reset"] and i == 0 else rng.randrange(1 << w)) for n, w in inputs.items()} for i in range(cycles)]
    for v in vectors[1:]:
        if meta["reset"] in v and rng.random() < 0.9:
            v[meta["reset"]] = 0
    expected = run(meta["module"], vectors, reset=meta["reset"], widths=widths, inputs=inputs)
    translation = translate(source, "dut_ref")
    decls = "".join(f"reg [{w - 1}:0] {n}; " for n, w in inputs.items()) + "".join(f"wire [{w - 1}:0] {n}; " for n, w in widths.items())
    wires = ", ".join([".clk(clk)"] + [f".{n}({n})" for n in [*inputs, *widths]])
    steps = "".join(
        "".join(f" {n} = {w}'d{v[n]};" for n, w in inputs.items())
        + f' #1; $display("{" ".join("%0d" for _ in widths)}", {", ".join(widths)}); clk = 1; #1; clk = 0; #1;\n'
        for v in vectors)
    bench = f"module tb; reg clk = 0; {decls}\n dut_ref dut_under_test({wires});\n initial begin\n{steps} $finish; end\nendmodule\n"
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "d.v").write_text(translation.verilog)
        (Path(tmp) / "tb.v").write_text(bench)
        built = subprocess.run([iverilog, "-g2012", "-o", "sim", "d.v", "tb.v"], cwd=tmp, capture_output=True, text=True)
        assert built.returncode == 0, built.stderr + translation.verilog
        out = subprocess.run([vvp, "sim"], cwd=tmp, capture_output=True, text=True).stdout.strip().splitlines()
    got = [dict(zip(widths, map(int, line.split()))) for line in out if line and line[0].isdigit()]
    assert got == [{n: (o[n] or 0) for n in widths} for o in expected], translation.verilog


CONSTRUCTS = '''from refkit import *
class Ref(Module):
    acc = Reg(8)
    r = Reg(16)
    @seq
    def s(self):
        t = 0
        for d in [1, 2, 3]:
            if d == 2:
                continue
            t += self.a[d]
        if self.mode in (1, 3):
            n = 5
        else:
            n = self.b
        self.acc.next = self.acc + t + n
        self.r.next = self.r.set_slice(7, 4, self.a[3:0]).set_bit(0, self.en)
    @comb
    def o(self):
        self.y = self.acc
        self.inrange = 2 <= self.acc < 9
        self.z = self.r
        self.odd = self.mode not in (0, 2)
__refkit__ = {"module": Ref, "combinational": False, "clock": None, "reset": "rst", "active": 1, "style": "sync",
  "widths": {"y": 8, "inrange": 1, "z": 16, "odd": 1}, "inputs": {"a": 8, "b": 8, "mode": 2, "en": 1, "rst": 1}}
'''


def test_translated_constructs_behave_like_refkit():
    _cosim(CONSTRUCTS)


LONG = '''from refkit import *
class Ref(Module):
    q = Reg(32)
    @seq
    def s(self):
        nxt = 0
        for i in range(32):
            cell = self.q[i]
            n = self.q[(i + 1) % 32] + self.q[(i + 31) % 32]
            if cell == 1:
                if n == 1 or n == 2:
                    nxt |= (1 << i)
            else:
                if n == 2:
                    nxt |= (1 << i)
        if self.load:
            self.q.next = self.d
        else:
            self.q.next = nxt
    @comb
    def o(self):
        self.out = self.q
__refkit__ = {"module": Ref, "combinational": False, "clock": None, "reset": "rst", "active": 1, "style": "sync",
  "widths": {"out": 32}, "inputs": {"d": 32, "load": 1, "rst": 1}}
'''


def test_a_value_joined_over_many_branches_does_not_blow_up_and_matches():
    t = translate(LONG)
    assert len(t.verilog) < 60000                     # held in wires, not copied into itself at every join
    _cosim(LONG)


@pytest.mark.skipif(not have_tools, reason="SymbiYosys not available")
def test_proof_proves_a_right_rtl_and_refutes_a_wrong_one(tmp_path):
    assert prove(COUNTER, GOOD, "TopModule", "clk", workdir=tmp_path / "good").status == "proven"
    wrong = prove(COUNTER, GOOD.replace("4'd15", "4'd14"), "TopModule", "clk", workdir=tmp_path / "bad")
    assert wrong.status == "refuted"
    assert wrong.counterexample[0]["reset"] == 1 and len(wrong.counterexample) >= 15   # it takes 14 increments to differ


@pytest.mark.skipif(not have_tools, reason="SymbiYosys not available")
def test_proof_of_a_state_machine_with_carry_and_bit_assignments(tmp_path):
    rtl = """module TopModule(input clk, input rst, input [7:0] a, input [7:0] b, input go,
      output [8:0] s9, output done, output [3:0] top, output reg [7:0] q);
      localparam IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2;
      reg [1:0] st; reg [3:0] cnt;
      assign s9 = a + b;
      assign done = st == DONE;
      wire [7:0] t = a + b;
      assign top = t[7:4];
      always @(posedge clk) begin
        if (rst) begin st <= IDLE; cnt <= 0; q <= 0; end
        else begin
          case (st)
            IDLE: if (go) begin st <= RUN; cnt <= 0; end
            RUN: begin cnt <= cnt + 1; if (cnt == 9) st <= DONE; end
            default: st <= IDLE;
          endcase
          q[1:0] <= a[1:0];
        end
      end
    endmodule
    """
    assert prove(FSM, rtl, "TopModule", "clk", workdir=tmp_path / "fsm").status == "proven"
    off_by_one = rtl.replace("if (cnt == 9)", "if (cnt == 8)")
    assert prove(FSM, off_by_one, "TopModule", "clk", workdir=tmp_path / "fsm2").status == "refuted"


@pytest.mark.skipif(not have_tools, reason="SymbiYosys not available")
def test_proof_with_an_asynchronous_reset(tmp_path):
    source = COUNTER.replace('"reset": "reset", "active": 1, "style": "sync"', '"reset": "areset", "active": 1, "style": "async"') \
        .replace('"inputs": {"en": 1, "reset": 1}', '"inputs": {"en": 1, "areset": 1}')
    rtl = ("module TopModule(input clk, input areset, input en, output reg [3:0] q, output tc);\n"
           " always @(posedge clk or posedge areset) if (areset) q <= 0; else if (en) q <= q + 1;\n"
           " assign tc = q == 4'd15;\nendmodule\n")
    assert prove(source, rtl, "TopModule", "clk", workdir=tmp_path / "a").status == "proven"
    sync_rtl = rtl.replace(" or posedge areset", "")
    assert prove(source, sync_rtl, "TopModule", "clk", workdir=tmp_path / "s").status == "refuted"   # a synchronous reset is wrong here


def test_an_output_register_assigned_to_itself_translates_as_the_register():
    source = COUNTER.replace("    cnt = Reg(4)", "    cnt = Reg(4)\n    q = Reg(4)").replace(
        "            self.cnt.next = self.cnt + 1", "            self.cnt.next = self.cnt + 1\n            self.q.next = self.q + 1").replace(
        "        self.q = self.cnt", "        self.q = self.q")
    assert "output reg [3:0] q" in translate(source).verilog


SHIFTS = '''from refkit import *
class Ref(Module):
    q = Reg()
    s = Reg(8)
    n = Reg()
    @seq
    def step(self):
        if self.shift_ena:
            self.q.next = self.q.shift_in_low(self.data)
        self.s.next = self.s.shift_in_high(self.data)
        self.n.next = self.n + 1
    @comb
    def out(self):
        self.lsb_first = self.s
        self.g[0] = self.data
        if self.shift_ena:
            self.g[2:1] = B(3, 2)
        else:
            self.g[2:1] = self.q[1:0]
__refkit__ = {"module": Ref, "combinational": False, "clock": None, "reset": "reset", "active": 1, "style": "sync",
  "widths": {"q": 4, "lsb_first": 8, "g": 3}, "inputs": {"shift_ena": 1, "data": 1, "reset": 1}}
'''


def test_shift_helpers_unsized_registers_and_bit_assignments_translate():
    v = translate(SHIFTS).verilog
    assert "output reg [3:0] q" in v and "reg [31:0] n" in v
    with pytest.raises(Unsupported, match="not set on every path"):
        translate(SHIFTS.replace("        else:\n            self.g[2:1] = self.q[1:0]\n", ""))


@pytest.mark.skipif(not have_tools, reason="SymbiYosys not available")
def test_proof_of_shift_helpers_and_bit_assignments(tmp_path):
    rtl = """module TopModule(input clk, input reset, input shift_ena, input data, output reg [3:0] q, output [7:0] lsb_first,
      output [2:0] g);
      reg [7:0] s;
      always @(posedge clk) begin
        if (reset) begin q <= 0; s <= 0; end
        else begin if (shift_ena) q <= {q[2:0], data}; s <= {data, s[7:1]}; end
      end
      assign lsb_first = s;
      assign g = {shift_ena ? 2'b11 : q[1:0], data};
    endmodule
    """
    assert prove(SHIFTS, rtl, "TopModule", "clk", workdir=tmp_path / "ok").status == "proven"
    flipped = rtl.replace("q <= {q[2:0], data}", "q <= {data, q[3:1]}")
    assert prove(SHIFTS, flipped, "TopModule", "clk", workdir=tmp_path / "flip").status == "refuted"


def test_the_clock_port_of_the_translation_can_take_the_blocks_own_name():
    v = translate(COUNTER, "counter", "CLK").verilog
    assert "module counter (input CLK," in v and "always @(posedge CLK)" in v and "posedge clk" not in v
    assert "input clk," in translate(COUNTER).verilog                    # the default is unchanged (the miter uses clk)


STATE_HOLD = '''from refkit import *
class Ref(Module):
    st = State("L", "R", reset="L")
    @seq
    def s(self):
        if self.a:
            self.st.next = "R"
        elif self.b:
            self.st.next = "L"
        else:
            self.st.next = self.st
    @comb
    def o(self):
        self.left = self.st == "L"
        self.right = self.st == "R"
__refkit__ = {"module": Ref, "combinational": False, "clock": None, "reset": "rst", "active": 1, "style": "sync",
  "widths": {"left": 1, "right": 1}, "inputs": {"a": 1, "b": 1, "rst": 1}}
'''


def test_a_state_register_assigned_to_itself_holds_its_value():
    _cosim(STATE_HOLD)
