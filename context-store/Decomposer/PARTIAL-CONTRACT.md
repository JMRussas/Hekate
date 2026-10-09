# C# partial round-trip contract

Scope: `CSharpDecomposer` -> node store -> `CSharpGenerator`, one file at a time.
Tests: `PlanContracts.Tests/CSharpPartialRoundTripTests.cs` (real decomposer and generator over an
in-memory `ICodeNodeRepository`; every generated file is compiled with Roslyn against the trusted
platform references).

## Stored metadata

| Node | Attribute | Meaning |
|------|-----------|---------|
| `class`, `struct`, `method` | `is_partial = "true"` | `partial` was written. Absent otherwise. |
| `class`, `struct`, `method`, ... | `access` | Written access keyword, or `implicit` if none was written. |
| `class`, `method` | `is_static = "true"` | `static` was written (already stored before). |

`implicit` is metadata only. The generator never emits it as a keyword; it omits the access
modifier instead (types, fields, constructors, methods).

A method with no body has no `block` child. The decomposer already did this for `;` bodies.

## Generated shapes

- Type: `[access] [static] [partial] class|struct Name`
- Method: `[access] [static] [override|virtual|abstract] [partial] ReturnType Name(params)`
  - `block` child present: body follows.
  - No `block` child: the declaration ends with `;` (partial declaration, also abstract).
- Expression-bodied methods are stored as a block with one statement:
  non-void `return expr;`, void `expr;` (a void `return expr;` is CS0127).
  The void fix applies to every expression-bodied void method, partial or not.

Supported and compile-verified: partial class and struct pieces in separate files; declaration-only
and implemented partial methods; implicit access (`partial void M();`); explicit access with
non-void returns (`public partial int M();`); `static partial` methods and `static partial` classes;
expression-bodied implementations.

## File identity

Each file is decomposed and generated on its own. Partial pieces are separate nodes in separate
files; nothing aggregates or merges partial types across files or the project. Re-decomposing a
file replaces only that file's nodes. Cross-file consistency (declaration in one piece,
implementation in another) is the source's responsibility; the generator does not check it.

## Behavior changes to nonpartial output

These were bugs and now produce compilable output:
- `access = implicit` no longer emits the literal `implicit`.
- `is_static` is honored for types and methods (previously dropped).
- Bodyless methods end with `;`.
- Expression-bodied void methods no longer emit `return expr;`.

## Outstanding generator limits (not addressed)

- Not stored or not emitted: type modifiers other than access/`static`/`partial` (`abstract`,
  `sealed`, `unsafe`, ...); `base_types` and `generic_params` are stored but not emitted;
  method type parameters, constraints, `async`, `extern`, `new`, `ref`/`out`/`in`/`params`/`this`
  parameter modifiers and default values; attributes; XML docs.
- `static` on fields and properties-as-nodes is not emitted by the field handler (properties
  use stored full text and are fine). A static field inside a `static partial class` therefore
  will not compile.
- Namespaces are always emitted file-scoped; nested namespaces, multiple namespaces per file,
  records, interfaces, delegates, local functions and partial properties/events are not handled.
- Constructors without a body, `partial` on interfaces, and top-level members are not handled.
- The `NodeRepository` change is limited to implementing `ICodeNodeRepository`; persistence is
  untouched.
