# RefKit: references written like Verilog, run by Python, proven with SymbiYosys

RefKit is how BARG writes the **reference model** of a block, the oracle every check compares the LLM's RTL against. Since
2026-10-16 it is the default for every block (`referenceStyle: "refkit"`); the older free-form Python function
(`referenceStyle: "function"`) is kept only for ablations.

Why: on the final VerilogEval figure, 72 of the 120 failed A0 runs (60%) started with a reference that disagreed with the
benchmark's golden design, 59 of them clocked designs (`bench/analysis/classify_failures.mjs`, `docs/benchmarking-continue.md`
section 3i). A free-form function over a list of cycles lets the model get the *timing* wrong as easily as the behaviour (an output
a cycle early, a reset at the wrong moment, a counter that does not wrap). RefKit takes the timing, the reset and the widths out
of the model's hands and leaves it only what the description says.

| Piece | File |
|---|---|
| The library (runtime, Verilog semantics, step mode, exact comparison, transition walk) | `packages/harness/src/harness/refkit.py` |
| RefKit to Verilog, and the SymbiYosys proof | `packages/harness/src/harness/refkit_verilog.py` |
| The guide the models are taught from (core + cookbook) | `packages/backend/guides/refkit.md` |
| Prompt, pattern selection, completing the model's class into the harness function | `packages/backend/src/pipeline/refkit.ts` |
| Where the pipeline uses it | `pipeline/reference.ts` (`BlindReference`), `workers/worker.ts` (S2, S7) |
| Tests | `packages/harness/tests/test_refkit.py`, `test_refkit_verilog.py`, `packages/backend/test/refkit.test.ts`, `benchRun.test.ts` |
| Measuring reference quality alone | `bench/analysis/ref_quality.mjs` |

## 1. What the model writes

```python
from refkit import Module, Reg, State, Wire, seq, comb

class Ref(Module):
    st = State("IDLE", "RUN")          # reg with named states, reset to IDLE
    cnt = Reg(4)                       # reg [3:0] cnt, reset to 0

    @seq                               # always @(posedge clk)
    def fsm(self):
        if self.st == "IDLE" and self.go:
            self.st.next = "RUN"       # st <= RUN
        elif self.st == "RUN":
            self.cnt.next = self.cnt + 1
            if self.cnt == 9:
                self.st.next = "IDLE"

    @comb                              # assign
    def outputs(self):
        self.busy = self.st == "RUN"
        self.q = self.cnt
```

The model never writes the loop over cycles, the reset, or a mask. The backend appends the function the harness calls
(`ref_<block>(cycles)` for a clocked block, `ref_<block>(**ports)` for a combinational one) and a `__refkit__` record of the module,
its ports, its reset and its clock, so the rest of the pipeline (stimulus, differential, mutation, ambiguity) is unchanged.

The complete language is in the guide (`guides/refkit.md`): a construct-by-construct Verilog to RefKit table and the full API.
Highlights:

- **Verilog's width rules, one to one.** Operators build expressions that are evaluated where Verilog evaluates them, at the
  width Verilog would use: an assignment to a 9-bit destination keeps the carry of two 8-bit operands; a Python integer is a 32-bit
  unsized constant (`self.count + 1 == 16` is true for a 4-bit count at 15); comparisons size both sides to the wider one;
  `~c == 0` is never true for a narrow `c`, as in Verilog.
- **Non-blocking assignments**: `self.r.next = v`, `next_bit`, `next_slice`; every `@seq` block sees the values from before the
  edge; `self.r = v` in a clocked block is an error that says so.
- **All edges**: `@seq` (posedge), `@seq(edge="neg")`, `@seq(edge="both")`, `@latch` (transparent), run step by step with the clock
  as an input (`run_stepped`), matching the harness's step mode.
- **Structure**: `Wire`, `Mem` (arrays), `State`, parameters as class attributes, functions as methods, submodules
  (`Instance` with `Input`/`Output` declarations), `match` for `case`, `casez()`, `mux()`, `clog2()`, signed operations.
- **Reset**: applied by RefKit from the block's interface (sync or async, active high or low) to every register with a reset
  value; `Reg(w, reset=None)` is a register the reset does not touch, `init=` its power-up value.
- **Written for the model, not for Verilog**: the constructs a model got wrong on the dev half have a form that says what
  happens. `v.shift_in_low(d)` / `v.shift_in_high(d)` name where the new bit enters; hand-written `cat()` shifts flipped the
  bit order. The guide teaches the language only: no advice written from a benchmark problem's description or answer. `Reg()` without a width is the output port's width, or 32
  bits; a width the model invented wrapped where the description had none. `self.g[0] = ...` sets bits of an output in `@comb`,
  as Verilog's `assign g[0] = ...` does.
