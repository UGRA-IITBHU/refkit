<!-- CORE: always given. The sections after the COOKBOOK marker below are patterns the model picks from (see pipeline/refkit.ts). -->
# RefKit: the reference of a block, written like Verilog, in Python

RefKit is a small Python library that behaves like a Verilog module. You write the block the way you would write its Verilog
(registers, `always @(posedge clk)` blocks, `assign` statements), and RefKit runs it clock cycle by clock cycle with Verilog's
rules: non-blocking assignments, bit widths that wrap, the reset applied for you. You never write the loop over cycles, never
handle the ordinary reset, and never mask a value to a width: RefKit does those.

Everything you can use is listed in **API** below. Nothing else exists: do not invent functions, methods or arguments.

Start every reference with `from refkit import *`, which imports all of these:

```python
from refkit import Module, Reg, State, Wire, Mem, Input, Output, Instance, seq, comb, latch, B, cat, mux, casez, clog2
```

## The shape of a reference

```python
from refkit import *

class Ref(Module):
    count = Reg(4)              # reg [3:0] count;          reset value 0
    load  = Reg(1, reset=1)     # reg load;                 reset value 1
    hit   = Wire(1)             # wire hit;                 an internal signal set in a @comb block

    @comb                       # assign ... / always @(*)
    def decode(self):
        self.hit = self.count == 9
        self.q = self.count     # q is an output port of the block
        self.done = self.hit

    @seq                        # always @(posedge clk)
    def counter(self):
        if self.hit:
            self.count.next = 0                  # count <= 0;
        elif self.en:
            self.count.next = self.count + 1     # count <= count + 1;
```

- One class, subclassing `Module`. Its name does not matter.
- **Declarations** are class attributes: `Reg`, `State`, `Mem` (things that hold a value across clock edges) and `Wire`
  (internal combinational signals). Output ports and input ports are **not** declared: they come from the block's interface.
- **`@comb` blocks** compute wires and output ports from inputs, registers and earlier wires: `self.y = expression`. They run
  every cycle in the order they are written, so a block that reads a wire must come after the block that sets it.
- **`@seq` blocks** run at the rising clock edge. They assign registers with `self.r.next = value` (Verilog `r <= value`) and
  memories with `self.mem.write(addr, value)`. They can read inputs, registers, wires and outputs of the cycle.
- Inside a block, `self.name` is: an input port, a register (its value now), a wire or an output already set. A port named like a
  Python keyword (`in`) is `self.in_`.
- Every output port must be set by a @comb block in every cycle, **unless** a `Reg` has the output's name: then the output is that
  register (Verilog `output reg q`). **Do not also assign it in @comb** (`self.q = ...`): the register already is the output;
  give it its value with `self.q.next = ...` in @seq.

## The timeline (get this right and the rest follows)

```
cycle:            0          1          2          3
inputs:           in0        in1        in2        in3
registers:        reset      R1         R2         R3       R1 = what the @seq blocks assigned with in0 and the reset values
outputs:          out0       out1       out2       out3     out_i = the @comb blocks, run with in_i and the registers of cycle i
```

- The outputs of a cycle are read **before** that cycle's clock edge.
- A register assigned in `@seq` during cycle *i* shows its new value from cycle *i+1* on. So **anything that goes through a
  register appears one cycle after the inputs that caused it**.
- A `@comb` output follows the inputs **in the same cycle**.
- All `.next` assignments of all `@seq` blocks happen together at the edge: every `@seq` block reads the values from before the
  edge, even if another block (or a line above) has already assigned `.next`. `self.a.next = self.b; self.b.next = self.a` swaps.
- If a @seq block assigns the same register twice in one edge, the last assignment wins (as in Verilog).

How to decide for each output:

| The description says | Write |
|---|---|
| "output reg", "registered", "updated on the clock edge", "the cycle after", "one cycle later", "delayed by one clock" | a `Reg`, assigned in `@seq`; the output is the register |
| "combinational", "assign", "as soon as", "in the same cycle", "while x is high, y is ..." | set it in `@comb` from the inputs (and registers) |
| "Moore machine": outputs depend on the state only | set it in `@comb` from the state register only, never from inputs |
| "Mealy machine": outputs depend on the state and the inputs | set it in `@comb` from the state and the inputs |

