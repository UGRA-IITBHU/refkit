# RefKit and the reference pipeline: known issues, findings and next work

Written 2026-10-17, while the held-out RTLLM run was still going. Everything here comes from the runs of this session; file names
are in `~/Desktop/BARG/bench/results/` unless a path is given. Nothing in either repository is committed: RefKit changes are on branch
`llm-friendly` in `~/Desktop/refkit`, BARG changes on `track-a` in `~/Desktop/BARG`.

Each issue has an id (M = measurement, L = language and guide, P = pipeline, T = translator, I = infrastructure), what was seen,
the evidence, a status and the work it points to. Status: **fixed** (in the working copy, measured or tested), **partly**, **open**.

---

## 1. State of the numbers (27B qwen3.8, VerilogEval dev half, the 13 problems the first RefKit run got wrong)

References right, out of references written. Function / old RefKit / comments arm are 2 samples at temperature 0.2; the rest are
one sample, so a flipped problem moves a total by 8 points.

| Version | Right | Notes |
|---|---|---|
| Function reference (free-form Python) | 13/26 (50.0%) | `arbiter-dev.jsonl` |
| Old RefKit, whole guide | 10/26 (38.5%) | `arbiter-dev.jsonl` |
| Comments arm (comment each state and case) | 6/26 (23.1%) | `plan-ablation-dev.jsonl` |
| New syntax + problem-derived guide, t=0 | 8/13 | `refkit-27b-failing-t0.jsonl`, `refkit-27b-guidefix-t0.jsonl`. **Not a general result**: the guide contained hints taken from these problems. |
| Cleaned guide, t=0 | 5/11 | `refkit-teacher-27b-t0.jsonl` (`before` field); run stopped at 11 |
| General guide, t=0 | 6/13 | `refkit-general-27b-t0.jsonl` |
| General guide vs + detailer, t=0.2 | partial: 5/10 vs 5/9 at 19 of 52 refs | `refkit-detail-27b-t02.jsonl`; stopped, notes too long |
| RTLLM held-out, general guide + detailer, t=0.2 | see section 6 | `rtllm-refkit-detail-27b.jsonl` |

On the 74 clocked VerilogEval problems (earlier session) function 55, old RefKit 51, comments 52; the arbiter (function vs RefKit)
gets 59 with a ceiling of 62. Earlier in this session I quoted totals of 15, 12 and 8 for the 13 problems; the correct ones are
13, 10 and 6 (above). A table with those wrong numbers was shown to the user once.

## 2. Measurement issues

- **M1. The benchmark is partly memorised. open.** VerilogEval is built from HDLBits, a public site with published solutions.
  The detailer's notes on q5a named the problem ("this specific ECE 241 problem (Prob089)"). The literature (section 7) says both
  VerilogEval and RTLLM show contamination. Every VerilogEval number flatters direct Verilog and the function most. Work: mutated,
  renamed or re-parameterised copies of the problems as a held-out set (LiveVerilogEval does this).
- **M2. Several descriptions leave the behaviour open. partly.** The golden design fixes something the text never says:
  `done` one cycle after the stop bit (fsm_serial, fsm_serialdata); "Moore" meaning `z` lags the input by a cycle (2014_q5a);
  the reset value of the gshare tables; what an output shows when nothing is reported (accu `data_out`, multi_16bit `yout`).
  Every arm, the function included, fails fsm_serialdata and q5a. A clarified copy exists at
  `~/datasets/verilog-eval/dataset_spec-to-rtl-clarified` (4 prompts edited, `CHANGES.md`); its results are not in yet
  (`refkit-general-27b-t0-clarified.jsonl` has 4 rows, from the general guide, not yet read against the originals).
  Work: audit all problems, not only the 13; record NOT STATED points per problem.
- **M3. Two scores disagree. open.** `ref_quality` compares the reference with the golden design on a stimulus (strict). RTLLM's own
  testbench checks only some cycles and outputs. accu and multi_booth_8bit differ from the golden design on the harness stimulus but
  their translations **pass** RTLLM's testbench. Report both, and say which one a claim uses.
- **M4. The stimulus misses behaviour. open.** 128 cycles never reach the 12→01 hour change in count_clock, so a reference with a
  wrong hour wrap scored "right" (`weak coverage` in the gate text). `transition_walk` exists but only covers small input spaces.
  Work: stimulus that reaches deep counter values (directed to state/counter limits), and a count of outputs never changed.
- **M5. Small samples, mixed temperatures. open.** One sample per problem at t=0 or two at t=0.2; 13 problems. Sign tests on
  that size have no power. The whole dev half (37 problems) with the same settings in every arm is needed before any claim.
