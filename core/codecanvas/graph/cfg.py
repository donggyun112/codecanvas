"""Control Flow Graph builder from Python AST.

Splits function bodies into basic blocks connected by typed edges:
  - fall_through: sequential flow
  - true / false: conditional branches
  - exception: try/except paths
  - back_edge: loop back to condition
  - exit: return/raise leaving the function
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from codecanvas.parser.call_graph import CallGraphBuilder, FunctionDef


def _branch_subject_name(node: ast.expr) -> str:
    """Extract a readable name from an AST expression for branch explanation."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _branch_subject_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return f"{node.func.id}()"
        if isinstance(node.func, ast.Attribute):
            return f"{node.func.attr}()"
    if isinstance(node, ast.Subscript):
        base = _branch_subject_name(node.value)
        return f"{base}[...]"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ""


def _body_is_error(body: list) -> bool:
    """Check if a body consists of raise or return-error patterns."""
    if not body:
        return False
    last = body[-1] if body else None
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.Return):
        return False
    # Single statement that's a raise
    if len(body) == 1 and isinstance(body[0], ast.Raise):
        return True
    return False


@dataclass
class CFGStatement:
    """One statement within a basic block."""
    line: int
    line_end: int | None = None
    text: str = ""
    kind: str = ""  # assign, call, branch_test, return, raise, loop_header, break, continue


@dataclass
class BasicBlock:
    """A maximal sequence of statements with single entry, single exit."""
    id: str
    statements: list[CFGStatement] = field(default_factory=list)
    label: str = ""
    kind: str = "block"  # block, entry, exit, error_exit, merge
    scope: str = ""
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CFGEdge:
    """Directed edge between basic blocks."""
    id: str
    source_block_id: str
    target_block_id: str
    kind: str = "fall_through"  # fall_through, true, false, exception, back_edge, exit
    label: str = ""
    condition: str = ""


@dataclass
class ControlFlowGraph:
    """Complete CFG for one function."""
    function_name: str = ""
    file_path: str | None = None
    blocks: list[BasicBlock] = field(default_factory=list)
    edges: list[CFGEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "functionName": self.function_name,
            "filePath": self.file_path,
            "blocks": [
                {
                    "id": b.id,
                    "label": b.label,
                    "kind": b.kind,
                    "scope": b.scope,
                    "filePath": b.file_path,
                    "lineStart": b.line_start,
                    "lineEnd": b.line_end,
                    "statements": [
                        {"line": s.line, "lineEnd": s.line_end, "text": s.text, "kind": s.kind}
                        for s in b.statements
                    ],
                    "metadata": b.metadata,
                }
                for b in self.blocks
            ],
            "edges": [
                {
                    "id": e.id,
                    "sourceBlockId": e.source_block_id,
                    "targetBlockId": e.target_block_id,
                    "kind": e.kind,
                    "label": e.label,
                    "condition": e.condition,
                }
                for e in self.edges
            ],
        }


# Standard Python exception hierarchy (simplified).
# Maps child → set of ancestors so we can check "is_subclass".
_EXCEPTION_PARENTS: dict[str, set[str]] = {}