## Reset

The harness tells RefKit which input is the reset, whether it is active high or low, and whether it is synchronous or asynchronous.
RefKit puts **every** register (and every memory word) back to its declared reset value:

- synchronous: at the clock edge of a cycle in which the reset is asserted (that cycle still shows the old values);
- asynchronous: at once (the cycle in which it is asserted already shows the reset values), and for as long as it is asserted.

So **do not** write `if self.reset:` for the ordinary reset. Give each register its reset value in the declaration
(`Reg(4, reset=8)`, `State("IDLE", "RUN", reset="IDLE")`). Only when the description says the reset does something that is not
"every register to its reset value" (for example, it loads an input into a register) write that in a `@seq` block; RefKit's reset
still acts first.

Registers start at their reset values before the first cycle.

## Other edges, dual-edge flip-flops and latches

`@seq` is `always @(posedge clk)`. For anything else:

| Verilog | RefKit |
|---|---|
| `always @(posedge clk)` | `@seq` |
| `always @(negedge clk)` | `@seq(edge="neg")` |
| `always @(posedge clk or negedge clk)` (dual-edge flip-flop) | `@seq(edge="both")` |
| a latch: `always @(*) if (en) q = d;` (transparent while `en` is 1, holds otherwise) | `@latch`, assigning `self.q.next = self.d` under the condition |

A module that uses one of these is run **step by step**: the clock is then an input like the others (`self.clk` can be read), and
each step the clock changes first, the other inputs just after. An edge between two steps runs the blocks of that edge with the
inputs of the step before; a `@latch` block runs at every step with the current inputs and its assignments take effect at once
(that is what "transparent" means); when it assigns nothing, the register holds. Do not model the clock yourself and do not
compare it with its previous value: the decorators do that.

## Case statements

Verilog `case` is Python's `match` (compare a number with `int(...)`, a state with its name):

```python
match int(self.op):          # case (op)
    case 0:                  #   2'd0: y = a + b;
        self.y = self.a + self.b
    case 1 | 2:              #   2'd1, 2'd2: y = a - b;
        self.y = self.a - self.b
    case _:                  #   default: y = 0;
        self.y = 0
```

- Always write the `case _:` default when the description has one, and when not every value is listed (in a `@comb` block, an
  output not set in some branch is an error: RefKit does not infer a latch).
- `casez` / `casex` (`4'b1??0`): use `if casez(self.op, "1??0"): ... elif casez(self.op, "01??"): ...`, in the order of the
  Verilog items (the first match wins). `?`, `x` and `z` mean any bit; the pattern is written most significant bit first.
- A state machine: `match self.st.value:` with `case "IDLE":` ..., or an `if / elif` chain on `self.st == "IDLE"`.

## Values and Verilog's widths

Every input, register, wire and output is a sized value (`Bits`). Operators follow Verilog exactly, including how wide a
computation is:

- **Arithmetic and bitwise operators** (`+ - * // % ** & | ^ ~`, unary `-`) are computed at the width of their context, as in
  Verilog: in an assignment, the larger of the destination's width and the widest operand. So
  `self.sum9 = self.a + self.b` (8-bit `a`, `b`, 9-bit `sum9`) keeps the carry, exactly like `assign sum9 = a + b;`, and an 8-bit
  destination drops it. `//` and `/` are both Verilog `/` (integer division).
- **A plain Python integer is a Verilog unsized constant: 32 bits.** `self.count + 1 == 16` is true for a 4-bit count at 15, as in
  Verilog. Use `B(value, width)` (Verilog `width'dvalue`) when the width matters, e.g. in `cat()`.
- **Comparisons** (`== != < <= > >=`) compute both sides at the wider side's width and give `True`/`False`. They are unsigned.
  `~self.c == 0` is never true for a 4-bit `c`, just as in Verilog (`~c` is extended to 32 bits first); write `self.c == 15` or
  `self.c.reduce_and()`.
