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
    stepped = COUNTER.replace("@seq\n", '@seq(edge="neg")\n')
    with pytest.raises(Unsupported):
        translate(stepped)


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