def _build_exc_tree() -> None:
    """Populate _EXCEPTION_PARENTS from the standard hierarchy."""
    tree: list[tuple[str, str]] = [
        # BaseException children
        ("Exception", "BaseException"),
        ("SystemExit", "BaseException"),
        ("KeyboardInterrupt", "BaseException"),
        ("GeneratorExit", "BaseException"),
        # Exception children
        ("ArithmeticError", "Exception"),
        ("AssertionError", "Exception"),
        ("AttributeError", "Exception"),
        ("BufferError", "Exception"),
        ("EOFError", "Exception"),
        ("ImportError", "Exception"),
        ("LookupError", "Exception"),
        ("MemoryError", "Exception"),
        ("NameError", "Exception"),
        ("OSError", "Exception"),
        ("ReferenceError", "Exception"),
        ("RuntimeError", "Exception"),
        ("StopIteration", "Exception"),
        ("StopAsyncIteration", "Exception"),
        ("SyntaxError", "Exception"),
        ("SystemError", "Exception"),
        ("TypeError", "Exception"),
        ("ValueError", "Exception"),
        ("Warning", "Exception"),
        # ArithmeticError children
        ("FloatingPointError", "ArithmeticError"),
        ("OverflowError", "ArithmeticError"),
        ("ZeroDivisionError", "ArithmeticError"),
        # LookupError children
        ("IndexError", "LookupError"),
        ("KeyError", "LookupError"),
        # OSError children (aliases: IOError, EnvironmentError)
        ("FileExistsError", "OSError"),
        ("FileNotFoundError", "OSError"),
        ("PermissionError", "OSError"),
        ("TimeoutError", "OSError"),
        ("ConnectionError", "OSError"),
        ("ConnectionResetError", "ConnectionError"),
        ("ConnectionRefusedError", "ConnectionError"),
        ("ConnectionAbortedError", "ConnectionError"),
        # ImportError children
        ("ModuleNotFoundError", "ImportError"),
        # NameError children
        ("UnboundLocalError", "NameError"),
        # RuntimeError children
        ("NotImplementedError", "RuntimeError"),
        ("RecursionError", "RuntimeError"),
        # SyntaxError children
        ("IndentationError", "SyntaxError"),
        ("TabError", "IndentationError"),
        # ValueError children
        ("UnicodeError", "ValueError"),
        ("UnicodeDecodeError", "UnicodeError"),
        ("UnicodeEncodeError", "UnicodeError"),
        # Common library exceptions
        ("HTTPException", "Exception"),
        ("RequestValidationError", "ValueError"),
        ("ValidationError", "ValueError"),
        ("IntegrityError", "Exception"),
        ("OperationalError", "Exception"),
        ("ProgrammingError", "Exception"),
        ("DatabaseError", "Exception"),
        ("DataError", "DatabaseError"),
        ("NotFoundError", "Exception"),
        ("AuthenticationError", "Exception"),
        ("AuthorizationError", "Exception"),
        ("PermissionDenied", "Exception"),
        ("DoesNotExist", "Exception"),
        ("MultipleObjectsReturned", "Exception"),
        ("Timeout", "Exception"),
        ("ConnectionRefused", "ConnectionError"),
    ]
    for child, parent in tree:
        ancestors: set[str] = set()
        ancestors.add(child)
        ancestors.add(parent)
        # Walk parent chain
        p = parent
        while p in {ch: pa for ch, pa in tree}:
            p = {ch: pa for ch, pa in tree}[p]
            ancestors.add(p)
        _EXCEPTION_PARENTS[child] = ancestors
    # BaseException is its own ancestor
    _EXCEPTION_PARENTS["BaseException"] = {"BaseException"}

_build_exc_tree()


def _exc_is_subclass(raised: str, handler: str) -> bool:
    """Check if `raised` would be caught by `except handler`.

    Uses the pre-built hierarchy. Unknown types are assumed to inherit
    from Exception (the common case for user-defined exceptions).
    """
    if not handler or handler == "BaseException":
        return True  # bare except or BaseException catches all
    if raised == handler:
        return True
    # Check hierarchy
    ancestors = _EXCEPTION_PARENTS.get(raised)
    if ancestors:
        return handler in ancestors
    # Unknown raised type: assume it inherits from Exception
    handler_ancestors = _EXCEPTION_PARENTS.get(handler)
    if handler_ancestors and "Exception" in handler_ancestors:
        return True  # handler catches Exception subtree, unknown is assumed in it
    if handler == "Exception":
        return True
    return False


def register_project_exceptions(call_graph: "CallGraphBuilder") -> None:
    """Discover user-defined exception classes from the call graph and
    register them in _EXCEPTION_PARENTS so that resolve_raise_target
    can match project-specific exception types."""
    for func in call_graph._functions.values():
        if func.definition_type != "class":
            continue
        for base in func.bases:
            base_simple = base.split(".")[-1]
            if base_simple in _EXCEPTION_PARENTS or base_simple in (
                "Exception", "BaseException", "RuntimeError", "ValueError",
                "TypeError", "KeyError", "OSError", "IOError",
                "HTTPException",
            ):
                # This class inherits from a known exception
                child_name = func.name
                if child_name in _EXCEPTION_PARENTS:
                    continue
                ancestors = {child_name}
                # Copy parent's ancestors
                parent_ancestors = _EXCEPTION_PARENTS.get(base_simple, {base_simple, "Exception", "BaseException"})
                ancestors.update(parent_ancestors)
                _EXCEPTION_PARENTS[child_name] = ancestors


@dataclass
class _ExceptHandler:
    """An except handler target with its exception type for matching."""
    block_id: str
    exc_type: str  # "Exception", "ValueError", etc. "" = bare except


@dataclass
class _FinallyFrame:
    """One level of try-finally nesting."""
    stmts: list  # ast.stmt list (raw AST to clone per exit path)