- **Shifts** `<< >>` keep the left operand's width (in an assignment, the context width); `v.sra(n)` is `>>>` on a signed value.
- **Selects**: `v[7:4]` (high bit first), `v[3]`, `v.part(base, w)` (`v[base +: w]`), `v.part_down(base, w)` (`v[base -: w]`).
- **Concatenation and replication**: `cat(a, b, c)` is `{a, b, c}` (the first part is the most significant); every part must be
  sized, so write constants as `B(0, 1)`. `v.rep(n)` is `{n{v}}`.
- **Signed**: values are unsigned, like Verilog without `signed`. For `$signed` behaviour: `v.signed` (the number), `a.slt(b)`,
  `a.sle(b)`, `a.sgt(b)`, `a.sge(b)` (signed comparisons), `a.sdiv(b)`, `a.smod(b)`, `a.smul(b)`, `v.sext(w)`, `v.sra(n)`.
- **Logical operators**: Verilog `&&`, `||`, `!` are Python `and`, `or`, `not`; `c ? a : b` is `mux(c, a, b)` (or Python
  `a if c else b`).
- Assigning to a register, wire or output truncates or zero-extends to its width; `True`/`False` assign as 1/0.
- `int(v)` is the number (use it to index a Python list or a range); `if v:` is true when `v` is not zero.

## Verilog to RefKit, construct by construct

| Verilog | RefKit |
|---|---|
| `module m (input [7:0] a, output [7:0] y);` | `class Ref(Module):` (the ports come from the interface) |
| `parameter W = 8;` / `localparam` | a class attribute `W = 8`, read as `self.W` |
| `reg [W-1:0] r;` | `r = Reg(W)` |
| `reg [3:0] r = 4'd5;` / `initial r = 5;` | `r = Reg(4, init=5)` (value at power-up) |
| a register the reset does not touch | `r = Reg(4, reset=None)` |
| `reg [7:0] mem [0:15];` | `mem = Mem(16, 8)`; read `self.mem[i]`; write `self.mem.write(i, v)` in `@seq` |
| `wire [3:0] w; assign w = ...;` | `w = Wire(4)`; `self.w = ...` in `@comb` |
| `assign y = expr;` / `always @(*)` | `@comb` block: `self.y = expr` |
| blocking `=` inside `always @(*)` (a temporary) | a Python local variable: `t = self.a ^ self.b` |
| `always @(posedge clk)` | `@seq` |
| `always @(negedge clk)` | `@seq(edge="neg")` |
| `always @(posedge clk or negedge clk)` | `@seq(edge="both")` |
| `always @(posedge clk or posedge rst) if (rst) ...` | `@seq`; the reset is applied by RefKit (asynchronous, as the interface says) |
| a latch: `always @(*) if (en) q = d;` | `@latch` with `self.q.next = self.d` under the condition |
| `r <= v;` | `self.r.next = v` |
| `r[3] <= 1'b1;` / `r[7:4] <= v;` | `self.r.next_bit(3, 1)` / `self.r.next_slice(7, 4, v)` |
| `if / else if / else` | `if / elif / else` |
| `case (s) 0: ...; 1, 2: ...; default: ...; endcase` | `match int(self.s): case 0: ... case 1 | 2: ... case _: ...` |
| `casez (op) 4'b1??0: ...` / `casex` | `if casez(self.op, "1??0"): ... elif ...` |
| `for (i = 0; i < 8; i = i + 1)` / `generate for` | a Python `for i in range(8):` |
| `function [7:0] f(input [7:0] x); ... endfunction` | a plain method `def f(self, x): return ...`, called `self.f(v)` |
| `Child u1 (.a(x), .y(w));` | `u1 = Instance(Child)`; in `@comb`: `o = self.u1(a=self.x)`, then `o.y` |
| `$clog2(n)` | `clog2(n)` |
| `{a, b}` / `{4{a}}` | `cat(a, b)` / `a.rep(4)` |
| `&v`, `|v`, `^v`, `~&v`, `~|v`, `~^v` | `v.reduce_and()`, `.reduce_or()`, `.reduce_xor()`, `.reduce_nand()`, `.reduce_nor()`, `.reduce_xnor()` |
| `a ~^ b` | `a.xnor(b)` |
| `$readmemh` contents of the block | `self.memory[i]` (only when the prompt says the contents come from a file) |
| `x` / "don't care" output | `None` (only where the description says the value does not matter) |

