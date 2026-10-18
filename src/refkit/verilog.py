"""RefKit to Verilog, and a formal proof that an RTL module is equivalent to its RefKit reference.

RefKit evaluates expressions with Verilog's own width rules, so a RefKit module translates into Verilog almost construct for
construct. The translator executes the Python of each block **symbolically**: every local variable, branch, `match`, loop and
helper-method call is followed, and what comes out is, for every wire, output and register, one Verilog expression of the inputs
and registers (branches become `?:`). A RefKit local variable holds an unevaluated expression, so substituting it is exact.

Then a miter instantiates the translated reference and the RTL on the same inputs, assumes the reset in the first cycle, and
asserts that every output agrees in every later cycle; SymbiYosys proves it (k-induction) or finds a counterexample. A
counterexample is replayed in simulation against the reference before it is believed, so a formal artefact (a register with no
reset, which simulation treats as unknown) never condemns the RTL.

The translation is refused (``Unsupported``) for what it does not cover yet: falling-edge and dual-edge blocks, latches,
instances, file-loaded memory, don't-care outputs. The caller then keeps the simulation-based checks.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import core as rk


class Unsupported(Exception):
    """A construct the translator does not cover: no proof is attempted."""


class _Continue(Exception):
    """A `continue` known at translation time (inside an unrolled loop, outside any hardware branch)."""


@dataclass
class E:
    """A Verilog expression with its self-determined width; `boolean` marks a 1-bit truth value (a comparison, `!`, `&&`)."""

    text: str
    width: int
    boolean: bool = False
    #: Contains an operator whose result depends on the evaluation width beyond truncation (division, right shift, comparison).
    width_sensitive: bool = False


def _const(value: int, width: int | None = None) -> E:
    w = width if width is not None else max(32, value.bit_length() + (1 if value < 0 else 0))
    return E(f"{w}'h{value & ((1 << w) - 1):x}", w)


_MISSING = object()


@dataclass
class _Ctx:
    cls: type
    decls: dict[str, Any]
    inputs: dict[str, int]
    outputs: dict[str, int]
    nets: dict[str, int] = field(default_factory=dict)      # nets set by earlier @comb blocks (name -> width)
    states: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class _Sym:
    """Symbolic execution of one block body."""

    def __init__(self, ctx: _Ctx, mode: str) -> None:
        self.ctx, self.mode = ctx, mode
        self.locals: dict[str, Any] = {}
        self.assigned: dict[str, E] = {}              # comb: net -> expression
        self.next: dict[str, E] = {}                  # seq: register -> next value
        self.mem_writes: list[tuple[str, E, E, E]] = []  # (memory, condition, address, value)
        self.condition: E = E("1'b1", 1, True)
        self.bits_set: dict[str, int] = {}              # comb: net -> the bits set on the current path
        self.hw_depth = 0                               # hardware (non-constant) branches open at this point
        self.loop_depths: list[int] = []                # hw_depth at the entry of each loop being unrolled

    # ── statements ──
    def run(self, body: list[ast.stmt]) -> None:
        for st in body:
            self.stmt(st)

    def stmt(self, st: ast.stmt) -> None:
        if isinstance(st, ast.Expr):
            if isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                return                                                   # a docstring
            if isinstance(st.value, ast.Call):
                self.call_statement(st.value)
                return
            raise Unsupported(f"expression statement {ast.unparse(st)}")
        if isinstance(st, ast.Pass):
            return
        if isinstance(st, ast.Assign):
            if len(st.targets) != 1:
                raise Unsupported("chained assignment")
            self.assign(st.targets[0], st.value)
            return
        if isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name):
            self.locals[st.target.id] = self.expr(ast.BinOp(st.target, st.op, st.value))
            return
        if isinstance(st, ast.If):
            known = self.static(st.test)
            if known is not None:
                self.run(st.body if known else st.orelse)
                return
            self.branch(self.cond(st.test), st.body, st.orelse)
            return
        if isinstance(st, ast.Continue):
            if not self.loop_depths or self.loop_depths[-1] != self.hw_depth:
                raise Unsupported("a continue under a condition that depends on the inputs or registers")
            raise _Continue()
        if isinstance(st, ast.Match):
            self.match(st)
            return
        if isinstance(st, ast.For):
            self.loop(st)
            return
        raise Unsupported(f"statement {type(st).__name__}: {ast.unparse(st)[:60]}")

    def assign(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.locals[target.id] = self.value_of(value)
            return
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) and len(target.elts) == len(value.elts):
            vals = [self.value_of(v) for v in value.elts]
            for t, v in zip(target.elts, vals):
                if not isinstance(t, ast.Name):
                    raise Unsupported("tuple assignment to a non-name")
                self.locals[t.id] = v
            return
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
            name = target.attr
            decl = self.ctx.decls.get(name)
            if (self.mode == "comb" and isinstance(decl, rk.Reg) and isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name) and value.value.id == "self" and value.attr == name):
                return                                   # `self.q = self.q` for an output that is a register: a no-op
            if self.mode != "comb" or isinstance(decl, (rk.Reg, rk.State, rk.Mem)):
                raise Unsupported(f"self.{name} = ... outside a @comb net assignment")
            width = decl.width if isinstance(decl, rk.Wire) else self.ctx.outputs.get(name)
            if width is None:
                raise Unsupported(f"self.{name} is not a wire or an output")
            if isinstance(value, ast.Constant) and value.value is None:
                raise Unsupported("a don't-care (None) output")
            self.set_net(name, self.expr(value), width)
            return
        if (isinstance(target, ast.Attribute) and target.attr == "next" and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name) and target.value.value.id == "self"):
            reg = target.value.attr
            decl = self.ctx.decls.get(reg)
            if self.mode != "seq" or not isinstance(decl, (rk.Reg, rk.State)):
                raise Unsupported(f"self.{reg}.next outside @seq")
            if isinstance(decl, rk.State):
                self.set_next(reg, self.state_value(reg, value), len(self.ctx.states[reg]).bit_length())
            else:
                self.set_next(reg, self.expr(value), decl.width)
            return
        if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name) and target.value.value.id == "self" and self.mode == "comb"):
            self.set_net_bits(target.value.attr, target.slice, value)
            return
        raise Unsupported(f"assignment to {ast.unparse(target)}")

    def set_net_bits(self, name: str, index: ast.expr, value: ast.expr) -> None:
        """`self.g[i] = v` / `self.g[hi:lo] = v` in @comb: those bits of the net, the others as they are on this path."""
        decl = self.ctx.decls.get(name)
        if isinstance(decl, (rk.Reg, rk.State, rk.Mem)):
            raise Unsupported(f"self.{name}[...] = ... on a register")
        width = decl.width if isinstance(decl, rk.Wire) else self.ctx.outputs.get(name)
        if width is None:
            raise Unsupported(f"self.{name} is not a wire or an output")
        if isinstance(value, ast.Constant) and value.value is None:
            raise Unsupported("a don't-care (None) output")
        if isinstance(index, ast.Slice):
            hi, lo = self.int_of(index.lower), self.int_of(index.upper)  # type: ignore[arg-type]
        else:
            hi = lo = self.int_of(index)
        current = self.assigned.get(name)
        if isinstance(current, _Partial):
            raise Unsupported(f"bits of {name} set after the whole of it on some paths only")
        base = current if current is not None else E(f"{width}'d0", width)
        new = _splice(base, hi, lo, _fit(self.expr(value), hi - lo + 1), width)
        self.assigned[name] = new if self.condition.text == "1'b1" else _mux(self.condition, new, base, width)
        self.bits_set[name] = self.bits_set.get(name, 0) | (((1 << (hi - lo + 1)) - 1) << lo)

    def set_net(self, name: str, e: E, width: int) -> None:
        self.bits_set[name] = (1 << width) - 1
        fitted = _fit(e, width)
        old = self.assigned.get(name)
        self.assigned[name] = fitted if old is None or self.condition.text == "1'b1" else _mux(self.condition, fitted, old, width)
        if old is None and self.condition.text != "1'b1":
            self.assigned[name] = _Partial(self.condition, fitted, width)  # type: ignore[assignment]

    def set_next(self, reg: str, e: E, width: int) -> None:
        fitted = _fit(e, width)
        current = self.next.get(reg, E(reg, width))
        self.next[reg] = fitted if self.condition.text == "1'b1" else _mux(self.condition, fitted, current, width)

    def call_statement(self, call: ast.Call) -> None:
        f = call.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and isinstance(f.value.value, ast.Name) \
                and f.value.value.id == "self":
            owner, method = f.value.attr, f.attr
            decl = self.ctx.decls.get(owner)
            if isinstance(decl, rk.Mem) and method == "write" and self.mode == "seq":
                addr, val = (self.expr(a) for a in call.args)
                self.mem_writes.append((owner, self.condition, addr, _fit(val, decl.width)))
                return
            if isinstance(decl, rk.Reg) and method in ("next_bit", "next_slice") and self.mode == "seq":
                current = self.next.get(owner, E(owner, decl.width))
                if method == "next_bit":
                    idx = self.int_of(call.args[0])
                    bitv = _fit(self.expr(call.args[1]), 1)
                    new = _splice(current, idx, idx, bitv, decl.width)
                else:
                    hi, lo = self.int_of(call.args[0]), self.int_of(call.args[1])
                    new = _splice(current, hi, lo, _fit(self.expr(call.args[2]), hi - lo + 1), decl.width)
                self.set_next(owner, new, decl.width)
                return
        raise Unsupported(f"call statement {ast.unparse(call)[:60]}")

    def branch(self, c: E, then: list[ast.stmt], orelse: list[ast.stmt]) -> None:
        if c.text == "1'b1":
            self.run(then)
            return
        if c.text == "1'b0":
            self.run(orelse)
            return
        saved_cond, saved_locals, saved_bits = self.condition, dict(self.locals), dict(self.bits_set)
        self.hw_depth += 1
        try:
            self._both_paths(c, then, orelse, saved_cond, saved_locals, saved_bits)
        finally:
            self.hw_depth -= 1

    def _both_paths(self, c: E, then: list[ast.stmt], orelse: list[ast.stmt], saved_cond: E, saved_locals: dict, saved_bits: dict) -> None:
        self.condition = _and(saved_cond, c)
        self.run(then)
        then_locals, then_bits = self.locals, self.bits_set
        self.locals, self.bits_set = dict(saved_locals), dict(saved_bits)
        self.condition = _and(saved_cond, _not(c))
        self.run(orelse)
        else_locals, else_bits = self.locals, self.bits_set
        self.bits_set = {n: then_bits.get(n, 0) & else_bits.get(n, 0) for n in set(then_bits) | set(else_bits)}
        self.condition = saved_cond
        merged = {}
        for name in set(then_locals) | set(else_locals):
            a, b = then_locals.get(name, _MISSING), else_locals.get(name, _MISSING)
            if a is b:
                merged[name] = a
            elif a is _MISSING or b is _MISSING:
                continue                                  # defined on one path only: reading it later is an error in Python too
            elif not isinstance(a, E) and not isinstance(b, E) and a == b:
                merged[name] = a
            elif _as_e(a) is not None and _as_e(b) is not None:
                # an expression on one path and an expression or a constant on the other (a Python number that differs between the
                # paths becomes a 32-bit unsized constant, as it is when it is used)
                a, b = _as_e(a), _as_e(b)
                w = max(a.width, b.width)
                joined = E(f"({c.text} ? {a.text} : {b.text})", w, a.boolean and b.boolean, a.width_sensitive or b.width_sensitive)
                if a.width != b.width:
                    self.ctx.notes.append(f"local {name} has widths {a.width} and {b.width} on two paths")
                # a value joined again and again (a running total over many cells) is held in a wire, or its text doubles each time
                merged[name] = E(_temp(joined), w, joined.boolean, joined.width_sensitive) if len(joined.text) > 160 else joined
            else:
                raise Unsupported(f"local {name} is a Python value that differs between branches")
        self.locals = merged

    def match(self, st: ast.Match) -> None:
        subject = st.subject
        chain: list[tuple[E | None, list[ast.stmt]]] = []
        for case in st.cases:
            if case.guard is not None:
                raise Unsupported("case guard")
            chain.append((self.pattern(subject, case.pattern), case.body))
        self._chain(chain)

    def _chain(self, chain: list[tuple[E | None, list[ast.stmt]]]) -> None:
        if not chain:
            return
        cond, body = chain[0]
        if cond is None:
            self.run(body)
            return
        self.branch(cond, body, [_Deferred(self, chain[1:])])  # type: ignore[list-item]

    def pattern(self, subject: ast.expr, p: ast.pattern) -> E | None:
        if isinstance(p, ast.MatchAs) and p.pattern is None:
            return None
        if isinstance(p, ast.MatchOr):
            parts = [self.pattern(subject, q) for q in p.patterns]
            if any(x is None for x in parts):
                return None
            return E("(" + " || ".join(x.text for x in parts) + ")", 1, True)  # type: ignore[union-attr]
        if isinstance(p, ast.MatchValue):
            return self.compare(subject, ast.Eq(), p.value)
        raise Unsupported(f"case pattern {ast.unparse(p)}")

    def loop(self, st: ast.For) -> None:
        if not (isinstance(st.target, ast.Name) and not st.orelse):
            raise Unsupported("a for loop that is not over range(...) or a list of constants")
        if isinstance(st.iter, ast.Call) and isinstance(st.iter.func, ast.Name) and st.iter.func.id == "range":
            values: list[Any] = list(range(*[self.int_of(a) for a in st.iter.args]))
        elif isinstance(st.iter, (ast.List, ast.Tuple)):
            values = [self.py(e) for e in st.iter.elts]
        else:
            raise Unsupported("a for loop that is not over range(...) or a list of constants")
        self.loop_depths.append(self.hw_depth)
        try:
            for i in values:
                self.locals[st.target.id] = i
                try:
                    self.run(st.body)
                except _Continue:
                    pass
        finally:
            self.loop_depths.pop()

    def static(self, node: ast.expr) -> bool | None:
        """Whether a condition is known at translation time (loop indexes and constants only): True, False, or None."""
        try:
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                a, b = self.py(node.left), self.py(node.comparators[0])
                ops = {ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b, ast.Lt: lambda: a < b, ast.LtE: lambda: a <= b,
                       ast.Gt: lambda: a > b, ast.GtE: lambda: a >= b}
                return bool(ops[type(node.ops[0])]()) if type(node.ops[0]) in ops else None
            if isinstance(node, ast.BoolOp):
                values = [self.static(v) for v in node.values]
                if isinstance(node.op, ast.And):
                    return False if any(v is False for v in values) else True if all(v is True for v in values) else None
                return True if any(v is True for v in values) else False if all(v is False for v in values) else None
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                inner = self.static(node.operand)
                return None if inner is None else not inner
            return bool(self.py(node))
        except (Unsupported, TypeError):
            return None

    # ── expressions ──
    def value_of(self, node: ast.expr) -> Any:
        """A local's value: a Python int/str/bool stays one (for indexes and loops), anything else is an expression."""
        try:
            return self.py(node)
        except Unsupported:
            return self.expr(node)

    def py(self, node: ast.expr) -> Any:
        """A compile-time Python value (parameters, loop indexes, constants), or Unsupported."""
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, str, bool)):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.locals and not isinstance(self.locals[node.id], E):
            return self.locals[node.id]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            attr = getattr(self.ctx.cls, node.attr, _MISSING)
            if isinstance(attr, (int, str)) and not isinstance(attr, bool) and node.attr not in self.ctx.decls \
                    and node.attr not in self.ctx.inputs:
                return attr
        if isinstance(node, ast.BinOp):
            a, b = self.py(node.left), self.py(node.right)
            if isinstance(a, int) and isinstance(b, int):
                return _pyop(node.op, a, b)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self.py(node.operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "clog2":
            return rk.clog2(self.py(node.args[0]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" \
                and isinstance(node.args[0], ast.Attribute) and isinstance(self.ctx.decls.get(node.args[0].attr), rk.Mem):
            return self.ctx.decls[node.args[0].attr].depth
        if isinstance(node, ast.Attribute) and node.attr == "width":
            return self.expr(node.value).width
        raise Unsupported("not a compile-time value")

    def int_of(self, node: ast.expr) -> int:
        v = self.py(node)
        if not isinstance(v, int):
            raise Unsupported(f"{ast.unparse(node)} must be a constant")
        return v

    def cond(self, node: ast.expr) -> E:
        e = self.expr(node)
        return e if e.boolean else E(f"({e.text} != {e.width}'d0)", 1, True)

    def expr(self, node: ast.expr) -> E:
        ctx = self.ctx
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return E("1'b1" if node.value else "1'b0", 1, True)
            if isinstance(node.value, int):
                return _const(node.value)
            raise Unsupported(f"constant {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                v = self.locals[node.id]
                return v if isinstance(v, E) else _const(int(v)) if isinstance(v, int) else _raise(f"local {node.id}")
            if node.id in ("True", "False"):
                return E("1'b1" if node.id == "True" else "1'b0", 1, True)
            raise Unsupported(f"name {node.id}")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            name = node.attr
            decl = ctx.decls.get(name)
            if name in ctx.nets and self.mode in ("comb", "seq"):
                return E(name, ctx.nets[name])
            if name in self.assigned and isinstance(self.assigned[name], E):
                return E(name, ctx.nets.get(name) or (decl.width if isinstance(decl, rk.Wire) else ctx.outputs[name]))
            if isinstance(decl, rk.Reg):
                return E(name, decl.width)
            if isinstance(decl, rk.State):
                raise Unsupported("a state register used as a number")
            key = name[:-1] if name.endswith("_") and name not in ctx.inputs and name[:-1] in ctx.inputs else name
            if key in ctx.inputs:
                return E(_vname(key), ctx.inputs[key])
            v = getattr(ctx.cls, name, _MISSING)
            if isinstance(v, int) and not isinstance(v, bool):
                return _const(v)
            raise Unsupported(f"self.{name}")
        if isinstance(node, ast.BinOp):
            try:
                v = self.py(node)
                if isinstance(v, int):
                    return _const(v)
            except Unsupported:
                pass
            a, b = self.expr(node.left), self.expr(node.right)
            if isinstance(node.op, (ast.LShift, ast.RShift)):
                n = self.int_of(node.right)
                op = "<<" if isinstance(node.op, ast.LShift) else ">>"
                return E(f"({a.text} {op} {n})", a.width, False, a.width_sensitive or op == ">>")
            ops = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "/", ast.Div: "/", ast.Mod: "%",
                   ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^", ast.Pow: "**"}
            op = ops.get(type(node.op))
            if op is None:
                raise Unsupported(f"operator {type(node.op).__name__}")
            if op == "**":
                raise Unsupported("power")
            return E(f"({_num(a)} {op} {_num(b)})", max(a.width, b.width), False,
                     a.width_sensitive or b.width_sensitive or op in ("/", "%"))
        if isinstance(node, ast.UnaryOp):
            a = self.expr(node.operand)
            if isinstance(node.op, ast.Invert):
                return E(f"(~{_num(a)})", a.width, False, a.width_sensitive)
            if isinstance(node.op, ast.USub):
                return E(f"(-{_num(a)})", a.width, False, a.width_sensitive)
            if isinstance(node.op, ast.Not):
                return E(f"(!{a.text})", 1, True)
            raise Unsupported("unary +")
        if isinstance(node, ast.BoolOp):
            parts = [self.expr(v) for v in node.values]
            last = parts[-1]
            if not (last.boolean or last.width == 1):
                raise Unsupported("and/or whose value is a multi-bit number (Python returns the operand, Verilog a truth value)")
            op = " && " if isinstance(node.op, ast.And) else " || "
            return E("(" + op.join(p.text for p in parts) + ")", 1, True)
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            parts = [self.compare(operands[i], op, operands[i + 1]) for i, op in enumerate(node.ops)]     # a < b < c is (a < b) and (b < c)
            return parts[0] if len(parts) == 1 else E("(" + " && ".join(p.text for p in parts) + ")", 1, True)
        if isinstance(node, ast.IfExp):
            c, a, b = self.cond(node.test), self.expr(node.body), self.expr(node.orelse)
            if a.width != b.width and (a.width_sensitive or b.width_sensitive):
                raise Unsupported("a conditional expression whose branches differ in width around a width-sensitive operator")
            return E(f"({c.text} ? {_num(a)} : {_num(b)})", max(a.width, b.width), a.boolean and b.boolean,
                     a.width_sensitive or b.width_sensitive)
        if isinstance(node, ast.Subscript):
            return self.subscript(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        raise Unsupported(f"expression {type(node).__name__}: {ast.unparse(node)[:60]}")

    def compare(self, left: ast.expr, op: ast.cmpop, right: ast.expr) -> E:
        if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, (ast.Tuple, ast.List, ast.Set)) and right.elts:
            parts = [self.compare(left, ast.Eq(), e) for e in right.elts]
            any_of = E("(" + " || ".join(p.text for p in parts) + ")", 1, True)
            return any_of if isinstance(op, ast.In) else _not(any_of)
        # a state register compared with a name
        state = _state_ref(left)
        if state is not None and state in self.ctx.states:
            name = self.py(right)
            if name not in self.ctx.states[state]:
                raise Unsupported(f"state {name!r}")
            code = f"{state.upper()}__{name}"
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                raise Unsupported("a state compared with < or >")
            return E(f"({state} {'==' if isinstance(op, ast.Eq) else '!='} {code})", 1, True)
        # signed comparisons through .signed on both sides (or a constant)
        if _is_signed(left) or _is_signed(right):
            a, b = self.signed_operand(left), self.signed_operand(right)
            ops = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
            return E(f"({a} {ops[type(op)]} {b})", 1, True)
        a, b = self.expr(left), self.expr(right)
        ops = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
        if type(op) not in ops:
            raise Unsupported(f"comparison {type(op).__name__}")
        return E(f"({_num(a)} {ops[type(op)]} {_num(b)})", 1, True)

    def signed_operand(self, node: ast.expr) -> str:
        if _is_signed(node):
            e = self.expr(node.value)  # type: ignore[attr-defined]
            return f"$signed({e.text})"
        v = self.py(node)
        if isinstance(v, int):
            w = max(32, v.bit_length() + 1)
            return f"$signed({w}'h{v & ((1 << w) - 1):x})"
        raise Unsupported(".signed compared with something that is not .signed or a constant")

    def subscript(self, node: ast.Subscript) -> E:
        base = node.value
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == "self" \
                and isinstance(self.ctx.decls.get(base.attr), rk.Mem):
            decl = self.ctx.decls[base.attr]
            return E(f"{base.attr}[{self.expr(node.slice).text}]", decl.width)
        v = self.expr(base)
        if isinstance(node.slice, ast.Slice):
            hi, lo = self.int_of(node.slice.lower), self.int_of(node.slice.upper)
            return E(f"{_sel(v)}[{hi}:{lo}]", hi - lo + 1)
        try:
            i = self.int_of(node.slice)
            return E(f"{_sel(v)}[{i}]", 1)
        except Unsupported:
            return E(f"{_sel(v)}[{self.expr(node.slice).text}]", 1)

    def call(self, node: ast.Call) -> E:
        f = node.func
        if isinstance(f, ast.Name):
            if f.id == "B":
                w = self.int_of(node.args[1])
                try:
                    return _const(self.int_of(node.args[0]), w)
                except Unsupported:
                    return _fit(self.expr(node.args[0]), w)
            if f.id == "cat":
                parts = [self.expr(a) for a in node.args]
                return E("{" + ", ".join(_sized(p) for p in parts) + "}", sum(p.width for p in parts))
            if f.id == "mux":
                c, a, b = self.cond(node.args[0]), self.expr(node.args[1]), self.expr(node.args[2])
                return E(f"({c.text} ? {_num(a)} : {_num(b)})", max(a.width, b.width))
            if f.id == "int":
                return self.expr(node.args[0])
            if f.id == "bool":
                return self.cond(node.args[0])
            if f.id == "casez":
                v, pattern = self.expr(node.args[0]), self.py(node.args[1])
                bits = str(pattern).replace("_", "")
                care = int("".join("0" if ch in "?xzXZ" else "1" for ch in bits), 2)
                value = int("".join("0" if ch in "?xzXZ" else ch for ch in bits), 2)
                w = max(v.width, len(bits))
                return E(f"(({_num(v)} & {w}'h{care:x}) == {w}'h{value:x})", 1, True)
            raise Unsupported(f"function {f.id}")
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id == "self":
                return self.inline(f.attr, node)
            method = f.attr
            v = self.expr(f.value)
            args = node.args
            if method == "zext":
                w = self.int_of(args[0])
                return E(_sized(v) if w == v.width else f"{{{{{w - v.width}{{1'b0}}}}, {_sized(v)}}}" if w > v.width
                         else f"{_sel(v)}[{w - 1}:0]", w)
            if method == "sext":
                w = self.int_of(args[0])
                if w <= v.width:
                    return E(f"{_sel(v)}[{w - 1}:0]", w)
                return E(f"{{{{{w - v.width}{{{_sel(v)}[{v.width - 1}]}}}}, {_sized(v)}}}", w)
            if method in ("shift_in_low", "shift_in_high"):
                try:
                    new = E(f"1'b{int(self.py(args[0])) & 1}", 1)
                except Unsupported:
                    new = self.expr(args[0])
                if method == "shift_in_low":
                    return E(f"{v.width}'({{{_sized(v)}, {_sized(new)}}})", v.width)
                return E(f"{v.width}'({{{_sized(new)}, {_sized(v)}}} >> {new.width})", v.width)
            if method == "set_bit":
                i = self.int_of(args[0])
                return _splice(v, i, i, _fit(self.expr(args[1]), 1), v.width)
            if method == "set_slice":
                hi, lo = self.int_of(args[0]), self.int_of(args[1])
                return _splice(v, hi, lo, _fit(self.expr(args[2]), hi - lo + 1), v.width)
            if method == "rep":
                n = self.int_of(args[0])
                return E(f"{{{n}{{{_sized(v)}}}}}", v.width * n)
            if method in ("reduce_and", "reduce_or", "reduce_xor", "reduce_nand", "reduce_nor", "reduce_xnor"):
                op = {"reduce_and": "&", "reduce_or": "|", "reduce_xor": "^", "reduce_nand": "~&", "reduce_nor": "~|",
                      "reduce_xnor": "~^"}[method]
                return E(f"({op}{_sized(v)})", 1)
            if method == "xnor":
                b = self.expr(args[0])
                return E(f"(~({_num(v)} ^ {_num(b)}))", max(v.width, b.width))
            if method == "part":
                base, w = self.expr(args[0]), self.int_of(args[1])
                return E(f"{_sel(v)}[{base.text} +: {w}]", w)
            if method == "part_down":
                base, w = self.expr(args[0]), self.int_of(args[1])
                return E(f"{_sel(v)}[{base.text} -: {w}]", w)
            if method in ("slt", "sle", "sgt", "sge"):
                b = self.expr(args[0])
                w = max(v.width, b.width)
                op = {"slt": "<", "sle": "<=", "sgt": ">", "sge": ">="}[method]
                return E(f"($signed({_sext(v, w)}) {op} $signed({_sext(b, w)}))", 1, True)
            if method == "sra":
                n = self.int_of(args[0])
                return E(f"($signed({_sized(v)}) >>> {n})", v.width, False, True)
            raise Unsupported(f"method .{method}")
        raise Unsupported(f"call {ast.unparse(node)[:60]}")

    def inline(self, name: str, node: ast.Call) -> E:
        fn = getattr(self.ctx.cls, name, None)
        if fn is None or not callable(fn) or getattr(fn, "_refkit_kind", None) is not None:
            raise Unsupported(f"self.{name}(...)")
        tree = _function_ast(self.ctx.cls, name)
        params = [a.arg for a in tree.args.args][1:]
        if len(params) != len(node.args) or node.keywords:
            raise Unsupported(f"call of self.{name} with keywords or a different number of arguments")
        inner = _Sym(self.ctx, "pure")
        inner.locals = {p: self.value_of(a) for p, a in zip(params, node.args)}
        body = tree.body
        if not body or not isinstance(body[-1], ast.Return) or any(isinstance(s, ast.Return) for s in body[:-1]):
            raise Unsupported(f"self.{name} must end with its only return")
        inner.run(body[:-1])
        return inner.expr(body[-1].value)  # type: ignore[arg-type]

    def state_value(self, reg: str, node: ast.expr) -> E:
        if isinstance(node, ast.IfExp):
            c = self.cond(node.test)
            a, b = self.state_value(reg, node.body), self.state_value(reg, node.orelse)
            return E(f"({c.text} ? {a.text} : {b.text})", a.width)
        # a state register read as a value: the same register (a hold), or another one with the same names
        source = _state_ref(node)
        if source is not None and source in self.ctx.states and self.ctx.states[source] == self.ctx.states[reg]:
            return E(source, len(self.ctx.states[reg]).bit_length())
        name = self.py(node)
        if name not in self.ctx.states[reg]:
            raise Unsupported(f"state {name!r}")
        return E(f"{reg.upper()}__{name}", len(self.ctx.states[reg]).bit_length())


class _Deferred(ast.stmt):
    """The rest of an if/elif chain built from a match, run as the else branch."""

    def __init__(self, sym: _Sym, chain: list) -> None:
        super().__init__()
        self.sym, self.chain = sym, chain


_orig_stmt = _Sym.stmt


def _stmt(self: _Sym, st: ast.stmt) -> None:
    if isinstance(st, _Deferred):
        self._chain(st.chain)
        return
    _orig_stmt(self, st)


_Sym.stmt = _stmt  # type: ignore[method-assign]


class _Partial(E):
    """A net set on some paths only: valid only if every path ends up setting it."""

    def __init__(self, cond: E, value: E, width: int) -> None:
        super().__init__(value.text, width)
        self.cond, self.value = cond, value


def _raise(msg: str) -> E:
    raise Unsupported(msg)


def _pyop(op: ast.operator, a: int, b: int) -> int:
    return {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.FloorDiv: lambda: a // b,
            ast.Mod: lambda: a % b, ast.Pow: lambda: a ** b, ast.LShift: lambda: a << b, ast.RShift: lambda: a >> b,
            ast.BitAnd: lambda: a & b, ast.BitOr: lambda: a | b, ast.BitXor: lambda: a ^ b}[type(op)]()


def _as_e(v: Any) -> E | None:
    """A local as an expression: an expression, or a Python number (a 32-bit unsized constant); anything else is not one."""
    if isinstance(v, E):
        return v
    if isinstance(v, bool):
        return E("1'b1" if v else "1'b0", 1, True)
    if isinstance(v, int):
        return _const(v)
    return None


def _num(e: E) -> str:
    return e.text


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*|\d+'h[0-9a-f]+|\d+'b[01]")
_TEMPS: list[tuple[str, str]] = []


def _temp(e: E) -> str:
    """A wire holding the expression at its own width (Verilog cannot select bits of an expression)."""
    name = f"__t{len(_TEMPS)}"
    _TEMPS.append((f"  wire {_rng(e.width)}{name};", f"  assign {name} = {e.width}'({e.text});"))
    return name


def _sized(e: E) -> str:
    """The expression at its own width (SystemVerilog cast; a concatenation operand is self-determined anyway)."""
    return e.text if _IDENT.fullmatch(e.text) else f"{e.width}'({e.text})"


def _sel(e: E) -> str:
    return e.text if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", e.text) else _temp(e)


def _sext(e: E, w: int) -> str:
    if w == e.width:
        return _sized(e)
    return f"{{{{{w - e.width}{{{_sel(e)}[{e.width - 1}]}}}}, {_sized(e)}}}"


def _fit(e: E, width: int) -> E:
    """The value as it lands in a destination of `width` bits: a cast evaluates it at max(width, its own width) and truncates,
    exactly as RefKit (and a Verilog assignment) does."""
    return E(f"{width}'({e.text})", width)


def _mux(c: E, a: E, b: E, width: int) -> E:
    return E(f"({c.text} ? {a.text} : {b.text})", width)


def _and(a: E, b: E) -> E:
    if a.text == "1'b1":
        return b
    return E(f"({a.text} && {b.text})", 1, True)


def _not(c: E) -> E:
    return E(f"(!{c.text})", 1, True)


def _splice(current: E, hi: int, lo: int, part: E, width: int) -> E:
    pieces = []
    if hi < width - 1:
        pieces.append(f"{_sel(current)}[{width - 1}:{hi + 1}]")
    pieces.append(_sized(part))
    if lo > 0:
        pieces.append(f"{_sel(current)}[{lo - 1}:0]")
    return E("{" + ", ".join(pieces) + "}", width)


def _state_ref(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def _is_signed(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "signed"


def _vname(name: str) -> str:
    return name


_SOURCES: dict[type, ast.ClassDef] = {}


def _function_ast(cls: type, name: str) -> ast.FunctionDef:
    tree = _SOURCES.get(cls)
    for klass in cls.__mro__:
        tree = _SOURCES.get(klass)
        if tree is None:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
    raise Unsupported(f"no source for {cls.__name__}.{name}")


# ─── the module ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Translation:
    verilog: str
    module: str
    notes: list[str]
    edge: str = "posedge"


def translate(source: str, module_name: str = "refkit_ref", clock_name: str = "clk") -> Translation:
    """The Verilog of a completed RefKit reference (one that carries `__refkit__`). `clock_name` is the name of the clock port of the
    translated module (the block's own, e.g. `CLK`); the miter always uses `clk`."""
    ns: dict[str, Any] = {}
    try:
        exec(compile(source, "<reference>", "exec"), ns)  # noqa: S102
    except Exception as error:  # noqa: BLE001
        raise Unsupported(f"the reference does not load: {error}") from error
    meta = ns.get("__refkit__")
    if not isinstance(meta, dict):
        raise Unsupported("not a completed RefKit reference")
    cls = meta["module"]
    _TEMPS.clear()
    if not isinstance(cls, type) or not issubclass(cls, rk.Module):
        raise Unsupported("only Module references are translated")
    edges = {getattr(f, "_refkit_edge", "pos") for f in cls._seq}
    if cls._latch or "both" in edges or len(edges) > 1:
        raise Unsupported("step mode (latches, both edges, or rising and falling edges in one module)")
    edge_kw = "negedge" if edges == {"neg"} else "posedge"
    if cls._instances:
        raise Unsupported("instances")
    if "memory" in source and "self.memory" in source:
        raise Unsupported("file-loaded memory")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and isinstance(ns.get(node.name), type):
            _SOURCES[ns[node.name]] = node
    if cls not in _SOURCES:
        raise Unsupported("the module class was not found in the source")
    inputs, outputs = dict(meta["inputs"]), dict(meta["widths"])
    # a Reg() declared without a width has the width of its output port, or 32 bits (as RefKit runs it)
    decls = {n: (rk.Reg(outputs.get(n, rk.NUMBER_WIDTH), d.reset, d.init) if isinstance(d, rk.Reg) and d.width is None else d)
             for n, d in cls._decls.items()}
    ctx = _Ctx(cls, decls, inputs, outputs)
    for name, decl in decls.items():
        if isinstance(decl, rk.State):
            ctx.states[name] = list(decl.names)
    combinational = bool(meta["combinational"])
    lines: list[str] = []
    reg_outputs = {n for n in outputs if isinstance(decls.get(n), rk.Reg)}
    for n in reg_outputs:
        if decls[n].width != outputs[n]:
            raise Unsupported(f"output {n} is a register of another width")
    ports = ([] if combinational else [f"input {clock_name}"]) + [f"input {_rng(w)}{n}" for n, w in inputs.items()] + \
        [f"output {'reg ' if n in reg_outputs else ''}{_rng(w)}{n}"
         + (f" = {w}'d{rk.mask(decls[n].power_up, w)}" if n in reg_outputs else "") for n, w in outputs.items()]
    lines.append(f"module {module_name} ({', '.join(ports)});")
    for name, states in ctx.states.items():
        width = len(states).bit_length()
        for i, s in enumerate(states):
            lines.append(f"  localparam [{width - 1}:0] {name.upper()}__{s} = {width}'d{i};")
    for name, decl in decls.items():
        if isinstance(decl, rk.Reg) and name not in reg_outputs:
            lines.append(f"  reg {_rng(decl.width)}{name} = {decl.width}'d{rk.mask(decl.power_up, decl.width)};")
        elif isinstance(decl, rk.State):
            w = len(decl.names).bit_length()
            lines.append(f"  reg [{w - 1}:0] {name} = {name.upper()}__{decl.reset};")
        elif isinstance(decl, rk.Mem):
            lines.append(f"  reg {_rng(decl.width)}{name} [0:{decl.depth - 1}];")
            lines.append(f"  integer {name}__i; initial for ({name}__i = 0; {name}__i < {decl.depth}; {name}__i = {name}__i + 1) "
                         f"{name}[{name}__i] = {decl.width}'d{rk.mask(decl.reset or 0, decl.width)};")
    declarations_end = len(lines)
    # @comb blocks, in order: each net becomes a wire
    set_nets: dict[str, E] = {}
    for block in cls._comb:
        sym = _Sym(ctx, "comb")
        sym.run(_function_ast(cls, block.__name__).body)
        for name, e in sym.assigned.items():
            width = ctx.outputs.get(name) or ctx.decls[name].width
            if sym.bits_set.get(name, 0) != (1 << width) - 1:
                raise Unsupported(f"some bits of {name} are not set on every path of a @comb block")
            if isinstance(e, _Partial):
                raise Unsupported(f"{name} is not set on every path of a @comb block")
            if name in set_nets:
                raise Unsupported(f"{name} is set by two @comb blocks")
            width = ctx.outputs.get(name) or ctx.decls[name].width
            ctx.nets[name] = width
            set_nets[name] = e
    for name in set_nets:
        if name not in outputs:
            lines.insert(declarations_end, f"  wire {_rng(ctx.nets[name])}{name};")
            declarations_end += 1
    for name, e in set_nets.items():
        lines.append(f"  assign {name} = {e.text};")
    for name in outputs:
        if name not in set_nets and name not in reg_outputs:
            raise Unsupported(f"output {name} is not set")
    # @seq blocks: each register's next value
    if cls._seq:
        sym = _Sym(ctx, "seq")
        for block in cls._seq:
            sym.run(_function_ast(cls, block.__name__).body)
        reset = meta["reset"]
        active = int(meta["active"])
        level = f"({_vname(reset)} == 1'b{active})" if reset is not None else None
        style = meta["style"]
        resettable = {n for n, d in decls.items()
                      if (isinstance(d, rk.Reg) and d.reset is not None) or isinstance(d, rk.State)}

        def reset_value(name: str) -> str:
            d = decls[name]
            return f"{name.upper()}__{d.reset}" if isinstance(d, rk.State) else f"{d.width}'d{rk.mask(d.reset, d.width)}"

        nexts = [(reg, e) for reg, e in sym.next.items()]
        if reset is not None and style == "async":
            if any(d.reset is not None for d in cls._mems.values()) and sym.mem_writes:
                raise Unsupported("a memory cleared by an asynchronous reset")
            edge = "posedge" if active == 1 else "negedge"
            fast = [(r, e) for r, e in nexts if r in resettable]
            if resettable:
                lines.append(f"  always @({edge_kw} {clock_name} or {edge} {_vname(reset)}) begin")
                lines.append(f"    if {level} begin")
                lines += [f"      {r} <= {reset_value(r)};" for r in sorted(resettable)]
                lines.append("    end else begin")
                lines += [f"      {r} <= {e.text};" for r, e in fast]
                lines.append("    end")
                lines.append("  end")
            slow = [(r, e) for r, e in nexts if r not in resettable]
            if slow or sym.mem_writes:
                lines.append(f"  always @({edge_kw} {clock_name}) begin")
                lines += [f"    {r} <= {e.text};" for r, e in slow]
                lines += [f"    if ({c.text}) {m}[{a.text}] <= {v.text};" for m, c, a, v in sym.mem_writes]
                lines.append("  end")
        else:
            body = [f"    {r} <= {e.text};" for r, e in nexts]
            body += [f"    if ({c.text}) {m}[{a.text}] <= {v.text};" for m, c, a, v in sym.mem_writes]
            if level is not None:
                resets = [f"      {r} <= {reset_value(r)};" for r in sorted(resettable)]
                for name, decl in cls._mems.items():
                    if decl.reset is not None:
                        resets.append(f"      for ({name}__i = 0; {name}__i < {decl.depth}; {name}__i = {name}__i + 1) "
                                      f"{name}[{name}__i] <= {decl.width}'d{rk.mask(decl.reset, decl.width)};")
                if resets:
                    body.append(f"    if {level} begin")
                    body += resets
                    body.append("    end")
            lines.append(f"  always @({edge_kw} {clock_name}) begin")
            lines += body
            lines.append("  end")
    lines.append("endmodule")
    out = lines[:declarations_end] + [d for d, _ in _TEMPS] + lines[declarations_end:-1] + [a for _, a in _TEMPS] + lines[-1:]
    return Translation("\n".join(out) + "\n", module_name, ctx.notes, edge_kw)


def _rng(width: int) -> str:
    return f"[{width - 1}:0] " if width > 1 else ""


# ─── the proof ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Proof:
    #: "proven" (equivalent for every input sequence after reset), "refuted" (a counterexample the simulation confirms),
    #: "unknown" (neither within the depth, or a counterexample the simulation does not confirm), "unsupported".
    status: str
    detail: str
    #: The confirmed counterexample: one dict of inputs per cycle, from reset.
    counterexample: list[dict[str, int]] = field(default_factory=list)


def _tool(name: str) -> str | None:
    """A tool of the OSS CAD Suite: `$REFKIT_TOOLS/<name>`, then PATH, then `$OSS_CAD_SUITE/bin`, then ~/tools/oss-cad-suite/bin."""
    import os
    candidates = []
    for base in (os.environ.get("REFKIT_TOOLS"), os.environ.get("OSS_CAD_SUITE") and f"{os.environ['OSS_CAD_SUITE']}/bin",
                 str(Path.home() / "tools" / "oss-cad-suite" / "bin")):
        if base:
            candidates.append(Path(base) / name)
    found = shutil.which(name)
    if found:
        return found
    return next((str(c) for c in candidates if c.exists()), None)


def miter(meta: dict[str, Any], dut_module: str, clock: str | None) -> str:
    """A module that drives the reference and the RTL with the same inputs and asserts their outputs agree after reset."""
    inputs, outputs = meta["inputs"], meta["widths"]
    combinational = bool(meta["combinational"])
    ports = ([] if combinational else ["input clk"]) + [f"input {_rng(w)}{n}" for n, w in inputs.items()]
    lines = [f"module refkit_miter ({', '.join(ports)});"]
    for n, w in outputs.items():
        lines.append(f"  wire {_rng(w)}r__{n}, d__{n};")
    ref_conn = ([] if combinational else [".clk(clk)"]) + [f".{n}({n})" for n in inputs] + [f".{n}(r__{n})" for n in outputs]
    dut_conn = ([] if combinational or clock is None else [f".{clock}(clk)"]) + [f".{n}({n})" for n in inputs] + \
        [f".{n}(d__{n})" for n in outputs]
    lines.append(f"  refkit_ref R ({', '.join(ref_conn)});")
    lines.append(f"  {dut_module} D ({', '.join(dut_conn)});")
    checks = " ".join(f"assert (r__{n} == d__{n});" for n in outputs)
    if combinational:
        lines.append(f"  always @(*) begin {checks} end")
    else:
        lines.append("  reg init = 1'b1;")
        lines.append("  always @(posedge clk) init <= 1'b0;")
        if meta["reset"] is not None:
            lines.append(f"  always @(*) if (init) assume ({meta['reset']} == 1'b{int(meta['active'])});")
        lines.append(f"  always @(*) if (!init) begin {checks} end")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _vcd_inputs(path: Path, names: list[str], scope: str = "refkit_miter") -> list[dict[str, int]]:
    """The miter's inputs at each step of a counterexample trace (one value change dump step per clock cycle)."""
    ids: dict[str, str] = {}
    depth, in_top = 0, False
    times: list[int] = []
    values: dict[str, int] = {}
    per_time: list[dict[str, int]] = []
    text = path.read_text()
    header, _, body = text.partition("$enddefinitions")
    for line in header.splitlines():
        tok = line.split()
        if tok[:1] == ["$scope"]:
            depth += 1
            in_top = depth == 1
        elif tok[:1] == ["$upscope"]:
            depth -= 1
            in_top = depth == 1
        elif tok[:1] == ["$var"] and in_top and depth == 1:
            ident, name = tok[3], tok[4]
            if name in names or name == "clk":
                ids[ident] = name
    current: int | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("$"):
            continue
        if line.startswith("#"):
            if current is not None:
                per_time.append(dict(values))
                times.append(current)
            current = int(line[1:])
            continue
        if line[0] in "01xz" and len(line) > 1 and line[1:] in ids:
            values[ids[line[1:]]] = 0 if line[0] in "xz" else int(line[0])
        elif line[0] == "b":
            bits, ident = line[1:].split()
            if ident in ids:
                values[ids[ident]] = int(bits.replace("x", "0").replace("z", "0"), 2)
    if current is not None:
        per_time.append(dict(values))
    # smtbmc writes each cycle as the clock high, then a half step with it low: one cycle per step with the clock high.
    if any("clk" in v for v in per_time):
        per_time = [v for v in per_time if v.get("clk", 0) == 1]
    return [{n: v.get(n, 0) for n in names} for v in per_time]


def prove(source: str, rtl: str, dut_module: str, clock: str | None, *, depth: int = 24, timeout: int = 120,
          workdir: Path | None = None) -> Proof:
    """Prove the RTL equivalent to the RefKit reference with SymbiYosys (k-induction), or find a counterexample."""
    try:
        translation = translate(source)
    except Unsupported as why:
        return Proof("unsupported", f"the reference was not translated to Verilog: {why}")
    if translation.edge != "posedge":
        return Proof("unsupported", "the reference is clocked on the falling edge: the proof is not run")
    meta = rk._meta_of(source)
    sby = _tool("sby")
    if sby is None or meta is None:
        return Proof("unsupported", "SymbiYosys is not available" if sby is None else "not a RefKit reference")
    own = workdir is None
    root = Path(tempfile.mkdtemp(prefix="refkit-prove-")) if own else workdir
    root.mkdir(parents=True, exist_ok=True)
    (root / "ref.sv").write_text(translation.verilog)
    (root / "dut.v").write_text(rtl)
    (root / "miter.sv").write_text(miter(meta, dut_module, clock))
    mode_depth = 1 if meta["combinational"] else depth
    (root / "proof.sby").write_text(
        "[options]\nmode prove\ndepth %d\n\n[engines]\nsmtbmc yices\n\n[script]\n"
        "read_verilog -sv -formal ref.sv\nread_verilog -sv -formal dut.v\nread_verilog -sv -formal miter.sv\n"
        "prep -top refkit_miter\n\n[files]\nref.sv\ndut.v\nmiter.sv\n" % mode_depth)
    env = None
    yosys = _tool("yosys")
    if yosys is not None:
        import os
        env = {**os.environ, "PATH": f"{Path(yosys).parent}:{os.environ.get('PATH', '')}"}
    try:
        done = subprocess.run([sby, "-f", "proof.sby"], cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return Proof("unknown", f"SymbiYosys did not finish within {timeout} s")
    log = done.stdout + done.stderr
    status = re.search(r"DONE \((PASS|FAIL|UNKNOWN|ERROR)", log)
    if status is None or status.group(1) == "ERROR":
        tail = "\n".join(line for line in log.splitlines() if "ERROR" in line or "rror" in line)[-600:]
        return Proof("unknown", f"SymbiYosys could not run the proof: {tail or log[-400:]}")
    if status.group(1) == "PASS":
        return Proof("proven", "SymbiYosys proved the RTL equivalent to the reference: every output agrees in every cycle after "
                     "reset, for every input sequence (k-induction)" if not meta["combinational"]
                     else "SymbiYosys proved the RTL equal to the reference on every input")
    if status.group(1) == "UNKNOWN":
        return Proof("unknown", f"no difference within {depth} cycles of reset, but the induction did not close, so it is not a proof")
    traces = sorted(root.glob("proof*/engine_*/trace*.vcd"))
    if not traces:
        return Proof("unknown", "SymbiYosys reported a difference but wrote no trace")
    cycles = _vcd_inputs(traces[0], list(meta["inputs"]))
    return Proof("refuted", f"SymbiYosys found inputs on which the RTL and the reference differ ({len(cycles)} cycle(s) from reset)",
                 cycles)


__all__ = ["translate", "prove", "miter", "Proof", "Unsupported"]
