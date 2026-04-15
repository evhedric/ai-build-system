import subprocess
import re
import sys


# Patterns that indicate a missing dependency in command output
MISSING_DEPENDENCY_PATTERNS = [
    r"No module named '([^']+)'",
    r"ModuleNotFoundError: No module named '([^']+)'",
    r"ImportError: No module named '([^']+)'",
    r"command not found: (\S+)",
    r"(\S+): command not found",
    r"which: no (\S+) in",
    r"error: externally-managed-environment",  # pip constraint notice
    r"ImportError: cannot import name '.+' from '([^']+)'",
]


def _detect_missing_dependency(output: str) -> str | None:
    """
    Inspect command output (stdout + stderr) for patterns that indicate
    a missing Python package or executable dependency.

    Returns the name of the missing package if detected, otherwise None.
    """
    for pattern in MISSING_DEPENDENCY_PATTERNS:
        match = re.search(pattern, output)
        if match:
            # The first capture group holds the package/module name
            package_name = match.group(1) if match.lastindex and match.lastindex >= 1 else None
            if package_name:
                # Normalise sub-module paths: "foo.bar.baz" -> "foo"
                top_level = package_name.split(".")[0]
                return top_level
    return None


def _install_dependency(package: str) -> tuple[bool, str]:
    """
    Attempt to install *package* using 'python -m pip install <package>'.

    Executes exactly once and returns a (success: bool, output: str) tuple.
    The caller is responsible for ensuring this function is not called more
    than once per command failure so that no infinite install loops occur.
    """
    install_cmd = [sys.executable, "-m", "pip", "install", package]
    try:
        result = subprocess.run(
            install_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        combined = result.stdout + result.stderr
        if result.returncode == 0:
            return True, combined
        return False, combined
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def _build_install_failure_error(
    package: str,
    install_output: str,
    original_returncode: int,
) -> str:
    """
    Build a human-readable error message for a failed dependency installation.

    The message explains what went wrong, shows the pip output for diagnosis,
    and provides concrete manual resolution steps the user can follow.

    Parameters
    ----------
    package:
        The name of the package that could not be installed.
    install_output:
        The combined stdout/stderr produced by the failed pip invocation.
    original_returncode:
        The return code of the original command that triggered the install
        attempt, included for context.

    Returns
    -------
    str
        A multi-line error string suitable for inclusion in a result dict's
        ``error`` field.
    """
    manual_install_cmd = f"{sys.executable} -m pip install {package}"
    lines = [
        f"Dependency installation failed for package '{package}'.",
        "",
        "Context",
        "-------",
        f"  - The original command exited with return code {original_returncode}.",
        f"  - An automatic 'pip install {package}' was attempted but did not succeed.",
        "",
        "Installation output",
        "-------------------",
        install_output.strip() if install_output.strip() else "(no output captured)",
        "",
        "Suggested manual resolution steps",
        "----------------------------------",
        f"  1. Run the following command in your terminal to install the package manually:",
        f"         {manual_install_cmd}",
        "  2. If pip reports a permissions error, try running with elevated privileges:",
        f"         {sys.executable} -m pip install --user {package}",
        "  3. If you are inside a virtual environment, make sure it is activated and",
        "     then retry the install command above.",
        "  4. If the package name shown here differs from the PyPI distribution name",
        "     (e.g. the module 'cv2' is distributed as 'opencv-python'), install the",
        "     correct distribution name instead.",
        "  5. If a network or proxy error is shown in the installation output above,",
        "     check your internet connection and proxy settings before retrying.",
        "  6. If the error mentions 'externally-managed-environment', consider using",
        "     a virtual environment or passing '--break-system-packages' (use with care).",
    ]
    return "\n".join(lines)


def run_command(command: list[str] | str, **kwargs) -> dict:
    """
    Execute a shell command with environment self-healing.

    Behaviour
    ---------
    1. Run the command.
    2. If it succeeds, return the result immediately.
    3. If it fails, inspect stdout + stderr for a recognisable missing-dependency
       pattern.
    4. If a missing dependency is detected:
       a. Attempt to install it **once** with ``python -m pip install <package>``.
       b. If installation fails, return a clear error — do **not** retry.
       c. If installation succeeds, retry the original command exactly once.
       d. Return the result of the retry (success or failure) without further
          looping.
    5. If no missing dependency is detected, return the original failure result.

    The self-healing logic is guarded so that installation and retry each happen
    at most once per call — there is no possibility of an infinite loop.

    Parameters
    ----------
    command:
        The command to run.  May be a list of strings or a single string.
        When a string is supplied, ``shell=True`` is implied.
    **kwargs:
        Extra keyword arguments forwarded to :func:`subprocess.run`.

    Returns
    -------
    dict with keys:
        ``returncode`` (int), ``stdout`` (str), ``stderr`` (str),
        ``error`` (str | None) — human-readable error summary when applicable.
    """
    shell = isinstance(command, str)

    def _run(cmd):
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=shell,
            **kwargs,
        )
        return result

    # ------------------------------------------------------------------ #
    # Step 1 — initial execution                                           #
    # ------------------------------------------------------------------ #
    result = _run(command)

    if result.returncode == 0:
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": None,
        }

    # ------------------------------------------------------------------ #
    # Step 2 — command failed; check for a missing dependency             #
    # ------------------------------------------------------------------ #
    combined_output = result.stdout + result.stderr
    missing_package = _detect_missing_dependency(combined_output)

    if missing_package is None:
        # Not a dependency issue — surface the original failure as-is.
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": (
                f"Command failed with return code {result.returncode}. "
                "No missing dependency detected."
            ),
        }

    # ------------------------------------------------------------------ #
    # Step 3 — attempt to install the missing dependency (once only)      #
    #                                                                      #
    # _install_dependency is called exactly once here.  No loop wraps     #
    # this call, so infinite installation cycles are structurally          #
    # impossible.  If installation fails we return immediately without     #
    # retrying the original command.                                       #
    # ------------------------------------------------------------------ #
    install_success, install_output = _install_dependency(missing_package)

    if not install_success:
        # ---------------------------------------------------------------- #
        # Step 5 — clear, actionable error message for a failed install    #
        #                                                                   #
        # _build_install_failure_error constructs a structured message     #
        # that:                                                             #
        #   • States what failed and why.                                  #
        #   • Reproduces the pip output verbatim so the caller can         #
        #     diagnose the root cause.                                     #
        #   • Lists concrete manual resolution steps covering the most     #
        #     common failure modes (permissions, venv, wrong package name, #
        #     network issues, externally-managed environments).            #
        #                                                                   #
        # We return immediately here — the original command is NOT         #
        # retried after a failed install, preventing any retry loop.       #
        # ---------------------------------------------------------------- #
        error_message = _build_install_failure_error(
            package=missing_package,
            install_output=install_output,
            original_returncode=result.returncode,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": error_message,
        }

    # ------------------------------------------------------------------ #
    # Step 4 — retry the original command once after successful install   #
    #                                                                      #
    # This is the single permitted retry.  The result is returned         #
    # unconditionally — no further self-healing is attempted, preventing  #
    # any infinite loop.                                                   #
    #                                                                      #
    # Specifically:                                                        #
    #   - _run(command) is called here for the second and final time.     #
    #   - There is no recursive call to run_command, so self-healing      #
    #     logic cannot be re-entered from this retry path.                #
    #   - Whether the retry succeeds or fails, we return immediately.     #
    # ------------------------------------------------------------------ #
    retry_result = _run(command)

    if retry_result.returncode == 0:
        return {
            "returncode": retry_result.returncode,
            "stdout": retry_result.stdout,
            "stderr": retry_result.stderr,
            "error": None,
        }

    # Retry also failed — report without looping further.
    return {
        "returncode": retry_result.returncode,
        "stdout": retry_result.stdout,
        "stderr": retry_result.stderr,
        "error": (
            f"Command failed even after successfully installing '{missing_package}'. "
            f"Return code: {retry_result.returncode}. "
            "Please check the command output above and verify that the installed "
            "package version is compatible with your environment. "
            f"You may also try running: {sys.executable} -m pip install --upgrade {missing_package}"
        ),
    }