@dataclass
class _Ctx:
    """Walker context — carries targets for control flow jumps."""
    func: FunctionDef
    exit_id: str
    error_exit_id: str
    except_handlers: list[_ExceptHandler] = field(default_factory=list)
    loop_exit_id: str | None = None
    loop_header_id: str | None = None
    # Stack of finally clauses, innermost last.
    # Non-local exits (return/raise/break/continue) must pass through
    # all frames from innermost to outermost before reaching their target.
    finally_stack: list[_FinallyFrame] = field(default_factory=list)

    def with_except(self, handlers: list[_ExceptHandler]) -> _Ctx:
        return _Ctx(
            func=self.func, exit_id=self.exit_id,
            error_exit_id=self.error_exit_id,
            except_handlers=handlers,
            loop_exit_id=self.loop_exit_id,
            loop_header_id=self.loop_header_id,
            finally_stack=list(self.finally_stack),
        )

    def with_loop(self, header_id: str, exit_id: str) -> _Ctx:
        return _Ctx(
            func=self.func, exit_id=self.exit_id,
            error_exit_id=self.error_exit_id,
            except_handlers=self.except_handlers,
            loop_exit_id=exit_id,
            loop_header_id=header_id,
            finally_stack=list(self.finally_stack),
        )

    def with_finally(self, stmts: list) -> _Ctx:
        """Push a finally frame onto the stack."""
        return _Ctx(
            func=self.func, exit_id=self.exit_id,
            error_exit_id=self.error_exit_id,
            except_handlers=self.except_handlers,
            loop_exit_id=self.loop_exit_id,
            loop_header_id=self.loop_header_id,
            finally_stack=list(self.finally_stack) + [_FinallyFrame(stmts=stmts)],
        )

    def pop_finally(self) -> _Ctx:
        """Return ctx with innermost finally removed (for walking the finally body)."""
        return _Ctx(
            func=self.func, exit_id=self.exit_id,
            error_exit_id=self.error_exit_id,
            except_handlers=self.except_handlers,
            loop_exit_id=self.loop_exit_id,
            loop_header_id=self.loop_header_id,
            finally_stack=list(self.finally_stack[:-1]),
        )

    @property
    def has_finally(self) -> bool:
        return len(self.finally_stack) > 0

    @property
    def innermost_finally(self) -> _FinallyFrame | None:
        return self.finally_stack[-1] if self.finally_stack else None

    def resolve_raise_target(self, raised_type: str) -> str | None:
        """Find the best matching except handler for a raised type."""
        for h in self.except_handlers:
            handler_type = h.exc_type or "BaseException"  # bare except
            if _exc_is_subclass(raised_type, handler_type):
                return h.block_id
        return None


