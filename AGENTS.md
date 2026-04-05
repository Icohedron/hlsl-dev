# LLVM HLSL Developer Environment - Agent Instructions

This document provides instructions for AI coding agents operating in this repository. The repository defines a Nix-based development environment for working on LLVM's HLSL support, DirectXShaderCompiler (DXC), and related test suites.

## 1. Build and Test Commands

This project uses CMake, Ninja, and sccache within a Nix development shell. It relies on `mask` for task execution. The projects are stored as git submodules.

### Environment Setup
Before running commands, ensure you are in the Nix shell and submodules are initialized:
```bash
nix develop
mask setup
```

### Building the Code
Use the `mask` tasks defined in `maskfile.md` to configure and build LLVM and DXC:

*   **Configure LLVM:** `mask configure-llvm`
*   **Build LLVM:** `mask build-llvm`
*   **Configure DXC:** `mask configure-dxc`
*   **Build DXC:** `mask build-dxc`

To build a specific target manually:
```bash
cd ./llvm-project/build
ninja <target_name>      # e.g., ninja clang
```

### Running Tests
LLVM uses `lit` (LLVM Integrated Tester) for its test suites.

*   **Run all Clang tests:**
    ```bash
    cd ./llvm-project/build
    ninja check-clang
    ```
*   **Run all LLVM tests:**
    ```bash
    cd ./llvm-project/build
    ninja check-llvm
    ```
*   **Run HLSL-specific tests (if configured under an external project or specific suite):**
    ```bash
    cd ./llvm-project/build
    ninja check-hlsl
    ```
*   **Run a SINGLE Test (Highly Recommended for iterative development):**
    Use the `llvm-lit` utility (built in the `bin/` directory of the build folder) and point it to the specific test file:
    ```bash
    cd ./llvm-project/build
    bin/llvm-lit ../clang/test/HLSL/your_test_file.hlsl
    # Or for verbose output on failure:
    bin/llvm-lit -v ../clang/test/HLSL/your_test_file.hlsl
    ```

### Linting & Formatting
*   **C++ Formatting:** Use `clang-format`. LLVM has an in-tree `.clang-format` file. Always format changes before committing:
    ```bash
    git clang-format HEAD~1
    ```
*   **Tidy:** Use `clang-tidy` for static analysis if configured, but prefer standard LLVM review practices.

## 2. Code Style Guidelines (LLVM / C++)

When writing or modifying C++ code for LLVM/Clang or DXC, strictly adhere to the [LLVM Coding Standards](https://llvm.org/docs/CodingStandards.html).

### Formatting and Whitespace
*   **Indentation:** 2 spaces. No tabs.
*   **Line Length:** 80 characters maximum.
*   **Braces:** Braces open on the same line for control flow (`if`, `for`, `while`). Functions and classes open braces on the next line. Single-statement `if` blocks generally omit braces unless required for clarity or part of a chain where one block needs them.
*   **Includes:** Sort includes logically. LLVM includes first, then Clang includes, then system headers. Use `clang-format` to automatically sort them.

### Naming Conventions
*   **Types (Classes, Structs, Enums, Typedefs):** `UpperCamelCase` (e.g., `Type`, `TargetInfo`).
*   **Functions:** `lowerCamelCase` (Wait, LLVM recently updated their standard to `lowerCamelCase` for functions, though older code might still use `UpperCamelCase`. Follow the surrounding file's convention, but prefer `lowerCamelCase` for new code in LLVM).
*   **Variables:** `UpperCamelCase` is historically used for local variables in LLVM, but the community is migrating. Follow the local file convention strictly.
*   **Private Members:** Usually start with an uppercase letter, or follow the class's established pattern.
*   **Macros:** `UPPER_SNAKE_CASE` (e.g., `LLVM_DEBUG`).

### Types and C++ Features
*   **No RTTI:** Do not use `dynamic_cast` or `typeid`. Use LLVM's RTTI system (`isa<>`, `cast<>`, `dyn_cast<>`).
    *   `isa<T>(ptr)`: Returns true if `ptr` is of type `T`.
    *   `cast<T>(ptr)`: Casts `ptr` to `T`, asserts if it's the wrong type.
    *   `dyn_cast<T>(ptr)`: Casts `ptr` to `T`, returns `nullptr` if it's the wrong type.
*   **No Exceptions:** Exceptions are disabled (`-fno-exceptions`). Do not use `try`, `catch`, or `throw`.
*   **Auto:** Use `auto` judiciously. Use it when the type is obvious from context (e.g., `auto *Node = cast<CXXRecordDecl>(D);`) or for iterators. Do not use it if it makes the code harder to read.
*   **Pointers and References:** Use references when a null value is logically impossible. Use pointers when a value can be null.
*   **Strings:** Prefer `llvm::StringRef` over `std::string` or `const char*` for passing string data without copying. Prefer `llvm::Twine` for concatenating strings.

### Error Handling
Since exceptions are banned, use LLVM's error handling constructs:
*   **Recoverable Errors:** Use `llvm::Expected<T>` (returns either a value of `T` or an `llvm::Error`) or `llvm::Error` (for functions that return `void` on success).
*   **Checking Errors:** Always check `llvm::Error` and `llvm::Expected`. Unchecked errors will cause the program to abort.
*   **Unrecoverable Errors:** Use `llvm_unreachable("Explanation")` for unreachable code paths, or `report_fatal_error("Message")` for severe, unrecoverable failures.
*   **Assertions:** Use `assert(condition && "Message")` liberally to document and verify assumptions.

### Imports & Modularity
*   Forward declare classes in headers whenever possible to reduce compile times.
*   Keep includes strictly to what is needed in the header file; move everything else to the `.cpp` file.

### Agent Workflow inside LLVM
1.  **Grep & Glob:** LLVM is huge. Use `grep` and `glob` extensively to find definitions (`isa`, AST nodes, HLSL specific classes).
2.  **Look for Examples:** If implementing a new HLSL intrinsic or AST node, find an existing one (e.g., `hlsl::Resource` or an OpenCL equivalent) and mimic its structure exactly.
3.  **Read CMake Caches:** Refer to the `flake.nix` CMake flags to understand how the project is configured (e.g. `HLSL_DISABLE_SOURCE_GENERATION`).
4.  **Test-Driven:** Write your `.hlsl` test case first, run it using `llvm-lit`, and ensure it fails. Then implement the feature in Clang/LLVM.