- **M6. `firstMismatch` labels. open, unverified.** In the stored mismatches the field named `expected` appeared to hold the
  candidate's value (e.g. count_clock `expected hh=0x13`, which the golden cannot produce). Check `checks.differential` before
  trusting the labels; verdicts (agrees true/false) are not affected.
- **M7. Held-out hygiene. note.** RTLLM was run before (RTL generation, function reference, `rtllm-opencode.jsonl`,
  `rtllm-glm.jsonl`, `rtllm-qwen-vote.jsonl`, models other than the 27B) and now once more (reference quality). Nothing was tuned on
  RTLLM, but the guide's "held-out" claim should say how many times it was looked at. After this run the guide and the
  settings must not be changed to fit RTLLM results.

## 3. Language and guide issues (what the 27B and the 7B got wrong)

- **L1. Shift direction. partly.** Hand-written `cat()` shifts flipped the bit order (shiftcount, q12, lfsr5, serial receivers).
  Added `shift_in_low` / `shift_in_high`, an MSB-first / LSB-first table and "write a shift as one operation". shiftcount flips
  between right and wrong depending on the guide text and the sample; the table is what fixes it. The detailer's prose misread
  "MSB first" as a left shift with `data` as the new top bit (shiftcount, both samples) and the writer followed it.
- **L2. `@seq` / `@comb` confusion in the 7B. open.** 13 of the 22 references the 7B wrote ended in an error: `.next` inside
  `@comb`, a register assigned with `=`, `self.out.next`, an undeclared register (`st_reg`), an invented method
  (`saturating_add`), a `part(3, 0)` of width 0, a module flagged as needing step mode. The new messages did not make the one
  fix succeed. 0 of 21 were right. Work: a form with less structure to get wrong, or a model that is better at it (fine-tune parked).
- **L3. Widths the description never gave. fixed.** `Reg()` with no width takes the output port's width, else 32 bits. lemmings4's
  5-bit fall counter wrapped; not re-measured on lemmings4.
- **L4. The ordinary reset written by hand. fixed (error).** q2bfsm: `if not self.resetn: go to F`. RefKit now reports an
  assignment its own reset overrides, with the line. The first wording named "after the reset is released" (the answer to a
  benchmark problem) and was neutralised.
- **L5. Outputs bit by bit, inputs. fixed.** `self.g[0] = ...` in `@comb`, `.next` on an input port, `r[i] = ...` on a register
  all raise messages that say what to write.
- **L6. What an idle output shows. open.** accu keeps `data_out` after the 4th input; multi_16bit keeps `yout` when `start` is
  low; the golden designs return 0 or clear it. The guide says "set every output in every cycle" but not what a registered result
  does when it isn't being reported. Work: a guide rule on result registers (hold only if the description says it holds).
- **L7. Pipeline latency. open.** multi_pipe_8bit: enable and data one stage short, so `mul_en_out` drops early. Even plain RTL
  from the earlier models failed it 0 of 3 in all systems; in two A0 runs the function reference and the RTL agreed with each
  other and both were wrong (false VERIFIED).
- **L8. Memory addressing and pointer start. open.** LIFObuffer: 4-word memory, stack pointer reset to 4, first write at address
  4 (RefKit raised, the one fix did not repair it).
- **L9. Special cases not written. open.** float_multi: overflow to infinity, zero and infinity operands are in the description and
  missing from the reference.
- **L10. Cascaded counters. partly.** count_clock: units stepped by the enable alone (hours advance every second). A rule that a
  counter steps only when the one below wraps fixed seconds and minutes; the hour logic (12→01 and AM/PM at 11:59:59) is still
  wrong and was never exercised by the stimulus (M4).
- **L11. Pattern search inside a larger machine. partly.** review2015_fsm detects 1101 with a shift register that keeps old bits and
  one cycle late; the guide now says "states or a shift register" without forcing either (the forcing text was problem-derived
  and removed). Still wrong in every guide.
- **L12. Held versus pulsed event outputs. open.** Moore/Mealy timing of an event flag: fsm_serial, fsm_serialdata, q5a. The
  guide has a general note; clarified descriptions are the real fix (M2).
- **L13. Sequence detectors and gshare. open.** sequence_detector (RTLLM): flag one cycle late; gshare: history updated on
  training that was not a misprediction, so `train_mispredicted` changed nothing.