Not modelled (they do not occur in a reference): delays (`#5`), `$display`, tri-state `z` on outputs, four-valued `x`
arithmetic. A child module used as an `Instance` declares its ports with `Input(width)` and `Output(width)`.

## API (complete: nothing else exists)

| Name | What it is |
|---|---|
| `Module` | base class of the reference |
| `Reg(width, reset=0, init=None)` | a register; `self.r` reads it; `self.r.next = v`, `self.r.next_bit(i, b)`, `self.r.next_slice(hi, lo, v)` assign it at the edge; `reset=None`: the reset does not touch it; `init`: power-up value |
| `State("A", "B", ..., reset="A")` | a state register holding one of the names; reset defaults to the first; `self.st == "A"`; `self.st.next = "B"`; `self.st.value` (the name, for `match`) |
| `Mem(depth, width, reset=0)` | `depth` words; `self.m[addr]` reads; `self.m.write(addr, v)` writes at the edge; `len(self.m)`; `reset=None`: not cleared by the reset |
| `Wire(width)` | an internal combinational signal, set in `@comb` with `self.w = v` |
| `Input(width)`, `Output(width)` | port declarations, needed only in a child module used as an `Instance` |
| `Instance(ChildModule)` | a submodule; call `self.u(port=value, ...)` in `@comb`; read outputs from the result |
| `@comb` | an `assign` / `always @(*)` block |
| `@seq`, `@seq(edge="neg")`, `@seq(edge="both")` | an `always @(posedge clk)`, `@(negedge clk)`, dual-edge block |
| `@latch` | a level-sensitive block; its `.next` assignments take effect at once |
| `B(value, width)` | a sized constant |
| `cat(a, b, ...)`, `mux(c, a, b)`, `casez(v, "1?0")`, `clog2(n)` | concatenation, `?:`, casez matching, `$clog2` |
| `v[hi:lo]`, `v[i]`, `v.part(base, w)`, `v.part_down(base, w)` | selects |
| `v.zext(w)`, `v.sext(w)`, `v.rep(n)`, `v.set_bit(i, b)`, `v.set_slice(hi, lo, x)` | resize, replicate, copies with bits replaced |
| `v.signed`, `v.slt/sle/sgt/sge(o)`, `v.sdiv/smod/smul(o)`, `v.sra(n)` | signed meaning, comparisons and arithmetic |
| `v.reduce_and()` ... `v.reduce_xnor()`, `a.xnor(b)` | reductions, `~^` |
| `v.width`, `int(v)` | the width; the number |
| `examples = [...]` | optional: cycle-by-cycle values the description gives (pattern "Waveforms and tables of values") |

Errors RefKit reports, so you know what is not allowed: assigning a register with `self.r = v` (use `.next`), `.next` outside
`@seq`/`@latch`, setting an output or wire inside `@seq`, reading a wire before the block that sets it, a name that is not an
input, register, wire, output, parameter or function, an output port no block sets, `v[3:7]` (write the high bit first), `cat` of
a plain integer, a negedge/dual-edge/latch module run without step mode.

## Combinational blocks

A block with no clock is written with `@comb` blocks only, no registers: RefKit evaluates it once per input vector.

```python
class Ref(Module):
    @comb
    def alu(self):
        match int(self.op):
            case 0:
                self.y = self.a + self.b
            case 1:
                self.y = self.a - self.b
            case _:
                self.y = self.a & self.b
        self.zero = self.y == 0
```

