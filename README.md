# refkit

Reference models of hardware blocks, **written like Verilog, in Python**.

- `refkit.core` (imported as `refkit`): the language. `Module`, `Reg`, `State`, `Mem`, `Wire`, `@seq`, `@comb`, `@latch`,
  `Bits` with Verilog's width rules (and `shift_in_low` / `shift_in_high`, which name where a shifted bit enters), run cycle by cycle (`run`, `run_stepped`), compared exactly (`compare`), walked through every
  transition (`transition_walk`).
- `refkit.verilog`: translates a RefKit module into Verilog and proves an RTL module equivalent to it with SymbiYosys
  (`translate`, `prove`).
- `refkit/guide.md`: the guide a language model is taught from (a core that is always given, and a cookbook of patterns).
  `refkit.guide_text()` returns it. Every module example in it is executed by the tests.
- `docs/design.md`: design notes written inside BARG, where this came from (the paths and command names there refer to BARG).

```python
from refkit import Module, Reg, seq, comb, run

class Ref(Module):
    count = Reg(4)
    @seq
    def tick(self):
        if self.en:
            self.count.next = self.count + 1
    @comb
    def out(self):
        self.q = self.count

run(Ref, [{"en": 1}] * 3, widths={"q": 4}, inputs={"en": 1})   # [{'q': 0}, {'q': 1}, {'q': 2}]
```

Tests: `pip install -e '.[test]' && pytest`. The proof needs `sby`, `yosys` and `yices-smt2` (OSS CAD Suite); they are found on
PATH, in `$REFKIT_TOOLS`, in `$OSS_CAD_SUITE/bin` or in `~/tools/oss-cad-suite/bin`, and the proof tests are skipped without them.

Used by BARG (UGRA-IITBHU/BARG), which pins a commit of this repository.