- **Checks on the model's own code**: an undeclared name, a register assigned without `.next`, an output never set, a `moore = True`
  claim that is false, an `examples` waveform from the description the module does not reproduce, `.next` on an input port, an output with some bits
never set, and an assignment made while the reset is asserted that the reset overrides, in a block that reads the reset port (the
ordinary reset written by hand, disagreeing with the declaration or with the reset's polarity). Any of these raises with a
  message that says what to write instead; the reference model gets one fix call showing only its own code and the error
  (`BlindReference.fixOwnError`), so the reference stays blind to the RTL.

## 2. Teaching the models: the guide and retrieval

The guide is split by the marker `<!-- COOKBOOK -->`:

- the **core** (always in the prompt): the shape of a reference, the cycle timeline, the reset, edges and latches, case
  statements, Verilog's widths, the Verilog-to-RefKit table, the complete API ("nothing else exists"), a checklist;
- the **cookbook**: patterns (counter, Moore and Mealy machines, sequence detector, shift register, serial receiver, edge
  detection, pipeline, memory and FIFO, arithmetic with carry and signed values, waveforms, enable and hold, one-hot states,
  negedge and latches), each with a "Use when" line.

`refkitRetrieval: "select"` shows the reference model the cookbook's titles and "use when" lines and lets it name the patterns it
wants (one extra call); no word of the description is matched by code. `"all"` (the default) gives the whole guide. Every module
example in the guide is executed by the tests, so the guide cannot teach code that does not run.

What keeps the model from inventing API: the complete API is always in the prompt, anything else raises at once with a named
error, and the one fix call follows. That makes invented calls fail loudly; it cannot make a misreading of the description
impossible.

## 3. What RefKit adds to the checking

1. **Exact comparison of two readings** (`refkit.compare`, `compare_sources`; harness `reference-stability`): two RefKit
   references are compared over every state they can reach together on every input, and the shortest input sequence on which they
   differ is returned. Used wherever two readings are compared (the second attempt, the reference vote). Falls back to the
   stimulus when the input space is above 4096 values per cycle or the joint state space above 20000 states.
2. **Transition coverage** (`refkit.transition_walk`; harness `stimulus`): for a clocked reference with a reset and at most 256
   input values per cycle, the stimulus gets cycles that take the reference through every transition it can make (every reachable
   state on every input value), within 1500 cycles. If some are not taken, the verdict is at most VERIFIED_LOWCONF and the
   reasons give the count.
3. **Formal proof with SymbiYosys** (`refkit_verilog.py`; harness `refkit-prove`; run option `formalProof`, on by default): once the
   simulation checks pass, the RefKit reference is translated to Verilog and SymbiYosys proves, by k-induction, that every output of
   the LLM's RTL equals the reference's in every cycle after reset, for every input sequence.
   - The translation executes each block symbolically: locals, branches, `match`, loops and helper methods become one expression
     per wire, output and register, with SystemVerilog casts giving each assignment exactly RefKit's width.
   - The miter assumes the reset in the first cycle and asserts equality afterwards (from the start for a combinational block).
   - A counterexample is replayed in simulation against the reference and counted only if the simulation shows the same
     difference: a register the RTL never resets is arbitrary in the proof but unknown in simulation, and must not condemn the RTL.
   - **proven**: the stimulus-coverage caveats (weak coverage, mutation gaps, untaken transitions) no longer apply.
     **refuted**: the verdict is REVIEW, with the counterexample in the reasons. **unknown/unsupported**: no change.
   - Not translated yet (the simulation checks still apply): falling-edge, dual-edge and latch blocks, instances, file-loaded
     memory, don't-care (`None`) outputs, a memory cleared by an asynchronous reset.

A proof shows the RTL equals the **reference**. If the reference misreads the description, the RTL is proven equal to the
misreading. The guide, the exact comparison of readings and the reference vote are what work on that.

## 4. Options

| Run option | Default | Meaning |
|---|---|---|
| `referenceStyle` | `"refkit"` | `"function"`: the older free-form reference (ablations only) |
| `refkitRetrieval` | `"all"` | `"select"`: the model picks cookbook patterns |
| `formalProof` | `true` | `false`: no SymbiYosys proof |

SymbiYosys and Yosys are found next to the Yosys the toolchain uses (`/home/aura/tools/oss-cad-suite/bin` on the Ubuntu machine).

## 5. Measuring it

`bench/analysis/ref_quality.mjs` writes references the way the pipeline does, with no RTL, and checks each against the benchmark's
golden design. The 74 clocked VerilogEval problems are split by position (even: dev, odd: test); the guide is tuned on dev only and
the test half is run once, at the end. RTLLM is the held-out benchmark.

Caveat for the first dev run (started 2026-10-16): its harness takes the transition walk for RefKit references only, so RefKit
references are judged on more input cycles than function references, which can only make RefKit look worse.