- **L15. Clock-domain and clock-generating designs. open.** freq_div, freq_divbyfrac, freq_divbyodd, synchronizer: the clock is not a signal in a RefKit module; step mode with `edge="neg"` exists but the models do not use it consistently.
- **L16. "Just became" conditions. open.** traffic_light: an edge condition (`not p and p`) with no stored previous value.
- **L14. The guide is long. note.** about 5,800 words (8k tokens). The 27B has a 16k context, so guide + description + notes
  must fit. Pattern selection (`refkit-select`) exists but was not measured with the new guide.

## 4. Repair and checking issues

- **P1. One narrow repair. open.** The model's own code error gets one fix (code + RefKit's message + line). Wrong behaviour
  that runs raises nothing, so it is never repaired. The regeneration after a gate rejection sends the same prompt and, at
  temperature 0, repeats it (circuit9, review2015_fsm, gshare were rejected twice with the same text). Now back at 0.2.
- **P2. A teacher/reviewer model did not help. dropped.** Shown the description, the code and a cycle-by-cycle trace it
  answered "NO ISSUE" on every wrong reference (shiftcount, q5a, fsm_serial). Removed at the user's request.
- **P3. The detailer. unproven, costly.** decomposer + exampler notes: q5a right once in two (plain 0 of 2), shiftcount wrong
  (decomposer's bit-order misread), the rest equal at 19 of 52 references; made up facts ("reset: none"); uncapped notes ran
  to 27k characters and 14-minute calls (5–20x the time). Capped now (1,200 and 1,500 tokens, "30 lines", "16 rows").
  Whether the exampler's table can serve as executable `examples=` (a repair signal) is untested.
- **P4. The long agent route is a design only.** Splitter, storage, timing, transition, bit-convention agents, consolidator,
  exampler, checker, writer, repair, rule auditor, second writer, arbiter: prompts were written, nothing was built beyond the
  decomposer and exampler.
- **P5. Gate rejects rather than repairs. open.** The non-triviality gate catches constant outputs and ignored inputs; no
  second reading is asked for beyond one blind regeneration.

## 5. Translator and infrastructure

- **T1. Not translated. open.** Python chained comparison (`a < b < c`) (multi_16bit); a local whose Python value differs between
  branches (float_multi); step-mode blocks, instances, file-loaded memory, don't-care outputs, the whole `Mem`+reset async case.
  Work: chained comparisons and branch-merged locals are generic and small.
- **T2. Translator clock name. fixed (translate(..., clock_name)); fsm and calendar now pass.** The translated module takes the design's name but the clock is always `clk`; fsm and
  calendar (RTLLM, clock `CLK`) failed to compile against their testbenches for that reason alone.
- **I1. Slow 27B. open.** 18 GB on a 16 GB GPU, 31% on CPU. One slot only; 4 parallel slots work for the 7B. A detail
  reference is 3 calls (100–1,200 s). Ollama (a user process) died once and hung once at 0% CPU with an open connection.
- **I2. Working copy wiring. note.** BARG's `.venv-harness` has refkit installed **editable from `~/Desktop/refkit`**
  (the pinned copy, commit 5d419e1, is backed up in the session scratchpad `refkit-pinned/`). BARG's `pyproject.toml` pins a
  RefKit commit; adopting these changes needs a RefKit commit and an updated pin.
- **I3. Uncommitted work.** RefKit: `core.py`, `verilog.py`, `guide.md`, tests, docs. BARG: `prompts.ts`, `reference.ts`,
  `refkit.ts`, `ref_quality.mjs`, `refkit_as_rtl.mjs`, `refkit_translate.py`, `plan_ablation.py`, tests. Ask before committing.
- **I4. Fine-tuning parked.** A conda env `refkit-ft` was started and stopped; a partial env remains at
  `~/miniconda3/envs/refkit-ft`. MG-Verilog (MIT) was identified as a corpus; nothing was downloaded or trained.
- **I5. Removed experiments.** The `cycle()` single-method form and the teacher arm were built, measured briefly and removed.

## 6. RTLLM held-out run (RefKit, general guide + capped detailer, 27B, t=0.2, 29 clocked designs, 1 reference each)

**12 of 29 right (41.4%); 4 ended with a rejected reference** (LIFObuffer, freq_div, synchronizer, traffic_light; scored wrong).
File: `rtllm-refkit-detail-27b.jsonl`. The function arm (`rtllm-function-27b.jsonl`) and the translate-and-score step
(`rtllm-refkit-as-rtl.jsonl`) follow in `scratchpad/rtllm_chain.sh`; compare them in the final breakdown. (The chain first hung because its
wait loop matched the command line of my own waiting job; fixed by waiting on the node process only.)

By family: Arithmetic 0/5, Control 4/6 (counters 3/3, fsm 1/2, LIFO 0/1), Frequency divider 0/3, Others 3/8, RISC-V 2/3,
Shifter 2/2, Signal generation 1/2.

| Right (12) | counter_12, JC_counter, up_down_counter, fsm, LFSR, right_shifter, calendar, edge_detect, parallel2serial, instr_reg, pe, signal_generator |
|---|---|

| Wrong | Cause (read from the stored source) |
|---|---|
| accu | `data_out` register holds the last sum; golden clears it when not reporting (L6) |
| multi_16bit | product register holds when `start` is low; golden returns 0; translation fails on a chained comparison (L6, T1) |
| multi_booth_8bit | no return to a known `rdy`; reset-time check differs. Its translation passes RTLLM's testbench (M3) |
| multi_pipe_8bit | enable pipeline one stage short, `mul_en_out` drops early (L7) |
| float_multi | special cases (overflow to infinity, zero, infinity operands) not written (L9); translation fails (T1) |
| sequence_detector | flag raised one cycle late (L13) |
| LIFObuffer (error) | 4-word memory, stack pointer reset to 4, first write at address 4 (L8) |
| freq_div (error) | the input clock is treated as an ordinary input; outputs come from free-running counters; gate: `CLK_in` changes nothing |
| freq_divbyfrac | a 3.5x divide needs half-cycle steps; wrong high window |
| freq_divbyodd | right shape (rising and falling counters ORed) but both flags reset to 1 and the duty comparison is off by one |
| pulse_detect | an extra state makes the registered output one cycle late (L12) |
| serial2parallel | output register loaded on the 8th input only; arrives a group late |
| synchronizer (error) | two clocks (`clk_a`, `clk_b`); RefKit has one, so the gate saw `clk_a` change nothing |
| traffic_light (error) | next-state conditions that can never be true (`not p_green and p_green`): the transitions never run, `pass_request` does nothing; no previous value was stored |
| width_8to16 | `valid_out` one cycle early |
| RAM | the description is inconsistent (depth 8, "2^6 locations"); the golden uses the full 8-bit address on a 12-entry array and clears 8 entries; the reference masks addresses to 6 bits and has 64 words, so addresses alias (M2) |
| square_wave | follows the description line by line (`count == freq - 1`, toggle); cause **not traced** (golden has `initial` power-up values; check power-up and the 32-bit `freq - 1`) |

### Function arm and translated-Verilog result on the same 29 designs (chain finished)

Function (free-form Python reference, same 27B, t=0.2): **6/29 (20.7%)**, 1,896 s total. RefKit + detailer: **12/29 (41.4%)**, 6,155 s total (3.2x).
Both right 6, only RefKit 6, only function 0, neither 17; exact sign test p = 0.031 (6 to 0). The six RefKit-only wins:
counter_12, calendar, fsm (the function builds the output row **after** updating the state, one cycle early), edge_detect (output from the
current input, not registered), pe (same early output), instr_reg (reset polarity read the other way, `fetch` taken as a bit pattern).
The reverse of the VerilogEval dev half, where the function led 55 to 51 (the descriptions there state timing less plainly, and the
guide had been tuned on that half).

RefKit references translated to Verilog and scored with RTLLM's own testbench (`rtllm-refkit-as-rtl.jsonl`): **15 pass (51.7%)**, 6 fail,
5 not translated, 3 no reference. Of the 12 right references, 10 pass and **2 fail to compile only because the translated clock is
named `clk` and the testbench uses `CLK`** (fsm, calendar; T2, a small generic fix). Five references that differ from the golden
design still pass the testbench (accu, multi_booth_8bit, sequence_detector, width_8to16, square_wave; M3). Not translated: multi_16bit
(chained comparison), float_multi (branch-merged local), freq_divbyfrac and freq_divbyodd (step mode), RAM (memory cleared by an
asynchronous reset).

### Three-way comparison on the same 29 RTLLM designs (27B, t=0.2, one reference each)

| | Reference agrees with golden | Time | Translated Verilog passes RTLLM testbench |
|---|---|---|---|
| Function | 6/29 (20.7%) | 1,896 s | n/a |
| RefKit, general guide only | 11/29 (37.9%) | 2,338 s | 15/29 (51.7%) |
| RefKit, general guide + detailer | 12/29 (41.4%) | 6,155 s | 17/29 (58.6%) |

Guide only vs function: 5 to 0, p = 0.062. Detailer vs guide only: 1 to 0 (edge_detect), p = 1.0, at 2.6x the time. So the gain over the
function comes from RefKit's timeline and guide, not from the detailer. The detailer's extra translated passes (accu, multi_booth_8bit)
are designs whose references still differ from the golden design (M3). The translator clock-name fix (T2) is applied to both
translated columns (`rtllm-refkit-as-rtl-v2.jsonl`, `rtllm-refkit-all-as-rtl.jsonl`). Not run: detailer without the guide; the
detailer on the 7B.

New issue classes from this run: **clock-generating and multi-clock designs (freq_div, synchronizer) are outside what a one-clock RefKit module can express** (L15); next-state conditions written as "just became" without a stored previous value (L16).

## 7. Related work (from web searches on 2026-10-17; I read only the AutoVeriFix page in full, the rest from search snippets)

See the answer given with this file for the links. Points that bear on the issues above:

- LLM-written **Python reference models as oracles** (AutoBench, CorrectBench, AutoVeriFix, AutoVeriFix+): same idea as RefKit,
  with coverage-guided inputs (85% line coverage in AutoVeriFix) and cycle-accurate trace feedback for state bugs (AutoVeriFix+).
  They report the reference "mostly" correct (94–98.6% functional correctness for GPT-4) and say its errors propagate.
- **Intermediate representations before code** (AoT, QiMeng-CRUX, Spec2RTL-Agent): structure first, then code; relevant to the detailer.
- **Ambiguous specs** (VClare, SpecLens, the RTL error analysis): clarification or behavioural divergence between readings.
- **Contamination** (VeriContaminated, LiveVerilogEval): M1.
- **Candidate ranking by behavioural agreement** (VRank, VFocus; CorrectHDL uses an HLS reference): the arbiter's relatives.
- **DSLs are not easier for LLMs** (ReChisel, ChiseLLM): baseline models are weaker at Chisel than at Verilog until trained or
  given a reflection loop with compiler and simulation feedback.

## 8. Order of work after the RTLLM run

1. Read the finished RTLLM results against section 6; do not tune on them (M7).
2. Whole dev half (37 problems), same settings in every arm: function, old RefKit, general guide, + detailer. (M5)
3. Rewrite or mutate problems for a clean held-out set; audit descriptions (M1, M2).
4. Stimulus that reaches counter limits (M4); verify the mismatch labels (M6).
5. Guide: result registers when idle (L6), pipeline latency (L7), special cases (L9), memory pointers (L8).
6. Translator: chained comparison, branch-merged locals (T1).
7. Repair: executable `examples=` from an agreed worked example (P3); AutoVeriFix-style trace feedback (P1).
8. Decide what to commit, and the RefKit pin in BARG (I2, I3).


## 9. VerilogEval dev half, three arms, same settings (2026-10-18)

27B qwen3.8, t=0.2, one sample, original prompts, 37 dev problems, guide with the added general rules (held-out hygiene: RTLLM is no longer held out for this guide).

| Arm | Agrees with golden | Time |
|---|---|---|
| Function | 23/37 (62.2%) | 1,703 s |
| RefKit, guide only | 26/37 (70.3%) | 2,798 s |
| RefKit, guide + detailer | 26/37 (70.3%) | 5,368 s |

- RefKit (guide only) vs function: 8 to 5, p = 0.58. Detailer vs guide only: 2 to 2, p = 1.0 at 1.9x the time.
- RefKit-only wins: dff8p, counter_2bc, lfsr5, fsm1, circuit9, fsm3, ece241_2013_q8, 2014_q3c. Function-only: review2015_shiftcount, lemmings1, count_clock, 2013_q2afsm, review2015_fsm.
- Wrong in all three: ece241_2014_q5a, fsm_serial, fsm_serialdata, gshare, lemmings4.
- Results: bench/results/dev37-{function,refkit-all,refkit-detail}-27b.jsonl in BARG.


## 10. Detailer dropped; repair, arbiter and translator added (2026-10-18)

The detailer measured no gain on either benchmark (RTLLM 12 vs 11, dev 26 vs 26, 1.9 to 2.6x the time) and is removed from BARG
(`refkit-detail` arm, `detail` option, its prompts and tests). `docs/agent-route.md` stays as a record of the design.

New, all blind to the RTL (BARG `packages/backend/src/pipeline/repair.ts`, `arbiter.ts`; harness `reference-run`, `reference-divergence`):

1. **Repair, one model per track.** Each track's code (a Python function; a RefKit module) is run on the first 24 cycles of the stimulus.
   The repairer of that track sees the description, the code and the outputs it gave, names the rows that contradict the description and
   rewrites the code, or answers NO CHANGE (up to 2 rounds). A rewrite replaces the code only if it passes the same gate.
2. **Arbiter.** When the two references differ, the shortest input sequence that shows it is found (cut at the first difference, then
   shortened) and the arbiter is shown the description and that sequence with both readings' outputs, not the code. Two calls, readings
   swapped; the function wins only if both calls name it, else RefKit (it can be proven and translated).
3. **Translator.** A winning function is translated to RefKit by a model told to follow the function, not the description; the result is
   checked against the function by running both (no RTL), with one retry shown the sequence that differs. If it does not match, the function
   stays the reference and cannot be translated to Verilog.

Not an ambiguity but a model failure, so not edited: lemmings4 (walking direction forgotten across a fall), lemmings1, count_clock.
Clarified in `~/datasets/verilog-eval/dataset_spec-to-rtl-clarified` (see its CHANGES.md): review2015_shiftcount, review2015_fsm,
2013_q2bfsm, on top of the four from 2026-10-17. Dev half only; the test half is not audited.


## 11. Dev half with repair, arbiter and translator (2026-10-18)

Same 37 problems, 27B t=0.2, original prompts. The function and RefKit references are the ones of section 9 (dev37-*-27b.jsonl); repair
(patch edits, up to 2 rounds, one repairer per track), arbiter and translator ran on top of them (dev37-arbiter-27b.jsonl, 2,630 s).

| Stage | Agrees with golden |
|---|---|
| Original function | 23/37 (62.2%) |
| Original RefKit | 26/37 (70.3%) |
| Function after repair | 24/37 (64.9%) |
| RefKit after repair | 25/37 (67.6%) |
| Arbiter final (repaired references) | 28/37 (75.7%) |

- Final vs original RefKit: +3 (count_clock, 2013_q2afsm, review2015_fsm), -1 (circuit9), p = 0.625. Vs original function: +7, -2, p = 0.18.
- Repair: fixed lfsr5 (function); broke circuit9 (RefKit, two rounds of edits); changed count_clock (RefKit) and lemmings4 without a score change.
- Arbiter: 18 agree, 12 decided (8 right, 1 wrong where the other reading was right: lemmings1), 4 default, 3 one reference only.
  Function winners translated to RefKit and matching the function on 128 vectors: count_clock, 2013_q2afsm, review2015_fsm. lemmings4: function won, translation did not match, stayed a function.
- Translated to Verilog and scored with the testbench: 23/37 pass, 6 fail, 8 not translated. Five references that agree with the golden design
  are not delivered as Verilog because the translator lacks a construct: step mode (dff8p), `.set_slice` (dff16e), a local that differs between
  branches (count_clock), a `for` over something that is not `range(...)` (conwaylife), a value that is not a compile-time one (review2015_fsm).
  Closing those five is the next translator work (T1 and the list of translator gaps).
- Not significant. Repair is net zero (one fixed, one broken), so on this sample the gain comes from the arbiter plus translator.


## 12. Follow-ups built after section 11 (2026-10-18)

1. **Translator gaps closed** (`src/refkit/verilog.py`, 48 tests, including cycle-by-cycle co-simulation of the translated Verilog against
   RefKit with Icarus Verilog): `x in (a, b)` / `not in`, chained comparisons, `for` over a list or tuple of constants and a `continue`
   known at translation time, conditions known at translation time, a local that is a Python number on one path and an expression on
   the other, `.set_slice` and `.set_bit`, a state register assigned to itself, and falling-edge modules (`always @(negedge clk)`; the
   proof is not run for them, and rising plus falling edges in one module, dual-edge blocks and latches are still refused). A running
   total joined over many branches is held in a wire; its text used to double at every join. Delivered as Verilog on the dev half:
   23/37 -> 27/37 (dff8p, dff16e, conwaylife, review2015_fsm). Still not translated: a memory cleared by an asynchronous reset (gshare).
2. **count_clock** translates and the Verilog matches RefKit, but fails the testbench: the reference toggles pm when the hour goes 12 -> 1,
   the golden design toggles it at 11 -> 12. The 128-step stimulus never reaches an hour rollover, so the reference "agreed" with the
   golden design. This is the stimulus-coverage issue (coverage-guided stimulus, section 8), not a translator bug.
3. **Repairer now has to cite a contradiction** (`pipeline/repair.ts`): an edit counts only if the answer names a cycle and output of the
   run, the value the code gives there (checked against the run) and a different value the description requires. No word of the
   description is looked at.
4. **Repair after the proof** (`workers/worker.ts`, option `maxProofRepairs`, default 2): a refuted proof is a counterexample the stimulus
   missed. The RTL (never the reference) is repaired, shown the sequence with the reference's outputs and its own; the simulation checks
   and the proof run again; a repair that fails them or whose proof is neither proven nor refuted is not kept. End-to-end test: a 32-bit
   inverter wrong only at 0xDEADBEEF is refuted, repaired and then verified.


## 13. Results of the follow-ups (2026-10-18)

Dev half, 37 problems, 27B t=0.2, original prompts. Second run of repair + arbiter + translator (patch edits that must cite a
contradiction that holds in the run, repair calls with thinking off, new translator); compared with the first run (section 11).

| Stage | Agrees with golden |
|---|---|
| Original function | 23/37 (62.2%) |
| Original RefKit | 26/37 (70.3%) |
| Function after repair | 23/37 (62.2%) |
| RefKit after repair | 27/37 (73.0%) |
| Arbiter final | 27/37 (73.0%) (first run: 28/37) |
| Delivered as Verilog, passing the testbench | 26/37 (70.3%) (first run: 27/37) |

- Final vs original RefKit: +2 (count_clock, 2013_q2afsm), -1 (circuit9), p = 1.0. Vs original function: +7, -3, p = 0.34. Vs the first run: -1 (review2015_fsm), p = 1.0.
- Repair: fixed fsm3 (function), count_clock and 2013_q2afsm (RefKit); broke circuit9 (RefKit) and lemmings1 (function): +3 -2. 17 repairs changed code. No edit was ignored for lacking a holding contradiction: the citation requirement never blocked anything on this set.
- Arbiter: 22 agree, 7 decided (4 right), 5 default, 3 one reference only. The function won once (lemmings4) and its translation did not match.
- **review2015_fsm** is the difference from the first run. There the RefKit reference differed from the (right) function on the stimulus, so the arbiter chose the function. This time the two agreed on the 128-vector stimulus, so the arbiter was never asked and the wrong RefKit reference stayed. The same stimulus-coverage limit as count_clock (section 12): two readings that differ only after a long run look equal. Coverage-guided stimulus (section 8) is the fix for the divergence check as well as for the differential.
- Time: 5,252 s against 2,630 s for the first run, with thinking off for repair, because repair now makes more and longer calls.
- count_clock is right as a reference after repair but its Verilog fails the testbench (the hour rollover), as noted in section 12.
- Clarified prompts (7 problems, section 9 and `~/datasets/verilog-eval/dataset_spec-to-rtl-clarified/CHANGES.md`): function 2/7 -> 3/7, RefKit 0/7 -> 4/7, arbiter final 1/7 -> 3/7, Verilog 1/7 -> 2/7. Not comparable with published VerilogEval.
- Rating by the keep-or-drop rule (fixed minus broke at least +2, time under 2x): the arbiter and translator are unproven (+2/-1 against always taking RefKit, and they did not reproduce the first run's translated wins); repair is net +1 on the final but +3/-2 as a stage and doubled the time, so it is not kept as it is; the translator gaps (+4 delivered Verilog) are kept.


## 14. Frozen configuration for the single run of the untouched test half (2026-10-18, written before the run)

Changes since section 13: (1) coverage-guided stimulus: for a clocked block with a reference, the stimulus gains `deep_runs` (each one-bit
input held at 1 for up to 4,096 cycles after a reset, then all of them, then a bursty segment, sized by how fast the reference runs; harness
`archetypes.deep_runs`), and the divergence check shrinks with chunked deletion so a difference after thousands of cycles is still found;
(2) the repair stage is removed (module, tests, harness command, the per-call `think` switch it needed). The post-proof RTL repair stays.
Re-scoring the dev references on the deeper stimulus (no model calls): function 23 -> 22 (count_clock now wrong, as the testbench says),
RefKit 26 -> 26, arbiter final of the second run 27 -> 26.

Frozen for the test run: the guide as it is; 27B qwen3.8 at t=0.2, one sample; the function reference (BARG's), the RefKit reference (guide
only, all patterns), the arbiter (two swapped calls, function wins only if both name it) and the translator; no repair; the deep stimulus.
Original prompts, not the clarified copy. The test half is the odd positions of the 74 clocked VerilogEval problems (37). It is run once and
reported whatever it shows; nothing is changed afterwards. Reported: reference agrees with the golden design (function, RefKit, arbiter
final), delivered Verilog passing the testbench (RefKit alone, arbiter final), time; sign tests (exact, two-sided) of the arbiter final
against the function and against RefKit alone. Not a claim: any result on 37 problems and one sample is a few problems either way.


## 15. The single untouched run: VerilogEval test half, 37 problems (2026-10-18)

Run once with the configuration frozen in section 14, on the odd-position clocked problems, original prompts, 27B qwen3.8 at t=0.2, one sample,
the deep stimulus. Nothing was changed after seeing it.

| Series | Agrees with the golden design |
|---|---|
| Function | 21/37 (56.8%) |
| RefKit alone | 22/37 (59.5%) |
| Arbiter final (arbiter + translator, no repair) | 21/37 (56.8%) |
| Delivered as Verilog passing the testbench: RefKit alone | 21/37 (56.8%) |
| Delivered as Verilog passing the testbench: arbiter final | 19/37 (51.4%) |

- Exact two-sided sign tests: arbiter final vs function 3 to 3 (dualedge, circuit7, shift18 against edgecapture, fsm_ps2, fsm_ps2data), p = 1.0; arbiter final vs RefKit alone 1 to 2 (countbcd against shift4, lemmings2), p = 1.0; RefKit alone vs function 5 to 4, p = 1.0. No difference is significant.
- Times: function 1,525 s, RefKit 3,155 s, arbiter and translator stage 2,042 s.
- Arbiter: 18 agree, 9 default to RefKit, 2 one reference only, 5 decided, 3 no usable reference. Of its 5 decisions, none picked a reference that agrees with the golden design: shift4 and lemmings2 picked the wrong one although the other was right (the function won, its translation did not match, so the wrong function stayed the reference), and fsm_hdlc, ece241_2013_q4 and 2014_q3fsm had both wrong. The only translated function win that helped was countbcd (the function was the only usable reference).
- Verilog: the arbiter route delivers fewer (19 against 21) because a function that stays a function cannot be translated, and several right references use constructs the translator refuses (countbcd: and/or whose value is a multi-bit number; dualedge: rising and falling edges in one module; ece241_2014_q5b: some bits of an output not set on every path).
- Wrong in all three: edgedetect2, edgedetect, ece241_2014_q4, rule110, 2014_q3fsm, fsm_hdlc, circuit8, circuit10, ece241_2013_q4, lemmings3, review2015_fancytimer.
- Reading: on the test half RefKit is not better than the function by more than noise (the dev half showed +4), and the arbiter route does not improve on RefKit alone. By the rule of section 14 (keep a change only if fixed minus broke is at least +2 with the time under 2x) the arbiter and translator are not kept as a gain: +1 -2 against RefKit alone on 37 problems, at an extra 2,042 s. The dev-half lead of RefKit over the function (26 against 22 on the deep stimulus) did not reproduce at that size on the untouched half, which fits a guide tuned on dev.

## 16. Translator: x-pessimism and constructs it refused (2026-10-30, branch `translator-fixes`)

From the CVDP spec audit in BARG (`docs/cvdp-spec-audit.md`): 4 of the 27 audited problems had logically right references that
failed only because of x, and 9 had references the translator refused.

- **T3. X-pessimism of the translation. fixed (two-state inputs, the default).** The translation is made of `?:` and comparisons,
  so an input a testbench leaves undriven (z, read as x) turned into x in every register it reached (CVDP: `sel` of
  clock_divider_0003, `data_valid` of data_width_converter_0003, the unselected `select_*` of events_to_apb_0001, `start` of
  factorial_0001), where hand-written `if`/`case` RTL takes a branch. The translated module now reads every input through a
  function `__rk2s_W(v)` that turns each x or z bit into 0. Why this and not `if`/`case` code: RefKit has no x; its values are
  integers, its runs start every input at 0 (`run_stepped`, `cosimulate`'s testbench) and the BARG harness gives an input
  nobody sets 0. Mapping x/z to 0 is the IEEE 1800 conversion of a four-state value to a two-state one (6.24.1), so the module
  is the reference's own two-state function, applied to the two-state reading of its ports. Procedural `if`/`case` would only
  copy the x-optimism of hand-written RTL (`if (x)` takes the else branch, which is neither 0 nor 1 semantics: `if (sel == 0)`
  is false for x while RefKit's sel = 0 makes it true). The function is applied where the input is read (no `assign` to a
  sanitised wire), so it adds no delta cycle: a register samples the port itself, and races with a testbench are what they
  were. The clock and the asynchronous reset in the sensitivity list stay raw; the reset level test reads the two-state value.
  Formal: for two-state inputs (SymbiYosys) the function is the identity, so the miter proves the same thing; tested (a right
  RTL proven, a wrong one refuted). `translate(..., two_state_inputs=False)` gives the old four-state reading.
  Measured: of the stored references of the four problems that translate and are clocked, 201 showed x on an output 16
  cycles after reset with every other input undriven, 0 do with the fix (Icarus, read-only on the BARG work folders); the
  CVDP scoring itself (cocotb in docker) was not rerun here. What it does not do: an x that the testbench drives explicitly
  also reads 0, so a test that checks that x propagates would see a difference (none known).
