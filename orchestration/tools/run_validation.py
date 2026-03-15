# tools/run_validation.py
import argparse
import subprocess
import sys
from pathlib import Path

def run_command(command: list[str]):
    """Runs a command and prints its output, exiting on failure."""
    print(f"\n--- Running: {' '.join(command)} ---")
    try:
        process = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        if process.stdout:
            print(process.stdout)
        if process.stderr:
            print(process.stderr, file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"--- Command failed: {' '.join(command)} ---", file=sys.stderr)
        print(e.stdout, file=sys.stdout)
        print(e.stderr, file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"--- Command not found: {command[0]} ---", file=sys.stderr)
        print("Please ensure the command is on your PATH and the virtual environment is active.", file=sys.stderr)
        sys.exit(1)

def main():
    """
    Main function to run validation steps on a given file.
    """
    parser = argparse.ArgumentParser(
        description="Run validation (linting, formatting, testing) on a file."
    )
    parser.add_argument(
        "file_path",
        type=Path,
        help="The path to the source file to validate."
    )
    parser.add_argument(
        "--test_path",
        type=Path,
        help="Optional: The path to the test file to run."
    )
    args = parser.parse_args()

    file_path = args.file_path
    if not file_path.exists():
        print(f"Error: File not found at {file_path}", file=sys.stderr)
        sys.exit(1)

    # Determine paths to executables in the virtual environment
    venv_dir = Path(sys.executable).parent
    ruff_path = venv_dir / "ruff.exe"
    pytest_path = venv_dir / "pytest.exe"

    # Step 1: Format the file
    run_command([str(ruff_path), "format", str(file_path)])

    # Step 2: Lint the file
    run_command([str(ruff_path), "check", str(file_path)])

    # Step 3: Run tests if a path is provided
    if args.test_path:
        test_path = args.test_path
        if not test_path.exists():
            print(f"Error: Test file not found at {test_path}", file=sys.stderr)
            sys.exit(1)
        run_command([str(pytest_path), str(test_path)])
    else:
        print("\n--- Skipping tests: --test_path not provided ---")

    print("\n--- Validation successful ---")

if __name__ == "__main__":
    main()