## Checklist before you answer

1. Every value the description says is stored or remembered (a register, a counter, a state, a previous value, a shift register,
   a memory) is declared, with the width and reset value the description gives.
2. For each output: same cycle (`@comb`) or one cycle later (a register)? Check it against the timeline and the table above.
3. For a state machine: every state the description names, every transition in the order the description gives it, and what
   happens when no condition holds (normally: stay).
4. Counters: the value they start from, the value they stop or wrap at, and in which cycle an output that depends on them is high.
   Count on paper: write out cycles 0, 1, 2, 3 of a short input sequence and the values you expect.
5. No `if self.reset` for the ordinary reset.
6. Every output port set in every cycle (or a `Reg` with its name).

<!-- COOKBOOK -->

## Pattern: counter (enable, wrap, terminal count, load)

Use when: the block counts cycles or events, possibly with enable, load, a maximum, or an output when it reaches a value.

```python
class Ref(Module):
    count = Reg(4)

    @seq
    def step(self):
        if self.load:
            self.count.next = self.d               # a load wins over counting
        elif self.en:
            if self.count == 9:
                self.count.next = 0                # a decade counter: 0..9, then back to 0
            else:
                self.count.next = self.count + 1

    @comb
    def out(self):
        self.q = self.count
        self.tc = self.en and self.count == 9      # high in the cycle in which the count is 9 and enabled
```

- A counter that wraps at its width needs no comparison: a 4-bit `count + 1` goes from 15 to 0 by itself.
- Down counters: `self.count - 1` goes from 0 to the maximum by itself.
- Decide whether "done"/"terminal count" is high **while** the count has its last value (`@comb`, as above) or **in the cycle
  after** it reached it (a `Reg`).

## Pattern: state machine (Moore)

Use when: the description gives states and transitions, and the outputs depend on the state only.

```python
class Ref(Module):
    st = State("IDLE", "ARMED", "FIRE")            # IDLE is the reset state

    @seq
    def next_state(self):
        if self.st == "IDLE":
            if self.go:
                self.st.next = "ARMED"            # no else: it stays in IDLE
        elif self.st == "ARMED":
            self.st.next = "FIRE" if self.go else "IDLE"
        elif self.st == "FIRE":
            self.st.next = "IDLE"

    @comb
    def outputs(self):
        self.pulse = self.st == "FIRE"            # Moore: from the state only
```

- Keep the description's state names. Include every state it names.
- A state table "current state, input -> next state, output" maps row by row onto the `if` chain.
- If the description numbers the states with an encoding and an output **is** the state code, use a `Reg` of that width with the
  codes, not `State`.

## Pattern: state machine (Mealy)

Use when: an output depends on the current state **and** the current input ("output z = 1 when in state B and x = 1").

```python
class Ref(Module):
    st = State("A", "B")

    @seq
    def next_state(self):
        if self.st == "A":
            self.st.next = "B" if self.x else "A"
        else:
            self.st.next = "B" if self.x else "A"

    @comb
    def outputs(self):
        self.z = self.st == "B" and self.x        # same cycle as the input
```

## Pattern: sequence detector

Use when: the block raises an output when a given pattern of input bits has been seen.

- Decide from the description: does the output come in the same cycle as the last bit of the pattern (Mealy, `@comb` using the
  input) or in the cycle after (Moore: a state "FOUND" entered on the last bit)?
- Overlapping: after a match, the bits already seen can be the start of the next match. Unless the description says the
  detector starts over, keep the longest suffix of what was seen that is still a prefix of the pattern. A shift register of the
  last N bits is often the simplest and clearest model:

```python
class Ref(Module):
    last = Reg(3)                                  # the last three input bits, newest in bit 0

    @seq
    def shift(self):
        self.last.next = cat(self.last[1:0], self.x)

    @comb
    def detect(self):
        self.found = cat(self.last[1:0], self.x) == 0b101    # Mealy: the current bit completes "101"
```

## Pattern: shift register and serial-to-parallel

Use when: bits move one position per clock, or a value is collected bit by bit.

