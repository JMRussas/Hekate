import os

output_files = [
    "task_a.out",
    "task_b.out",
    "task_c.out",
    "task_d.out",
]

print("Task E: Reading outputs of previous tasks.")

for file in output_files:
    try:
        with open(file, 'r') as f:
            content = f.read()
            print(f"--- Output of {file} ---")
            print(content)
            print(f"--- End of {file} ---")
    except FileNotFoundError:
        print(f"--- Output of {file} ---")
        print(f"Output file not found. Task likely failed as expected.")
        print(f"--- End of {file} ---")

print("Summary task complete.")
