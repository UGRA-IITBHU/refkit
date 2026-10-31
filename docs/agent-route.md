# The agent route for writing a RefKit reference: design, and what is built

Designed 2026-10-17 in conversation. **Only a small part is built** (section 2). The rest is a design to build after the RTLLM
run; the prompts below are the ones written for it, kept here so they are not lost.

## 1. The route

Every agent has one small job, sees only the description, the ports and the reset (never the RTL or the golden design), runs at
temperature 0.2 on the local 27B, and answers in JSON that code validates. No code matches words in the description; any routing
decision (for example skipping the transition agent for a block with no state) is a model's answer.

```
DESCRIPTION + PORTS + RESET
   |
   +- A1 Splitter ---------- units of the block (counter, FSM, shifter, ...)
   +- A2 Storage agent ----- every stored value: width, reset, when it changes
   +- A3 Timing agent ------ every output: same cycle or next cycle, and why
   +- A4 Transition agent -- every state/mode x every input combination, priority, default
   +- A5 Bit-convention agent  bit order, numbering, packing, signedness, wrap/saturate
   |
   +- A6 Consolidator ------ settles conflicts and NOT STATED points -> SPEC SHEET (rules R1..Rn)
   +- A7 Exampler ---------- 2 worked scenarios, cycle by cycle, from the spec sheet
   +- A8 Example checker --- recomputes each row from the description alone; keeps agreed rows
   |
   +- B1 Writer ------------ RefKit module (guide + spec sheet + agreed examples as `examples=`)
   +- B2 Repair ------------ RefKit errors + line + example mismatches -> fix (2 rounds)
   |
   +- C1 Rule auditor ------ each rule -> the line that implements it, or MISSING -> one fix
   +- C2 Second writer ----- an independent reference (no notes); compare_sources compares the two exactly
   +- C3 Arbiter ----------- if they differ: which output matches the description on the counterexample
                             -> final reference, plus the list of disputed or NOT STATED points
```

Why these: A2 to A5 each own one class of failure measured on the benchmark (widths and reset values, output timing, missing
cases, bit order); A8 and C2 are independent second opinions (the only checks that catch a confident misreading); the agreed examples
become `examples=` so a wrong module raises an error the writer can read (the first repair signal for wrong behaviour that needs no
RTL); C1 is a lookup task (rule to line), not "find the bugs" (a reviewer asked to find bugs answered NO ISSUE on every wrong reference).

Cost: about 12 to 15 model calls per block, about 15 to 25 minutes per problem on the single-slot 27B.

## 2. What is built

| Route | Built? | Where |
|---|---|---|
| A1 to A5 | **merged into one call**, the "decomposer": parts A (stored values), B (states), C (outputs and their timing), D (transitions and cases), each quoting the description, `NOT STATED` where it is silent, a numeric bit-order check for shifting values | `decomposePrompt` in `BARG/packages/backend/src/pipeline/refkit.ts` |
| A6 Consolidator, spec sheet with rule ids | no | |
| A7 Exampler | **built**, one scenario of 8 to 16 cycles as a table, no rule ids | `examplePrompt`, same file |
| A8 Example checker | no | |
| B1 Writer | built as the normal RefKit prompt plus the notes as a hint ("if they contradict the description, follow the description"); no rule ids in comments, no `examples=` | `detailBlock`, `refkitPrompt(..., detail)` |
| B2 Repair, 2 rounds | no; the existing one fix of the model's own error remains | `BlindReference.fixOwnError` |
| C1 Rule auditor | no | |
| C2 Second writer | no (compare_sources exists in RefKit) | |
| C3 Arbiter | a prototype exists for function vs RefKit readings (59/74 on the dev half); not wired to this route | `bench/analysis/arbiter.mjs` |

Run as the bench arm `refkit-detail` of `bench/analysis/ref_quality.mjs`. Both detail calls are capped at 1,200 and 1,500 tokens
and told to be brief (uncapped they wrote up to 27k characters and took up to 14 minutes a call). Nothing here is in the pipeline
that BARG runs; it is a measurement arm.

What the arm showed (VerilogEval, 27B, 0.2, 19 of 52 references when stopped): equal to the plain general guide
(5 of 9 against 5 of 10), q5a right once where the plain arm was 0 of 2, shiftcount wrong because the decomposer read
"MSB first" as a left shift with `data` as the new top bit. The RTLLM run (29 clocked designs) uses this arm; see
`known-issues.md` section 6.

## 3. Shared system prompt (A1 to A8, C1, C3)