```python
class Ref(Module):
    sr = Reg(8)

    @seq
    def shift(self):
        if self.en:
            self.sr.next = cat(self.sr[6:0], self.din)    # shift left: old bits move up one, the new bit enters at bit 0
            # shift right, new bit in at the top:  cat(self.din, self.sr[7:1])

    @comb
    def out(self):
        self.q = self.sr
        self.dout = self.sr[7]
```

- "Least significant bit first": the first bit received ends up in bit 0 of the word, so shift right with the new bit entering at
  the top (`cat(self.din, self.sr[7:1])`), and after 8 bits the first one is in bit 0.

## Pattern: serial receiver with a bit counter

Use when: a machine waits for a start condition, collects N bits, checks a stop condition, then reports.

```python
class Ref(Module):
    st = State("IDLE", "DATA", "STOP", "DONE", "ERR")
    n = Reg(4)
    data = Reg(8)

    @seq
    def fsm(self):
        if self.st == "IDLE":
            if self.rx == 0:                          # start bit
                self.st.next = "DATA"
                self.n.next = 0
        elif self.st == "DATA":
            self.data.next = cat(self.rx, self.data[7:1])     # LSB first
            self.n.next = self.n + 1
            if self.n == 7:
                self.st.next = "STOP"
        elif self.st == "STOP":
            self.st.next = "DONE" if self.rx == 1 else "ERR"
        elif self.st == "DONE":
            self.st.next = "DATA" if self.rx == 0 else "IDLE"   # a start bit right away
            self.n.next = 0
        elif self.st == "ERR":
            if self.rx == 1:
                self.st.next = "IDLE"

    @comb
    def out(self):
        self.done = self.st == "DONE"
        self.byte = self.data
```

- Count the cycles: with `n` from 0 to 7 the DATA state lasts exactly 8 cycles. Write out the cycles of one byte on paper and
  check in which cycle `done` is high.
- What happens in the DONE cycle if a new start bit arrives is part of the description: read it, do not assume.

## Pattern: edge detection and a previous value

Use when: an output reacts to a change of an input (rising edge, falling edge, any change).

```python
class Ref(Module):
    prev = Reg(8)

    @seq
    def remember(self):
        self.prev.next = self.d

    @comb
    def edges(self):
        self.rise = self.d & ~self.prev               # bits that are 1 now and were 0 in the previous cycle
```

- If the description wants the edge flag **one cycle later** (a registered output), make it a `Reg` assigned from the same
  expression in `@seq`: `self.rise_r.next = self.d & ~self.prev`.
- A falling edge is `~self.d & self.prev`, any change is `self.d ^ self.prev`.
- "Captures" or "sticky" bits: set on an edge and held until a reset or a clear: `self.cap.next = self.cap | (edge)`.

## Pattern: pipeline and delay line

Use when: a value appears N cycles after it entered, possibly computed in stages.

```python
class Ref(Module):
    s1 = Reg(8)
    s2 = Reg(8)

    @seq
    def stages(self):
        self.s1.next = self.a + self.b            # stage 1
        self.s2.next = self.s1 * 2                # stage 2 reads stage 1 from before the edge

    @comb
    def out(self):
        self.y = self.s2                          # appears two cycles after a and b
```

- Valid flags travel with the data: give each stage a `vN = Reg(1)` assigned from the previous stage's flag.

## Pattern: memory, register file, FIFO

Use when: the block stores words at addresses, or queues them.

```python
class Ref(Module):
    mem = Mem(16, 8)
    wp = Reg(4)
    rp = Reg(4)
    cnt = Reg(5)

    @seq
    def fifo(self):
        push = self.wr and self.cnt != 16
        pop = self.rd and self.cnt != 0
        if push:
            self.mem.write(self.wp, self.din)
            self.wp.next = self.wp + 1
        if pop:
            self.rp.next = self.rp + 1
        if push and not pop:
            self.cnt.next = self.cnt + 1
        elif pop and not push:
            self.cnt.next = self.cnt - 1

    @comb
    def out(self):
        self.dout = self.mem[self.rp]             # combinational read; a registered read is a Reg assigned in @seq
        self.empty = self.cnt == 0
        self.full = self.cnt == 16
```