class CFGBuilder:
    """Build a ControlFlowGraph from a Python function's AST."""

    def __init__(self, call_graph: CallGraphBuilder):
        self.cg = call_graph
        self.cg.analyze_project()
        self._block_counter = 0
        self._edge_counter = 0
        self._cfg = ControlFlowGraph()

    def build(
        self,
        handler_name: str,
        handler_file: str,
        handler_line: int | None = None,
    ) -> ControlFlowGraph:
        # Fresh CFG per build call — prevents block accumulation across
        # multiple build() invocations on the same CFGBuilder instance.
        self._cfg = ControlFlowGraph()
        self._block_counter = 0
        self._edge_counter = 0

        # Register project-specific exception classes for hierarchy matching
        register_project_exceptions(self.cg)

        func = self.cg._find_function(handler_name, handler_file, handler_line)
        if not func:
            return self._cfg

        ast_node = self.cg.get_ast_node(func.qualified_name)
        if not ast_node or not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self._cfg

        self._cfg.function_name = func.qualified_name
        self._cfg.file_path = func.file_path

        entry = self._new_block(label="entry", kind="entry",
                                file_path=func.file_path, line_start=ast_node.lineno)
        exit_block = self._new_block(label="exit", kind="exit",
                                     file_path=func.file_path)
        error_exit = self._new_block(label="error exit", kind="error_exit",
                                     file_path=func.file_path)

        ctx = _Ctx(func=func, exit_id=exit_block.id, error_exit_id=error_exit.id)

        tail_ids = self._walk_body(ast_node.body, ctx, entry.id)

        for tid in tail_ids:
            if tid and tid != exit_block.id and tid != error_exit.id:
                self._add_edge(tid, exit_block.id, kind="fall_through")

        # Remove unreachable terminal blocks
        edge_targets = {e.target_block_id for e in self._cfg.edges}
        if exit_block.id not in edge_targets:
            self._cfg.blocks = [b for b in self._cfg.blocks if b.id != exit_block.id]
        if error_exit.id not in edge_targets:
            self._cfg.blocks = [b for b in self._cfg.blocks if b.id != error_exit.id]

        return self._cfg

    # ------------------------------------------------------------------
    # Core walker
    # ------------------------------------------------------------------

    def _walk_body(self, stmts: list[ast.stmt], ctx: _Ctx, current_block_id: str) -> list[str]:
        """Walk statements, splitting into blocks. Returns tail block IDs."""
        tail_ids = [current_block_id]
        for stmt in stmts:
            new_tails: list[str] = []
            for tid in tail_ids:
                new_tails.extend(self._process_stmt(stmt, ctx, tid))
            tail_ids = [t for t in new_tails if t]
        return tail_ids

    def _process_stmt(self, stmt: ast.stmt, ctx: _Ctx, block_id: str) -> list[str]:
        """Process one statement. Returns tail block IDs."""

        if isinstance(stmt, ast.If):
            return self._handle_if(stmt, ctx, block_id)

        _try_types = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
        if isinstance(stmt, _try_types):
            return self._handle_try(stmt, ctx, block_id)

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            return self._handle_for(stmt, ctx, block_id)
        if isinstance(stmt, ast.While):
            return self._handle_while(stmt, ctx, block_id)

        # --- With / Async With ---
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self._handle_with(stmt, ctx, block_id)

        # --- Match (Python 3.10+) ---
        if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
            return self._handle_match(stmt, ctx, block_id)

        # --- Return ---
        if isinstance(stmt, ast.Return):
            text = f"return {self._unparse(stmt.value)}" if stmt.value else "return"
            self._add_stmt(block_id, stmt.lineno, text=text[:80], kind="return")
            self._emit_finally_then(ctx, block_id, ctx.exit_id, "exit", "return")
            return []

        # --- Raise ---
        if isinstance(stmt, ast.Raise):
            text = f"raise {self._unparse(stmt.exc)}" if stmt.exc else "raise"
            self._add_stmt(block_id, stmt.lineno, text=text[:80], kind="raise")
            raised_type = ""
            if stmt.exc:
                if isinstance(stmt.exc, ast.Call):
                    raised_type = self._get_call_name(stmt.exc)
                elif isinstance(stmt.exc, ast.Name):
                    raised_type = stmt.exc.id
            # Route to matched except handler, or error_exit
            if ctx.except_handlers:
                target = ctx.resolve_raise_target(raised_type)
                if target:
                    self._emit_finally_then(ctx, block_id, target, "exception",
                                            f"raise {raised_type}" if raised_type else "raise")
                else:
                    self._emit_finally_then(ctx, block_id, ctx.error_exit_id, "exit",
                                            f"raise {raised_type}" if raised_type else "raise")
            else:
                self._emit_finally_then(ctx, block_id, ctx.error_exit_id, "exit", "raise")
            return []

        # --- Break ---
        if isinstance(stmt, ast.Break):
            self._add_stmt(block_id, stmt.lineno, text="break", kind="break")
            if ctx.loop_exit_id:
                self._emit_finally_then(ctx, block_id, ctx.loop_exit_id, "fall_through", "break")
            return []

        # --- Continue ---
        if isinstance(stmt, ast.Continue):
            self._add_stmt(block_id, stmt.lineno, text="continue", kind="continue")
            if ctx.loop_header_id:
                self._emit_finally_then(ctx, block_id, ctx.loop_header_id, "back_edge", "continue")
            return []

        # --- Nested function def: skip ---
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return [block_id]

        # --- Regular statements ---
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return [block_id]  # docstring
            # Yield: does not terminate block (generator continues after yield)
            if isinstance(stmt.value, (ast.Yield, ast.YieldFrom)):
                text = self._unparse(stmt.value)
                self._add_stmt(block_id, stmt.lineno, text=text[:80], kind="yield")
                return [block_id]
            text = self._unparse(stmt.value)
            kind = "call" if isinstance(stmt.value, (ast.Call, ast.Await)) else "expr"
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            text = self._unparse(stmt)
            kind = "assign"
        else:
            text = self._unparse(stmt)
            kind = "stmt"

        self._add_stmt(block_id, stmt.lineno, text=text[:80], kind=kind)
        return [block_id]

    # ------------------------------------------------------------------
    # Compound statement handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _human_branch_labels(test_node) -> tuple[str, str]:
        """Derive readable true/false branch labels from an if-test AST node.

        For `if not X:` → true="not X", false="X" (removes double-negation).
        For `if X is None:` → true="None", false="exists".
        For simple `if X:` → true="yes", false="no".
        """
        try:
            cond = ast.unparse(test_node) if hasattr(ast, "unparse") else ""
        except Exception:
            cond = ""

        # `if not X:` → true branch means X is falsy
        if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not):
            inner = ast.unparse(test_node.operand) if hasattr(ast, "unparse") else "X"
            inner = inner[:30]
            return (f"not {inner}", inner)

        # `if X is None:` / `if X is not None:`
        if isinstance(test_node, ast.Compare) and len(test_node.comparators) == 1:
            op = test_node.ops[0]
            right = test_node.comparators[0]
            if isinstance(right, ast.Constant) and right.value is None:
                left = ast.unparse(test_node.left) if hasattr(ast, "unparse") else "X"
                left = left[:30]
                if isinstance(op, ast.Is):
                    return (f"{left} is None", f"{left} exists")
                if isinstance(op, ast.IsNot):
                    return (f"{left} exists", f"{left} is None")

        # `if X == value:` / `if X != value:`
        if isinstance(test_node, ast.Compare) and len(test_node.comparators) == 1:
            op = test_node.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                left = ast.unparse(test_node.left) if hasattr(ast, "unparse") else ""
                right_str = ast.unparse(test_node.comparators[0]) if hasattr(ast, "unparse") else ""
                if len(left) + len(right_str) < 40:
                    if isinstance(op, ast.Eq):
                        return (f"{left}=={right_str}", f"{left}!={right_str}")
                    return (f"{left}!={right_str}", f"{left}=={right_str}")

        return ("yes", "no")

    def _handle_if(self, stmt, ctx: _Ctx, block_id: str) -> list[str]:
        condition = self._unparse(stmt.test)
        explanation = self._explain_branch(stmt.test, stmt.body, stmt.orelse)
        self._add_stmt(block_id, stmt.lineno,
                       text=f"if {condition}"[:80], kind="branch_test")
        # Store explanation on the block
        block = next(b for b in self._cfg.blocks if b.id == block_id)
        if explanation:
            block.metadata["branch_explanation"] = explanation

        true_label, false_label = self._human_branch_labels(stmt.test)

        true_block = self._new_block(
            label=f"if {self._compact(condition)}", kind="block",
            file_path=ctx.func.file_path,
            line_start=stmt.body[0].lineno if stmt.body else stmt.lineno,
        )
        self._add_edge(block_id, true_block.id, kind="true",
                       condition=condition, label=true_label)
        true_tails = self._walk_body(stmt.body, ctx, true_block.id)

        if stmt.orelse:
            false_block = self._new_block(
                label="else", kind="block",
                file_path=ctx.func.file_path,
                line_start=stmt.orelse[0].lineno,
            )
            self._add_edge(block_id, false_block.id, kind="false",
                           condition=condition, label=false_label)
            false_tails = self._walk_body(stmt.orelse, ctx, false_block.id)
        else:
            merge = self._new_block(label="merge", kind="merge",
                                    file_path=ctx.func.file_path)
            self._add_edge(block_id, merge.id, kind="false",
                           condition=condition, label=false_label)
            false_tails = [merge.id]

        all_tails = true_tails + false_tails
        if len(all_tails) > 1:
            merge = self._new_block(label="merge", kind="merge",
                                    file_path=ctx.func.file_path)
            for tid in all_tails:
                if tid:
                    self._add_edge(tid, merge.id, kind="fall_through")
            return [merge.id]
        return all_tails

    def _emit_finally_then(
        self, ctx: _Ctx, from_id: str, target_id: str,
        edge_kind: str, label: str,
    ) -> None:
        """If finally clause(s) are active, emit finally copies between
        from_id and target_id.  Otherwise, emit a direct edge.

        For nested try/finally, walks through all finally frames from
        innermost to outermost before reaching the final target.
        The finally body is walked with:
          - The current except_handlers preserved (so raise in finally
            can route to an enclosing except)
          - The finally stack popped by one (so outer finallys still apply
            but the current one doesn't recurse)
        """
        if not ctx.has_finally:
            self._add_edge(from_id, target_id, kind=edge_kind, label=label)
            return

        frame = ctx.innermost_finally
        assert frame is not None

        fin_block = self._new_block(
            label=f"finally ({label})", kind="block",
            file_path=ctx.func.file_path,
            line_start=frame.stmts[0].lineno if frame.stmts else None,
        )
        self._add_edge(from_id, fin_block.id, kind="fall_through", label="finally")

        # Walk finally body with innermost finally popped:
        #  - except_handlers preserved (raise in finally → enclosing except)
        #  - outer finally frames preserved (nested finally chains)
        popped_ctx = ctx.pop_finally()
        fin_tails = self._walk_body(frame.stmts, popped_ctx, fin_block.id)
        # Connect finally tails to target — recursively through any
        # remaining outer finally frames.
        for tid in fin_tails:
            if tid:
                self._emit_finally_then(popped_ctx, tid, target_id, edge_kind, label)

    def _handle_try(self, stmt, ctx: _Ctx, block_id: str) -> list[str]:
        has_finally = hasattr(stmt, 'finalbody') and stmt.finalbody

        # Inner context: if finally exists, carry stmts so return/raise/break/continue
        # will clone a finally block before reaching their target.
        inner_ctx = ctx.with_finally(stmt.finalbody) if has_finally else ctx

        # 1. Build except handler blocks
        except_handlers: list[_ExceptHandler] = []
        handler_tails: list[str] = []
        for handler in stmt.handlers:
            exc = self._unparse(handler.type) if handler.type else ""
            exc_label = exc or "Exception"
            handler_block = self._new_block(
                label=f"except {exc_label}", kind="block",
                file_path=ctx.func.file_path,
                line_start=handler.lineno,
            )
            except_handlers.append(_ExceptHandler(block_id=handler_block.id, exc_type=exc))
            h_tails = self._walk_body(handler.body, inner_ctx, handler_block.id)
            handler_tails.extend(h_tails)

        # 2. Walk try body with except targets
        #    Only override except_handlers if this try has handlers.
        #    Otherwise, keep the enclosing handlers (for re-raise propagation).
        if except_handlers:
            try_ctx = inner_ctx.with_except(except_handlers)
        else:
            try_ctx = inner_ctx
        try_tails = self._walk_body(stmt.body, try_ctx, block_id)

        # 2b. Implicit exception edges: any call inside try body can raise,
        #     so connect the try entry block to each handler as a potential path.
        #     Explicit raises already have edges from _process_stmt; these are
        #     additional "implicit raise from callee" paths.
        if except_handlers:
            for handler in except_handlers:
                # Only add if not already connected by an explicit raise
                already = any(
                    e.source_block_id == block_id and e.target_block_id == handler.block_id
                    for e in self._cfg.edges
                )
                if not already:
                    exc_label = handler.exc_type or "Exception"
                    self._add_edge(block_id, handler.block_id, kind="exception",
                                   label=f"except {exc_label}")

        # 3. else clause
        else_tails: list[str] = []
        if stmt.orelse:
            else_block = self._new_block(
                label="else (no exception)", kind="block",
                file_path=ctx.func.file_path,
                line_start=stmt.orelse[0].lineno,
            )
            for tid in try_tails:
                if tid:
                    self._add_edge(tid, else_block.id, kind="fall_through", label="else")
            else_tails = self._walk_body(stmt.orelse, inner_ctx, else_block.id)
        else:
            else_tails = try_tails

        # 4. Collect all non-finally tails (normal fall-through paths)
        all_tails = else_tails + handler_tails
        all_tails = [t for t in all_tails if t]

        # 5. Normal fall-through finally (for paths that didn't return/raise/break)
        if has_finally and all_tails:
            finally_block = self._new_block(
                label="finally", kind="block",
                file_path=ctx.func.file_path,
                line_start=stmt.finalbody[0].lineno,
            )
            for tid in all_tails:
                self._add_edge(tid, finally_block.id, kind="fall_through", label="finally")
            # Walk with OUTER ctx (no finally_stmts to prevent recursion)
            all_tails = self._walk_body(stmt.finalbody, ctx, finally_block.id)

        return [t for t in all_tails if t]

    def _handle_for(self, stmt, ctx: _Ctx, block_id: str) -> list[str]:
        target = self._unparse(stmt.target)
        iter_expr = self._unparse(stmt.iter)

        header = self._new_block(
            label=f"for {target} in {self._compact(iter_expr)}",
            kind="block", file_path=ctx.func.file_path,
            line_start=stmt.lineno,
        )
        header.statements.append(CFGStatement(
            line=stmt.lineno,
            text=f"for {target} in {iter_expr}"[:80],
            kind="loop_header",
        ))
        self._add_edge(block_id, header.id, kind="fall_through")

        # Post-loop merge (break target)
        post = self._new_block(label="after loop", kind="merge",
                               file_path=ctx.func.file_path)

        body_block = self._new_block(
            label="loop body", kind="block",
            file_path=ctx.func.file_path,
            line_start=stmt.body[0].lineno if stmt.body else stmt.lineno,
        )
        self._add_edge(header.id, body_block.id, kind="true", label="iterate")

        loop_ctx = ctx.with_loop(header.id, post.id)
        body_tails = self._walk_body(stmt.body, loop_ctx, body_block.id)

        for tid in body_tails:
            if tid:
                self._add_edge(tid, header.id, kind="back_edge", label="next")

        self._add_edge(header.id, post.id, kind="false", label="done")
        return [post.id]

    def _handle_while(self, stmt, ctx: _Ctx, block_id: str) -> list[str]:
        condition = self._unparse(stmt.test)

        header = self._new_block(
            label=f"while {self._compact(condition)}",
            kind="block", file_path=ctx.func.file_path,
            line_start=stmt.lineno,
        )
        header.statements.append(CFGStatement(
            line=stmt.lineno,
            text=f"while {condition}"[:80],
            kind="loop_header",
        ))
        self._add_edge(block_id, header.id, kind="fall_through")

        post = self._new_block(label="after while", kind="merge",
                               file_path=ctx.func.file_path)

        body_block = self._new_block(
            label="loop body", kind="block",
            file_path=ctx.func.file_path,
            line_start=stmt.body[0].lineno if stmt.body else stmt.lineno,
        )
        self._add_edge(header.id, body_block.id, kind="true",
                       condition=condition, label="yes")

        loop_ctx = ctx.with_loop(header.id, post.id)
        body_tails = self._walk_body(stmt.body, loop_ctx, body_block.id)

        for tid in body_tails:
            if tid:
                self._add_edge(tid, header.id, kind="back_edge", label="loop")

        self._add_edge(header.id, post.id, kind="false",
                       condition=condition, label="no")
        return [post.id]

    def _handle_with(self, stmt, ctx: _Ctx, block_id: str) -> list[str]:
        """with/async with: walk body with __exit__ cleanup as finally-like frame."""
        # Add context expression as a statement
        for item in stmt.items:
            text = self._unparse(item.context_expr)
            if item.optional_vars:
                text = f"{self._unparse(item.optional_vars)} = {text}"
            prefix = "async with " if isinstance(stmt, ast.AsyncWith) else "with "
            self._add_stmt(block_id, stmt.lineno, text=(prefix + text)[:80], kind="call")

        # Model __exit__ as a finally frame so return/raise inside body
        # will go through cleanup
        exit_stmts = [ast.parse("__exit__()").body[0]]  # synthetic cleanup
        inner_ctx = ctx.with_finally(exit_stmts)
        body_tails = self._walk_body(stmt.body, inner_ctx, block_id)

        # Normal exit also goes through __exit__
        if body_tails:
            cleanup = self._new_block(
                label="__exit__", kind="merge",
                file_path=ctx.func.file_path,
            )
            for tid in body_tails:
                self._add_edge(tid, cleanup.id, kind="fall_through")
            return [cleanup.id]
        return body_tails

    def _handle_match(self, stmt, ctx: _Ctx, block_id: str) -> list[str]:
        """match/case (Python 3.10+): each case is a branch."""
        subject = self._unparse(stmt.subject)
        self._add_stmt(block_id, stmt.lineno,
                       text=f"match {subject}"[:80], kind="branch_test")

        case_tails: list[str] = []
        for i, case in enumerate(stmt.cases):
            pattern = self._unparse(case.pattern) if hasattr(case, 'pattern') else f"case {i}"
            is_wildcard = isinstance(case.pattern, ast.MatchAs) and case.pattern.name is None if hasattr(ast, 'MatchAs') else False

            case_block = self._new_block(
                label=f"case {pattern}" if not is_wildcard else "case _",
                kind="block", file_path=ctx.func.file_path,
                line_start=case.body[0].lineno if case.body else stmt.lineno,
            )
            edge_kind = "false" if is_wildcard else "true"
            self._add_edge(block_id, case_block.id, kind=edge_kind,
                           label=pattern[:30] if not is_wildcard else "default")
            tails = self._walk_body(case.body, ctx, case_block.id)
            case_tails.extend(tails)

        case_tails = [t for t in case_tails if t]
        if len(case_tails) > 1:
            merge = self._new_block(label="merge", kind="merge",
                                    file_path=ctx.func.file_path)
            for tid in case_tails:
                self._add_edge(tid, merge.id, kind="fall_through")
            return [merge.id]
        return case_tails

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _new_block(self, **kwargs) -> BasicBlock:
        self._block_counter += 1
        block = BasicBlock(id=f"bb.{self._block_counter}", **kwargs)
        self._cfg.blocks.append(block)
        return block

    def _add_edge(self, src: str, tgt: str, kind: str = "fall_through",
                  label: str = "", condition: str = "") -> None:
        self._edge_counter += 1
        self._cfg.edges.append(CFGEdge(
            id=f"ce.{self._edge_counter}",
            source_block_id=src,
            target_block_id=tgt,
            kind=kind, label=label, condition=condition,
        ))

    def _add_stmt(self, block_id: str, line: int, text: str = "",
                  kind: str = "", line_end: int | None = None) -> None:
        block = next((b for b in self._cfg.blocks if b.id == block_id), None)
        if block:
            block.statements.append(CFGStatement(
                line=line, line_end=line_end, text=text, kind=kind,
            ))
            if block.line_start is None:
                block.line_start = line
            block.line_end = line_end or line

    @staticmethod
    def _explain_branch(test: ast.expr, body: list, orelse: list) -> str:
        """Generate a human-readable explanation for a branch condition."""
        # Pattern: if x is None / if x is not None → guard clause
        if isinstance(test, ast.Compare):
            if len(test.ops) == 1:
                op = test.ops[0]
                comparator = test.comparators[0]
                left_name = _branch_subject_name(test.left)

                # x is None
                if isinstance(op, ast.Is) and isinstance(comparator, ast.Constant) and comparator.value is None:
                    if _body_is_error(body):
                        return f"Guard: reject if {left_name} not found"
                    return f"Check: {left_name} is missing"

                # x is not None
                if isinstance(op, ast.IsNot) and isinstance(comparator, ast.Constant) and comparator.value is None:
                    return f"Check: proceed if {left_name} exists"

                # x == value / x != value
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    return f"Match: compare {left_name}"

                # x > 0, len(x) == 0, etc → validation
                if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                    return f"Validate: check {left_name} bounds"

        # Pattern: not x → truthiness guard (simple operands only)
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            if isinstance(test.operand, (ast.Name, ast.Attribute)):
                subject = _branch_subject_name(test.operand)
                if _body_is_error(body):
                    return f"Guard: reject if {subject} is empty or false"
                return f"Check: {subject} is falsy"

        # Pattern: if x → truthiness check
        if isinstance(test, ast.Name):
            if _body_is_error(body):
                return f"Guard: reject unless {test.id}"
            return f"Check: {test.id} is truthy"

        # Pattern: if x.attr → mode selection / feature flag
        if isinstance(test, ast.Attribute):
            attr = test.attr
            obj = _branch_subject_name(test.value)
            return f"Mode: branch on {obj}.{attr}"

        # Pattern: isinstance(x, T) → type dispatch
        if isinstance(test, ast.Call):
            func_name = ""
            if isinstance(test.func, ast.Name):
                func_name = test.func.id
            elif isinstance(test.func, ast.Attribute):
                func_name = test.func.attr
            if func_name == "isinstance" and len(test.args) >= 2:
                subject = _branch_subject_name(test.args[0])
                type_name = _branch_subject_name(test.args[1])
                return f"Type dispatch: {subject} is {type_name}"
            if func_name == "hasattr" and len(test.args) >= 2:
                subject = _branch_subject_name(test.args[0])
                return f"Feature check: {subject} has attribute"
            # Method calls: x.startswith(), x.endswith(), x.is_valid(), etc.
            if isinstance(test.func, ast.Attribute):
                method = test.func.attr
                obj = _branch_subject_name(test.func.value)
                if method in ("startswith", "endswith", "contains"):
                    return f"String check: {obj}.{method}()"
                if method.startswith("is_") or method.startswith("has_"):
                    return f"Flag: {obj}.{method}()"
                if method in ("exists", "count", "any", "all"):
                    return f"Existence check: {obj}.{method}()"

        # Pattern: x and y / x or y → compound condition
        if isinstance(test, ast.BoolOp):
            parts = []
            for val in test.values[:3]:  # limit to 3 sub-conditions
                sub = CFGBuilder._explain_branch(val, body, orelse)
                if sub:
                    parts.append(sub)
                else:
                    parts.append(_branch_subject_name(val))
            joiner = " AND " if isinstance(test.op, ast.And) else " OR "
            return f"Combined: {joiner.join(parts)}"

        # Pattern: x in collection / x not in collection → membership test
        if isinstance(test, ast.Compare):
            if len(test.ops) == 1:
                op = test.ops[0]
                left_name = _branch_subject_name(test.left)
                right_name = _branch_subject_name(test.comparators[0])
                if isinstance(op, ast.In):
                    return f"Membership: {left_name} in {right_name}"
                if isinstance(op, ast.NotIn):
                    return f"Exclusion: {left_name} not in {right_name}"

        # Pattern: not isinstance(...), not x.method() → negated complex
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            inner = CFGBuilder._explain_branch(test.operand, body, orelse)
            if inner and not inner.startswith("Check:"):
                return f"Negated {inner.lower()}"
            # Fallback for not func() / not obj.method()
            subject = _branch_subject_name(test.operand)
            if _body_is_error(body):
                return f"Guard: reject if not {subject}"
            return f"Check: not {subject}"

        return ""

    @staticmethod
    def _get_call_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    @staticmethod
    def _unparse(node: ast.AST | None) -> str:
        if node is None:
            return ""
        if hasattr(ast, "unparse"):
            return ast.unparse(node)
        return "<expr>"

    @staticmethod
    def _compact(s: str, max_len: int = 35) -> str:
        s = " ".join(s.split())
        return s[:max_len] + "..." if len(s) > max_len else s