```
You are one step in a pipeline that writes a reference model of a digital hardware block from its natural-language
description. You see the description, the port list and the reset; you never see the hardware design under test and must
not guess what it contains. Work only from the description's words and the standard meaning of digital-design terms.
Quote the description for every claim you make. When the description does not settle something, write NOT STATED, then
the most conventional choice and one line of reason. Do not write code unless asked. Answer only in the format requested,
inside one ```json fenced block.
```

Every agent receives `{DESCRIPTION}`, `{PORTS}` (name, direction, width) and `{RESET}` (port, active level, sync or async).

## 4. Prompts

**A1 Splitter**
```
Split this block into the smallest units that each do one job: a counter, a shift register, a state machine, a
comparator, a memory or register file, an output decoder, a combinational function. A block that does one job is one unit.
For each unit give: "id" (U1, U2, ...), "job" (one sentence), "reads" (input ports and other units it uses), "drives"
(output ports it sets and values it gives other units), "quotes" (the sentences of the description it comes from).
Then "clocked": true if anything is remembered from one cycle to the next, else false.
Format: {"units": [...], "clocked": true|false}
```

**A2 Storage agent** (input: A1 units)
```
List every value the block must remember from one clock cycle to the next (registers, counters, states, previous values,
shift registers, memories). For each: "name", "unit", "meaning", "width" (the number of bits the description gives, or
NOT STATED and your choice; a count with no stated width is "unbounded count"), "reset" (its value after reset, quote, or
NOT STATED), "changes" (every condition under which it changes and to what, in the description's order), "limits"
(wraps at / saturates at / stops at / none, with the quote). Do not list values that only exist within one cycle.
Format: {"stored": [...]}
```

**A3 Output timing agent** (input: A1 units, A2 stored values)
```
For each output port: "port", "meaning", "depends_on" (input ports and stored values), "timing" ("SAME CYCLE": it follows the
inputs of the cycle, combinational or Mealy; or "NEXT CYCLE": it shows a value only after a clock edge, registered, or Moore:
from the state only), "evidence" (the quote that decides the timing; if the description calls the machine Moore, or says the
output depends on the state only, the output cannot react to an input in the same cycle; if nothing decides it, NOT STATED),
"during_reset" (its value while the reset is asserted and in the first cycle after), "dont_care" (the conditions under which
the description says its value does not matter, or "never").
Format: {"outputs": [...]}
```

**A4 Transition agent** (input: A1, A2)
```
For each state, or for each mode of a unit with no states, list every condition the description gives and what happens
(next state, values changed), in the description's priority order. Then: if 3 or fewer inputs control a state, give a full
table of every combination of them, inputs that are 1 at the same time included, with the result of each; say what happens
when no condition holds (stay, or the quote); name states that cannot be left except by reset and states never reached.
Format: {"states": [{"state": "...", "rules": [{"if": "...", "then": "...", "quote": "..."}], "table": [...], "otherwise": "..."}]}
```

**A5 Bit-convention agent** (input: A2)
```
For every multi-bit value, every multi-bit port and every serial stream, state: "numbering" (is bit 0 the least significant
bit; are positions numbered from 0 or from 1: a "tap at position 5" of a 5-bit value is bit 4), "serial_order" (for bits that
arrive or leave one per cycle: MSB first or LSB first; the end of the register a new bit enters and the end the data leaves
from), "packing" (which field is high and which low in each multi-bit port), "encoding" (binary, BCD, one-hot, Gray, or the
state codes the description gives), "signed" (signed or unsigned; whether arithmetic keeps a carry or overflow bit), "check"
(a tiny numeric example proving each convention, e.g. "receiving 1,0,1,1 MSB first gives 1011").
Format: {"values": [...]}
```

**A6 Consolidator** (input: A1 to A5)
```
1. Find every place where these answers disagree with each other or with the description. Settle each by quoting the
   description; if it does not settle it, record it as NOT STATED with the conventional choice.
2. Write the SPEC SHEET: numbered rules R1, R2, ..., each one sentence that can be checked on a cycle-by-cycle trace
   ("R4: done is 1 in the cycle after the stop bit, and 0 otherwise"), with its quote. Cover every stored value, every output
   (with its timing), every transition and every bit convention. One fact per rule.
3. List the open points: every NOT STATED choice, and every disagreement settled without a quote.
Format: {"rules": [{"id": "R1", "rule": "...", "quote": "..."}], "open": ["..."]}
```

**A7 Exampler** (input: the spec sheet)
```
Make two worked scenarios of 8 to 16 cycles each: Scenario 1, normal operation (the main path, every output changing at least
once); Scenario 2, edge cases (inputs that are 1 at the same time, a counter wrapping or saturating, an event right after
another, a reset in the middle). Start each scenario with the reset asserted for one cycle. For every cycle give the inputs, the
stored values at the START of the cycle, the outputs of that cycle, and the rule ids that fixed them. Compute each row from the
row before by the rules only, with numbers.
Format: {"scenarios": [{"cycles": [{"inputs": {...}, "stored": {...}, "outputs": {...}, "rules": ["R3", "R7"]}]}]}
```

**A8 Example checker** (independent: it never sees the spec sheet or the rule ids)
```
Recompute every row yourself from the description alone, starting from the first row. For each row give "AGREE", or
"DISAGREE" with the value you get, the quote it comes from, and whether the description actually settles the point
(SETTLED or NOT STATED).
Format: {"rows": [{"scenario": 1, "cycle": 0, "verdict": "AGREE|DISAGREE", "expected": {...}, "quote": "...", "settled": true|false}]}
```
Code, not a model: if any row disagrees, A7 revises once with the checker's notes; a scenario is kept only up to its first
disputed row; disputed points the description does not settle go to the open-points list.

**B1 Writer** (the existing RefKit prompt, plus the spec sheet)
```
=== SPEC SHEET (from readers of the description; the description stays the authority) ===
{rules}
=== END ===
Write the RefKit module so that every rule R1..Rn is implemented. Beside each line that implements a rule, write its id as a
comment (# R4). Do not write the examples: RefKit is given them.
```
Code inserts the agreed rows as `examples = [...]`; RefKit then checks the module against them on every run.

**B2 Repair** (the existing one-time fix, extended to 2 rounds)
```
{writer prompt}
You wrote: {code}
Running it, RefKit reported: {error}
The error came from line {N}: {line}          <- or, for an example that does not match:
Scenario {s}, cycle {c}: the agreed example gives {port} = {value}; your module gives {other}. The rule behind that row is
{Rk}: "{rule}".
Correct the module. If you are sure the example contradicts the description, quote the sentence and keep your code.
```

**C1 Rule auditor**
```
For each rule, give the line numbers that implement it and "OK", "MISSING" (no line does it) or "CONTRADICTS" (a line does the
opposite: say how). Do not look for other problems.
Format: {"rules": [{"id": "R1", "lines": [12, 13], "status": "OK|MISSING|CONTRADICTS", "why": "..."}]}
```
For any MISSING or CONTRADICTS the writer gets that list for one fix, and RefKit reruns the examples.

**C2 Second writer**: the plain RefKit prompt (guide plus description, no spec sheet), an independent reading. `compare_sources`
compares the two exactly over every reachable state: equal means done with high confidence; different returns the shortest input
sequence on which they differ, and C3 decides.

**C3 Arbiter** (the BARG arbiter prototype)
```
Input sequence: {counterexample}
Reading A gives: {outputs A per cycle}     Reading B gives: {outputs B per cycle}
They first differ at cycle {k}. Which reading does what the description says at that cycle? Quote the sentence. If the
description does not decide it, answer NOT STATED.
Format: {"choice": "A|B|NOT STATED", "cycle": k, "quote": "..."}
```
Asked twice with A and B swapped. NOT STATED, or disagreement between the two asks, marks the block low-confidence and does not
force a choice.

## 5. Rules for the whole route
- One job per agent and a small context. Only the writer and the repair agent get the guide.
- JSON out, validated by code. Malformed JSON gets one retry; a field that is still bad is dropped, never guessed.
- Open points travel with the reference: a NOT STATED choice the final reference depends on is reported, so an RTL verdict there is
  low-confidence and not "wrong".

## 6. Order to build
1. Split the decomposer into A2 to A5 and add A6, the spec sheet with rule ids in the writer's comments.
2. Add A7 with two scenarios and A8, then insert agreed rows as `examples=` and extend repair to 2 rounds (B2).
3. Add C1.
4. Add C2 and C3.

Measure each step on the same set (the dev half with the same settings in every arm, then a fresh held-out set), and keep a step only
if it earns its extra calls. Before building more, read the RTLLM result: the families that fail (arithmetic, clock dividers) may
need guide rules, not more agents.

## 7. Known risks of this route
- The notes can be wrong and the writer follows them (shiftcount: the decomposer's bit order). The numeric check in A5 and the
  independent A8 are meant to catch that; neither has been tested.
- A wrong example, used as `examples=`, forces a wrong module. A8 agreement and the rule that the description wins are the guard.
- The route cannot fix a description that does not settle the point (fsm_serialdata, q5a): it can only report it as NOT STATED.
- Cost: 12 to 15 calls per block on a model that is 31% on CPU.
