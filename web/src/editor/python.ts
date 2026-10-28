// A richer Python tokenizer than Monaco's built-in one (which colours `def`, `if`, `self`,
// `len` and `True` all as "keyword"): control flow vs. declarations, constants, self/cls,
// builtins, def/class names, calls, decorators, f-string fields. Semantic tokens from the
// language server refine it further (parameters, properties, modules, …).
import type * as Monaco from "monaco-editor";

const ident = {
  cases: {
    "@control": "keyword.control",
    "@keywords": "keyword",
    "@constants": "constant.language",
    "@selfs": "variable.language",
    "@builtinTypes": "support.type",
    "@builtinFunctions": "support.function",
    "~[A-Z][A-Z0-9_]*[A-Z0-9]": "constant",
    "~_*[A-Z]\\w*[a-z]\\w*": "entity.name.type",
    "@default": "identifier",
  },
};
const callee = {
  cases: {
    "@control": "keyword.control",
    "@keywords": "keyword",
    "@selfs": "variable.language",
    "@builtinTypes": "support.type",
    "@builtinFunctions": "support.function",
    "~_*[A-Z]\\w*[a-z]\\w*": "entity.name.type",
    "@default": "function.call",
  },
};

export const pythonLanguage: Monaco.languages.IMonarchLanguage = {
  defaultToken: "",
  tokenPostfix: ".python",
  control: [
    "if", "elif", "else", "for", "while", "break", "continue", "return", "yield", "try", "except", "finally",
    "raise", "with", "assert", "pass", "import", "from", "as", "del", "await",
  ],
  keywords: ["def", "class", "lambda", "and", "or", "not", "in", "is", "async", "global", "nonlocal"],
  constants: ["True", "False", "None", "NotImplemented", "Ellipsis", "__debug__"],
  selfs: ["self", "cls"],
  builtinFunctions: [
    "abs", "aiter", "all", "anext", "any", "ascii", "bin", "breakpoint", "callable", "chr", "compile", "delattr",
    "dir", "divmod", "enumerate", "eval", "exec", "filter", "format", "getattr", "globals", "hasattr", "hash",
    "help", "hex", "id", "input", "isinstance", "issubclass", "iter", "len", "locals", "map", "max", "min", "next",
    "oct", "open", "ord", "pow", "print", "repr", "reversed", "round", "setattr", "sorted", "sum", "vars", "zip",
    "__import__",
  ],
  builtinTypes: [
    "bool", "bytearray", "bytes", "classmethod", "complex", "dict", "float", "frozenset", "int", "list",
    "memoryview", "object", "property", "range", "set", "slice", "staticmethod", "str", "super", "tuple", "type",
    "BaseException", "BaseExceptionGroup", "Exception", "ExceptionGroup", "ArithmeticError", "AssertionError",
    "AttributeError", "BlockingIOError", "BrokenPipeError", "BufferError", "ChildProcessError", "ConnectionError",
    "EOFError", "FileExistsError", "FileNotFoundError", "FloatingPointError", "GeneratorExit", "ImportError",
    "IndentationError", "IndexError", "InterruptedError", "IsADirectoryError", "KeyError", "KeyboardInterrupt",
    "LookupError", "MemoryError", "ModuleNotFoundError", "NameError", "NotADirectoryError", "NotImplementedError",
    "OSError", "OverflowError", "PermissionError", "ProcessLookupError", "RecursionError", "ReferenceError",
    "RuntimeError", "StopAsyncIteration", "StopIteration", "SyntaxError", "SystemError", "SystemExit", "TabError",
    "TimeoutError", "TypeError", "UnboundLocalError", "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError",
    "ValueError", "ZeroDivisionError", "Warning", "DeprecationWarning", "FutureWarning", "RuntimeWarning",
    "UserWarning",
  ],
  brackets: [
    { open: "{", close: "}", token: "delimiter.curly" },
    { open: "[", close: "]", token: "delimiter.bracket" },
    { open: "(", close: ")", token: "delimiter.parenthesis" },
  ],

  tokenizer: {
    root: [
      // line-start constructs come before whitespace eats the indentation
      [/^\s*@[a-zA-Z_][\w.]*/, "decorator"],
      [/^\s*(?:match|case)\b(?=\s+[^=\s,)\].][^\n]*:\s*(#.*)?$)/, "keyword.control"], // soft keywords
      [/^\s*type\b(?=\s+[a-zA-Z_]\w*(\[[^\]]*\])?\s*=)/, "keyword"],
      [/\s+/, "white"],
      [/#.*$/, "comment"],
      [/(def)(\s+)([a-zA-Z_]\w*)/, ["keyword", "white", "entity.name.function"]],
      [/(class)(\s+)([a-zA-Z_]\w*)/, ["keyword", "white", "entity.name.class"]],
      // strings: the body state is parameterised by quote kind ($S2) and "x" + prefix ($S3)
      [/([rRbBuUfF]{1,2})(''')/, ["storage.string", { token: "string", next: "@s.tsq.x$1" }]],
      [/([rRbBuUfF]{1,2})(""")/, ["storage.string", { token: "string", next: "@s.tdq.x$1" }]],
      [/([rRbBuUfF]{1,2})(')/, ["storage.string", { token: "string", next: "@s.sq.x$1" }]],
      [/([rRbBuUfF]{1,2})(")/, ["storage.string", { token: "string", next: "@s.dq.x$1" }]],
      [/'''/, { token: "string", next: "@s.tsq.x" }],
      [/"""/, { token: "string", next: "@s.tdq.x" }],
      [/'/, { token: "string", next: "@s.sq.x" }],
      [/"/, { token: "string", next: "@s.dq.x" }],
      // numbers
      [/0[xX][0-9a-fA-F_]+/, "number.hex"],
      [/0[oO][0-7_]+|0[bB][01_]+/, "number"],
      [/\d[\d_]*(\.[\d_]*)?([eE][+-]?\d[\d_]*)?[jJ]?/, "number"],
      [/\.\d[\d_]*([eE][+-]?\d[\d_]*)?[jJ]?/, "number"],
      [/\.\.\./, "constant.language"],
      [/\.(?=\s*[a-zA-Z_])/, { token: "delimiter", next: "@attr" }], // obj.method( / obj.attr
      [/[a-zA-Z_]\w*(?=\s*\()/, callee],
      [/[a-zA-Z_]\w*/, ident],
      [/[{}()[\]]/, "@brackets"],
      [/->|:=|\*\*=?|\/\/=?|<<=?|>>=?|[+\-*/%&|^~<>!=]=?|@=/, "operator"],
      [/[;,.:\\]/, "delimiter"],
    ],

    // $S2 = sq | dq | tsq | tdq, $S3 = "x" + prefix (f → fields, r → raw)
    s: [
      [/\{\{|\}\}/, { cases: { "$S3~x.*[fF].*": "string.escape", "@default": "string" } }],
      [/\{/, { cases: { "$S3~x.*[fF].*": { token: "delimiter.fstring", next: "@fexpr" }, "@default": "string" } }],
      [/\\./, { cases: { "$S3~x.*[rR].*": "string", "@default": "string.escape" } }],
      [/\\$/, "string.escape"],
      [/'''/, { cases: { "$S2==tsq": { token: "string", next: "@pop" }, "@default": "string" } }],
      [/"""/, { cases: { "$S2==tdq": { token: "string", next: "@pop" }, "@default": "string" } }],
      [/'/, { cases: { "$S2==sq": { token: "string", next: "@pop" }, "@default": "string" } }],
      [/"/, { cases: { "$S2==dq": { token: "string", next: "@pop" }, "@default": "string" } }],
      // an unterminated single-line string ends with the line
      [/[^\\'"{}]+|[{}]/, { cases: { "@eos": { cases: { "$S2~sq|dq": { token: "string", next: "@pop" }, "@default": "string" } }, "@default": "string" } }],
    ],

    attr: [
      [/\s+/, "white"],
      [/[a-zA-Z_]\w*(?=\s*\()/, { token: "function.call", next: "@pop" }],
      [/[A-Z][A-Z0-9_]*[A-Z0-9]\b/, { token: "constant", next: "@pop" }],
      [/[a-zA-Z_]\w*/, { token: "identifier", next: "@pop" }],
    ],

    fexpr: [
      [/\}/, { token: "delimiter.fstring", next: "@pop" }],
      [/![rsa](?=[:}])/, "string.format"],
      [/:[^{}'"\n]*(?=\})/, "string.format"],
      { include: "@root" },
    ],
  },
};