- A read in `@comb` gives the word in the same cycle; "the data is available on the next clock" means a `Reg` loaded in `@seq`.
- Writing and reading the same address in one cycle: a `@comb` read sees the old word (the write happens at the edge).

## Pattern: arithmetic with carry, signed values, accumulators

Use when: sums need a carry out, values are signed, or a register accumulates.

```python
class Ref(Module):
    acc = Reg(16)

    @seq
    def add(self):
        if self.clear:
            self.acc.next = 0
        elif self.valid:
            self.acc.next = self.acc + self.x.zext(16)

    @comb
    def out(self):
        s = self.a.zext(9) + self.b.zext(9)        # 9 bits: keeps the carry of two 8-bit numbers
        self.sum = s[7:0]
        self.cout = s[8]
        self.total = self.acc
        self.neg = self.acc.signed < 0
```

- Signed comparison: compare `.signed` values. Sign extension: `self.x.sext(16)`.
- Overflow of a signed add: the operands have the same sign and the result's sign differs.

## Pattern: waveforms and tables of values

Use when: the description shows values cycle by cycle (a timing diagram, a table with one row per clock cycle, a simulation trace).

Copy them into `examples` so RefKit checks your module reproduces them. Each example is `(inputs per cycle, outputs per cycle)`.
Give only the ports the description shows; `None` where it leaves a value blank or marks it `x`. Include the reset input when the
waveform shows it.

```python
class Ref(Module):
    examples = [
        ([{"reset": 1, "en": 0}, {"reset": 0, "en": 1}, {"reset": 0, "en": 1}, {"reset": 0, "en": 0}],
         [{"q": None},           {"q": 0},              {"q": 1},              {"q": 2}]),
    ]
    count = Reg(4)
    ...
```

- Alignment: the row of a cycle gives that cycle's inputs and the outputs read in that cycle, **before** its clock edge. If a
  waveform shows an output changing at the same clock edge as an input, the output is a register and shows the value computed
  from the inputs of the previous cycle.
- If the module does not reproduce the waveform, the module is wrong (or the alignment is): fix it rather than the example.

## Pattern: clock enable, hold, and "otherwise keep its value"

Use when: registers change only under some condition.

- A register not assigned in an edge keeps its value: write nothing for the "hold" case.
- Priority between conditions (reset > load > enable > hold) is the order of an `if / elif` chain, as in Verilog.

## Pattern: one-hot and encoded state outputs

Use when: the output is the state itself, as a one-hot vector or as a binary code the description specifies.

```python
class Ref(Module):
    state = Reg(4, reset=0b0001)                  # one-hot, reset to the first state

    @seq
    def step(self):
        self.state.next = cat(self.state[2:0], self.state[3])    # rotate: A -> B -> C -> D -> A

    @comb
    def out(self):
        self.y = self.state
```

- When the description gives the encoding of each state, use a `Reg` with exactly those codes so the output matches bit for bit.

## Pattern: negedge, dual-edge flip-flops and latches

Use when: the description says a value changes on the falling edge, on both edges of the clock, or is held by a latch.

```python
class Ref(Module):
    pos = Reg(1)
    neg = Reg(1)
    lat = Reg(8)

    @seq                                          # rising edge
    def on_rise(self):
        self.pos.next = self.d

    @seq(edge="neg")                              # falling edge
    def on_fall(self):
        self.neg.next = self.d

    @latch                                        # transparent while en is 1
    def hold(self):
        if self.en:
            self.lat.next = self.data

    @comb
    def out(self):
        self.q = self.pos if self.clk else self.neg     # a dual-edge output: the rising-edge value while clk is high
        self.l = self.lat
```

- A dual-edge flip-flop is simplest as `@seq(edge="both")` assigning one register; the two-register form above is how it is often
  built in Verilog, and both behave the same.
